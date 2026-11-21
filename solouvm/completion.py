"""Static shell completion generators (Bash 3.2+ and Zsh) built from CLI parser specs."""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass, field

from solouvm.backends import BACKENDS
from solouvm.cli_parser import Handler, build_parser
from solouvm.errors import ConfigError
from solouvm.model import _CONFIDENCES, _JOB_STATUSES, _REQUIRED, _VERDICTS
from solouvm.notes.query import VALID_NOTE_TYPES
from solouvm.ops import HELP_TOPICS, SCHEMAS
from solouvm.roles import VALID_ROLES

SUPPORTED_SHELLS: tuple[str, ...] = ("bash", "zsh")

__all__ = ["SUPPORTED_SHELLS", "generate_completion"]

_GLOBAL_PATH_KINDS: dict[str, str] = {
    "--config": "file",
}

_OPTION_PATH_KINDS: dict[tuple[str, str], str] = {
    ("report", "--output"): "file",
    ("report", "--compare"): "file",
    ("doc", "--output"): "dir",
    ("cov merge", "--output"): "dir",
    ("cov report", "--merged-dir"): "dir",
    ("update", "--prefix"): "dir",
    ("update", "--python"): "file",
}


def _static_extra_choices() -> dict[tuple[str, str], tuple[str, ...]]:
    notes_statuses = tuple(dict.fromkeys((*_REQUIRED.keys(), "done")))
    return {
        ("", "--role"): tuple(sorted(VALID_ROLES)),
        ("init", "--simulator"): tuple(BACKENDS.keys()),
        ("init", "--agents"): ("claude",),
        ("rerun", "--status"): tuple(sorted(_JOB_STATUSES)),
        ("notes", "--type"): tuple(sorted(VALID_NOTE_TYPES)),
        ("notes", "--status"): notes_statuses,
        ("bug update", "--status"): tuple(_REQUIRED.keys()),
        ("triage write", "--verdict"): tuple(_VERDICTS),
        ("triage write", "--confidence"): tuple(_CONFIDENCES),
        ("help", "topic"): tuple(HELP_TOPICS.keys()),
        ("schema", "name"): tuple(SCHEMAS.keys()),
    }


@dataclass(frozen=True)
class _OptionSpec:
    flags: tuple[str, ...]
    help_text: str
    takes_value: bool
    repeatable: bool
    metavar: str
    choices: tuple[str, ...] = ()
    path_kind: str | None = None

    @property
    def primary_flag(self) -> str:
        long_flags = [f for f in self.flags if f.startswith("--")]
        return long_flags[0] if long_flags else self.flags[0]


@dataclass(frozen=True)
class _PositionalSpec:
    dest: str
    help_text: str
    choices: tuple[str, ...] = ()
    path_kind: str | None = None


@dataclass
class _CommandSpec:
    path: str
    name: str
    help_text: str = ""
    options: list[_OptionSpec] = field(default_factory=list)
    positionals: list[_PositionalSpec] = field(default_factory=list)
    subcommands: dict[str, _CommandSpec] = field(default_factory=dict)

    @property
    def bash_key(self) -> str:
        return self.path.replace(" ", "_").replace("-", "_") if self.path else ""

    @property
    def fn_suffix(self) -> str:
        return f"_{self.bash_key}" if self.bash_key else ""


def _noop_handler(_args: argparse.Namespace) -> int:
    return 0


def _action_takes_value(action: argparse.Action) -> bool:
    no_value_types = (
        argparse._StoreTrueAction,
        argparse._StoreFalseAction,
        argparse._StoreConstAction,
        argparse._CountAction,
        argparse._HelpAction,
        argparse._VersionAction,
    )
    if isinstance(action, no_value_types):
        return False
    return action.nargs != 0


def _action_repeatable(action: argparse.Action) -> bool:
    return isinstance(
        action,
        (argparse._AppendAction, argparse._AppendConstAction, argparse._CountAction),
    )


def _extract_command_spec(
    parser: argparse.ArgumentParser,
    *,
    path: str,
    name: str,
    help_text: str,
    extra_choices: dict[tuple[str, str], tuple[str, ...]],
) -> _CommandSpec:
    spec = _CommandSpec(path=path, name=name, help_text=help_text)
    seen_flags: set[tuple[str, ...]] = set()

    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            help_map = {ca.dest: (ca.help or "") for ca in action._choices_actions}
            for sub_name, sub_parser in action.choices.items():
                sub_path = f"{path} {sub_name}".strip()
                spec.subcommands[sub_name] = _extract_command_spec(
                    sub_parser,
                    path=sub_path,
                    name=sub_name,
                    help_text=help_map.get(sub_name, ""),
                    extra_choices=extra_choices,
                )
            continue

        if action.help is argparse.SUPPRESS:
            continue

        raw_help = action.help if isinstance(action.help, str) else ""
        action_choices = tuple(str(c) for c in action.choices) if action.choices else ()

        if action.option_strings:
            flags = tuple(action.option_strings)
            if flags in seen_flags:
                continue
            seen_flags.add(flags)

            primary = max(flags, key=len)
            takes_value = _action_takes_value(action)
            choices = (
                action_choices
                or extra_choices.get((path, primary))
                or extra_choices.get(("", primary))
                or ()
            )
            path_kind = _OPTION_PATH_KINDS.get((path, primary)) or _GLOBAL_PATH_KINDS.get(primary)
            metavar = str(action.metavar or action.dest or "value").lower()
            spec.options.append(
                _OptionSpec(
                    flags=flags,
                    help_text=raw_help,
                    takes_value=takes_value,
                    repeatable=_action_repeatable(action),
                    metavar=metavar,
                    choices=choices,
                    path_kind=path_kind,
                )
            )
        else:
            choices = action_choices or extra_choices.get((path, action.dest), ())
            spec.positionals.append(
                _PositionalSpec(
                    dest=action.dest,
                    help_text=raw_help,
                    choices=choices,
                    path_kind=None,
                )
            )

    return spec


def _walk_commands(spec: _CommandSpec) -> list[_CommandSpec]:
    out = [spec]
    for sub_spec in spec.subcommands.values():
        out.extend(_walk_commands(sub_spec))
    return out


def _sh_escape_sq(text: str) -> str:
    return text.replace("'", "'\\''")


def _zsh_escape_desc(text: str) -> str:
    escaped = (
        text.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace(":", "\\:")
    )
    return _sh_escape_sq(escaped)


def _zsh_describe_escape(text: str) -> str:
    return _sh_escape_sq(text.replace("\\", "\\\\").replace(":", "\\:"))


# ---------- Bash 3.2+ completion ----------


def _render_bash(root: _CommandSpec) -> str:
    all_specs = _walk_commands(root)
    global_val_flags = sorted({f for opt in root.options if opt.takes_value for f in opt.flags})
    global_opt_sigs = {
        (opt.primary_flag, opt.choices, opt.path_kind)
        for opt in root.options
        if opt.takes_value and (opt.choices or opt.path_kind)
    }
    group_names = sorted(name for name, sub in root.subcommands.items() if sub.subcommands)

    lines: list[str] = [
        "# bash completion for solouvm (generated by solouvm completion bash)",
        "",
        "_solouvm_is_group() {",
        '    case "$1" in',
    ]
    if group_names:
        lines.append(f"        {'|'.join(group_names)}) return 0 ;;")
    lines.extend(
        [
            "        *) return 1 ;;",
            "    esac",
            "}",
            "",
            "_solouvm_opt_takes_value() {",
            '    local key="$1" opt="$2"',
            '    case "$opt" in',
        ]
    )
    if global_val_flags:
        lines.append(f"        {'|'.join(global_val_flags)}) return 0 ;;")
    lines.extend(["    esac", '    case "$key:$opt" in'])
    for spec in all_specs:
        if not spec.path:
            continue
        cmd_val_flags = sorted(
            {
                f
                for opt in spec.options
                if opt.takes_value
                for f in opt.flags
                if f not in global_val_flags
            }
        )
        if cmd_val_flags:
            patterns = "|".join(f"{spec.bash_key}:{f}" for f in cmd_val_flags)
            lines.append(f"        {patterns}) return 0 ;;")
    lines.extend(
        [
            "    esac",
            "    return 1",
            "}",
            "",
            "_solouvm_complete_paths() {",
            '    local kind="$1" prefix="$2" val="$3" line',
            '    local flag="-f"',
            '    if [ "$kind" = "dir" ]; then',
            '        flag="-d"',
            "    fi",
            '    while IFS= read -r line; do',
            '        COMPREPLY+=("${prefix}${line}")',
            '    done < <(compgen "$flag" -- "$val" || true)',
            '    if [ -z "$prefix" ]; then',
            "        compopt -o filenames 2>/dev/null || true",
            "    fi",
            "}",
            "",
            "_solouvm_complete_opt_value() {",
            '    local key="$1" opt="$2" val="$3" prefix="$4"',
            "    COMPREPLY=()",
            '    case "$key:$opt" in',
        ]
    )

    for spec in all_specs:
        if not spec.path:
            continue
        for opt in spec.options:
            if not opt.takes_value or not (opt.choices or opt.path_kind):
                continue
            if (opt.primary_flag, opt.choices, opt.path_kind) in global_opt_sigs:
                continue
            patterns = "|".join(f"{spec.bash_key}:{f}" for f in opt.flags)
            if opt.choices:
                words = " ".join(opt.choices)
                lines.append(
                    f'        {patterns}) COMPREPLY=( $(compgen -W "{words}" -P "$prefix" '
                    '-- "$val" || true) ); return 0 ;;'
                )
            elif opt.path_kind:
                lines.append(
                    f'        {patterns}) _solouvm_complete_paths "{opt.path_kind}" '
                    '"$prefix" "$val"; return 0 ;;'
                )

    lines.extend(["    esac", '    case "$opt" in'])
    for opt in root.options:
        if not opt.takes_value or not (opt.choices or opt.path_kind):
            continue
        patterns = "|".join(opt.flags)
        if opt.choices:
            words = " ".join(opt.choices)
            lines.append(
                f'        {patterns}) COMPREPLY=( $(compgen -W "{words}" -P "$prefix" '
                '-- "$val" || true) ); return 0 ;;'
            )
        elif opt.path_kind:
            lines.append(
                f'        {patterns}) _solouvm_complete_paths "{opt.path_kind}" '
                '"$prefix" "$val"; return 0 ;;'
            )

    lines.extend(
        [
            "    esac",
            "    return 0",
            "}",
            "",
            "_solouvm_complete() {",
            "    local cur i word cmd subcmd key expect_opt after_dd pos opt val words_str",
            "    COMPREPLY=()",
            '    cur="${COMP_WORDS[COMP_CWORD]}"',
            '    cmd=""',
            '    subcmd=""',
            '    key=""',
            '    expect_opt=""',
            "    after_dd=0",
            "    pos=0",
            "",
            "    i=1",
            '    while [ "$i" -lt "$COMP_CWORD" ]; do',
            '        word="${COMP_WORDS[$i]}"',
            '        if [ -n "$expect_opt" ]; then',
            '            if [ "$word" = "=" ]; then',
            "                i=$((i + 1))",
            "                continue",
            "            fi",
            '            expect_opt=""',
            "            i=$((i + 1))",
            "            continue",
            "        fi",
            '        if [ "$after_dd" -eq 0 ]; then',
            '            case "$word" in',
            "                --)",
            "                    after_dd=1",
            "                    i=$((i + 1))",
            "                    continue",
            "                    ;;",
            "                --*=*)",
            "                    i=$((i + 1))",
            "                    continue",
            "                    ;;",
            "                -*)",
            '                    if _solouvm_opt_takes_value "$key" "$word"; then',
            '                        expect_opt="$word"',
            "                    fi",
            "                    i=$((i + 1))",
            "                    continue",
            "                    ;;",
            "            esac",
            "        fi",
            '        if [ -z "$cmd" ]; then',
            '            cmd="$word"',
            '            key="${cmd//-/_}"',
            '        elif [ -z "$subcmd" ] && _solouvm_is_group "$cmd"; then',
            '            subcmd="$word"',
            '            key="${cmd//-/_}_${subcmd//-/_}"',
            "        else",
            "            pos=$((pos + 1))",
            "        fi",
            "        i=$((i + 1))",
            "    done",
            "",
            '    if [ -n "$expect_opt" ]; then',
            '        _solouvm_complete_opt_value "$key" "$expect_opt" "$cur" ""',
            "        return 0",
            "    fi",
            "",
            '    if [ "$after_dd" -eq 0 ]; then',
            '        case "$cur" in',
            "            --*=*)",
            '                opt="${cur%%=*}"',
            '                val="${cur#*=}"',
            '                _solouvm_complete_opt_value "$key" "$opt" "$val" "${opt}="',
            "                return 0",
            "                ;;",
            "            -*)",
            '                words_str=""',
            '                case "$key" in',
        ]
    )

    for spec in all_specs:
        opt_words = " ".join(f for opt in spec.options for f in opt.flags)
        lines.append(f'                    "{spec.bash_key}") words_str="{opt_words}" ;;')

    lines.extend(
        [
            "                esac",
            '                if [ -n "$words_str" ]; then',
            '                    COMPREPLY=( $(compgen -W "$words_str" -- "$cur" || true) )',
            "                fi",
            "                return 0",
            "                ;;",
            "        esac",
            "    fi",
            "",
            '    words_str=""',
            '    case "$key" in',
        ]
    )

    for spec in all_specs:
        if spec.subcommands:
            sub_words = " ".join(spec.subcommands.keys())
            lines.append(f'        "{spec.bash_key}") words_str="{sub_words}" ;;')

    lines.extend(
        [
            "    esac",
            '    if [ -n "$words_str" ]; then',
            '        COMPREPLY=( $(compgen -W "$words_str" -- "$cur" || true) )',
            "        return 0",
            "    fi",
            "",
            '    case "$key:$pos" in',
        ]
    )

    for spec in all_specs:
        for idx, pspec in enumerate(spec.positionals):
            if pspec.choices:
                choice_words = " ".join(pspec.choices)
                lines.append(f'        "{spec.bash_key}:{idx}") words_str="{choice_words}" ;;')

    lines.extend(
        [
            "    esac",
            '    if [ -n "$words_str" ]; then',
            '        COMPREPLY=( $(compgen -W "$words_str" -- "$cur" || true) )',
            "    fi",
            "    return 0",
            "}",
            "",
            "complete -F _solouvm_complete solouvm",
            "",
        ]
    )
    return "\n".join(lines)


# ---------- Zsh completion ----------


def _zsh_opt_specs(options: list[_OptionSpec]) -> list[str]:
    entries: list[str] = []
    for opt in options:
        desc = f"[{_zsh_escape_desc(opt.help_text)}]" if opt.help_text else ""
        if opt.takes_value:
            if opt.choices:
                val_part = f":{opt.metavar}:({' '.join(opt.choices)})"
            elif opt.path_kind == "file":
                val_part = f":{opt.metavar}:_files"
            elif opt.path_kind == "dir":
                val_part = f":{opt.metavar}:_files -/"
            else:
                val_part = f":{opt.metavar}:"
        else:
            val_part = ""

        if opt.repeatable:
            for flag in opt.flags:
                entries.append(f"'*{flag}{desc}{val_part}'")
        elif len(opt.flags) > 1:
            excl = f"({' '.join(opt.flags)})"
            for flag in opt.flags:
                entries.append(f"'{excl}{flag}{desc}{val_part}'")
        else:
            entries.append(f"'{opt.flags[0]}{desc}{val_part}'")
    return entries


def _zsh_positional_specs(positionals: list[_PositionalSpec]) -> list[str]:
    entries: list[str] = []
    for idx, pspec in enumerate(positionals, start=1):
        label = _zsh_escape_desc(pspec.dest)
        if pspec.choices:
            entries.append(f"'{idx}:{label}:({' '.join(pspec.choices)})'")
        elif pspec.path_kind == "file":
            entries.append(f"'{idx}:{label}:_files'")
        elif pspec.path_kind == "dir":
            entries.append(f"'{idx}:{label}:_files -/'")
        else:
            entries.append(f"'{idx}:{label}:'")
    return entries


def _render_zsh_describe_fn(
    fn_name: str, label: str, subcommands: dict[str, _CommandSpec]
) -> list[str]:
    out = [f"{fn_name}() {{", "    local -a commands", "    commands=("]
    for sub_name, sub_spec in subcommands.items():
        if sub_spec.help_text:
            item = f"{sub_name}:{_zsh_describe_escape(sub_spec.help_text)}"
        else:
            item = sub_name
        out.append(f"        '{item}'")
    out.extend(["    )", f"    _describe -t commands '{label}' commands", "}", ""])
    return out


def _render_zsh_leaf_fn(spec: _CommandSpec) -> list[str]:
    fn_name = f"_solouvm_cmd{spec.fn_suffix}"
    arg_specs = _zsh_opt_specs(spec.options) + _zsh_positional_specs(spec.positionals)
    if not arg_specs:
        return [f"{fn_name}() {{", "    _arguments -s -S", "}", ""]
    out = [f"{fn_name}() {{", "    _arguments -s -S \\"]
    for idx, item in enumerate(arg_specs):
        suffix = " \\" if idx < len(arg_specs) - 1 else ""
        out.append(f"        {item}{suffix}")
    out.extend(["}", ""])
    return out


def _render_zsh_dispatch_fn(spec: _CommandSpec, fn_name: str, desc_fn: str) -> list[str]:
    arg_specs = [*_zsh_opt_specs(spec.options), "'(-): :->command'", "'(-)*:: :->args'"]
    out = [
        f"{fn_name}() {{",
        "    local context state state_descr line",
        "    typeset -A opt_args",
        "",
        "    _arguments -C -s -S \\",
    ]
    for idx, item in enumerate(arg_specs):
        suffix = " \\" if idx < len(arg_specs) - 1 else " && return"
        out.append(f"        {item}{suffix}")
    out.extend(
        [
            "",
            '    case "$state" in',
            "        command)",
            f"            {desc_fn}",
            "            ;;",
            "        args)",
            '            case "$words[1]" in',
        ]
    )
    for sub_name, sub_spec in spec.subcommands.items():
        target = f"_solouvm_cmd{sub_spec.fn_suffix}"
        out.append(f"                {sub_name}) {target} ;;")
    out.extend(["            esac", "            ;;", "    esac", "}", ""])
    return out


def _render_zsh(root: _CommandSpec) -> str:
    lines: list[str] = [
        "#compdef solouvm",
        "# zsh completion for solouvm (generated by solouvm completion zsh)",
        "",
    ]

    lines.extend(_render_zsh_describe_fn("_solouvm_commands", "solouvm command", root.subcommands))
    for sub_spec in root.subcommands.values():
        if sub_spec.subcommands:
            lines.extend(
                _render_zsh_describe_fn(
                    f"_solouvm{sub_spec.fn_suffix}_commands",
                    f"solouvm {sub_spec.name} command",
                    sub_spec.subcommands,
                )
            )

    for spec in _walk_commands(root):
        if not spec.path:
            continue
        if spec.subcommands:
            lines.extend(
                _render_zsh_dispatch_fn(
                    spec,
                    f"_solouvm_cmd{spec.fn_suffix}",
                    f"_solouvm{spec.fn_suffix}_commands",
                )
            )
        else:
            lines.extend(_render_zsh_leaf_fn(spec))

    lines.extend(_render_zsh_dispatch_fn(root, "_solouvm", "_solouvm_commands"))
    lines.extend(
        [
            'if [ "${funcstack[1]:-}" = "_solouvm" ]; then',
            '    _solouvm "$@"',
            "elif (( $+functions[compdef] )); then",
            "    compdef _solouvm solouvm",
            "fi",
            "",
        ]
    )
    return "\n".join(lines)


def generate_completion(shell: str, parser: argparse.ArgumentParser | None = None) -> str:
    """Return a static shell completion script for ``bash`` or ``zsh``."""
    if shell not in SUPPORTED_SHELLS:
        raise ConfigError(
            f"unsupported shell {shell!r}",
            code="CFG_INVALID",
            hint=f"supported shells: {', '.join(SUPPORTED_SHELLS)}",
        )

    target_parser = parser
    if target_parser is None:
        handlers: dict[str, Handler] = defaultdict(lambda: _noop_handler)
        target_parser = build_parser(handlers)

    root_spec = _extract_command_spec(
        target_parser,
        path="",
        name="solouvm",
        help_text="",
        extra_choices=_static_extra_choices(),
    )
    if shell == "bash":
        return _render_bash(root_spec)
    return _render_zsh(root_spec)
