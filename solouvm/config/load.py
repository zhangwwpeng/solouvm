"""Find, run, validate and merge ``solouvm.conf.py`` / ``solouvm.local.py`` (config.md §1-§3)."""

from __future__ import annotations

import contextlib
import os
import re
import runpy
import shlex
import sys
import traceback
from collections.abc import Callable
from difflib import get_close_matches
from pathlib import Path

from solouvm.backends import get_backend
from solouvm.config.schema import (
    ANY_KEY,
    BUILD_DEFAULTS,
    DEFAULTS,
    PROVIDER_SPEC,
    REQUIRED,
    SPEC,
    BuildConfig,
    CheckConfig,
    Config,
    DocConfig,
    HistoryConfig,
    JobHooks,
    LauncherConfig,
    LimitsConfig,
    ProviderConfig,
    RegressConfig,
    RunConfig,
    SyncConfig,
    TransportConfig,
    TriageConfig,
    UvmConfig,
    invalid,
    v_choice,
    v_str_list,
)
from solouvm.errors import ConfigError
from solouvm.model import Checks
from solouvm.providers.base import (
    BUILTIN_LAUNCHER_KINDS,
    BUILTIN_SYNC_KINDS,
    BUILTIN_TRANSPORT_KINDS,
    ProviderRegistry,
    build_scoped_registry,
)

CONF_NAME = "solouvm.conf.py"
LOCAL_NAME = "solouvm.local.py"
SHADOW_NAME = "solouvm.py"

# builds / providers 的名字会出现在目录名与命令行里，限制字符集
# sim 中按正则列表处理的字段，其余字段写成列表时视为命令
_REGEX_LIST_FIELDS = ("error_regex", "license_regex")

_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")

# ${VAR:-默认值}、${VAR}、$VAR
_ENV_RE = re.compile(r"\$\{(?P<braced>\w+)(?::-(?P<default>[^}]*))?\}|\$(?P<bare>[A-Za-z_]\w*)")


# ---------------------------------------------------------------- 公共函数


def find_config(start: Path, *, search_parents: bool = True) -> Path:
    """Return the config file for ``start``.

    ``start`` may be the config file itself or a directory; for a directory the search
    walks up to ``/`` unless ``search_parents`` is False (``-c DIR``).
    """
    start = Path(start).expanduser().absolute()
    if start.is_file():
        return start
    if not start.is_dir():
        raise ConfigError(f"config path {str(start)!r} does not exist", code="CFG_NOT_FOUND")
    candidates = [start, *start.parents] if search_parents else [start]
    for directory in candidates:
        candidate = directory / CONF_NAME
        if candidate.is_file():
            return candidate
    where = f"{start} or any parent directory" if search_parents else str(start)
    raise ConfigError(
        f"{CONF_NAME} not found in {where}",
        code="CFG_NOT_FOUND",
        hint="run 'solouvm init' to create one, or pass -c PATH",
    )


def deep_merge(base: dict, override: dict) -> dict:
    """Merge ``override`` onto ``base``: dicts recurse, lists and scalars replace.

    Returns a new dict; neither input is modified and no list/dict is shared with them.
    """
    out = {key: _copy(value) for key, value in base.items()}
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = _copy(value)
    return out


def expand_env(value: str) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` / ``$VAR`` with the A-side environment.

    Only for values used on A (config.md §2.11). ``:-`` uses the default when VAR is unset
    or empty; an unset VAR without default raises ``CFG_INVALID``.
    """

    def repl(match: re.Match[str]) -> str:
        name = match.group("braced") or match.group("bare")
        current = os.environ.get(name)
        default = match.group("default")
        if default is not None:
            return current or default
        if current is None:
            raise ConfigError(
                f"environment variable {name!r} is not set (in {value!r})",
                code="CFG_INVALID",
                hint=f"set {name} or write ${{{name}:-default}}",
            )
        return current

    return _ENV_RE.sub(repl, value)


_DEFAULT_SECTION_KINDS: dict[str, str] = {
    "transport": "local",
    "launcher": "make",
    "sync": "none",
}
_BUILTIN_SECTION_KINDS: dict[str, frozenset[str]] = {
    "transport": BUILTIN_TRANSPORT_KINDS,
    "launcher": BUILTIN_LAUNCHER_KINDS,
    "sync": BUILTIN_SYNC_KINDS,
}


def _collect_plugins_and_kinds(
    layers: list[tuple[str, dict, Path | None]],
    root: Path,
) -> tuple[ProviderRegistry, dict[tuple[str, str], object]]:
    """Load effective plugins and collect final provider section kinds across raw layers."""
    raw_plugins: list[str] = list(DEFAULTS["plugins"])  # type: ignore[arg-type]
    plugins_file: Path | None = layers[0][2] if layers else None
    effective_kinds: dict[tuple[str, str], object] = {}

    for _label, data, file in layers:
        if "plugins" in data:
            raw_plugins = _with_file(file, v_str_list, "plugins", data["plugins"])
            plugins_file = file
        raw_providers = data.get("providers")
        if isinstance(raw_providers, dict):
            for pname, pval in raw_providers.items():
                if not isinstance(pname, str) or not isinstance(pval, dict):
                    continue
                for sec in _DEFAULT_SECTION_KINDS:
                    sec_val = pval.get(sec)
                    if isinstance(sec_val, dict) and "kind" in sec_val:
                        effective_kinds[(pname, sec)] = sec_val["kind"]

    registry = _with_file(plugins_file, build_scoped_registry, raw_plugins, root)
    return registry, effective_kinds


def _validate_provider_section(
    section: str,
    value: object,
    path: str,
    valid_kinds: tuple[str, ...],
    effective_kind: object,
) -> dict:
    if not isinstance(value, dict):
        raise invalid(path, "dict", value)
    if "kind" in value:
        v_choice(*valid_kinds)(f"{path}.kind", value["kind"])
    layer_kind = value.get("kind", effective_kind)
    if isinstance(layer_kind, str) and layer_kind in _BUILTIN_SECTION_KINDS[section]:
        base_spec = dict(PROVIDER_SPEC[section])  # type: ignore[arg-type]
        base_spec["kind"] = v_choice(*valid_kinds)
        return _validate(value, base_spec, f"{path}.")
    out: dict = {}
    for k, v in value.items():
        if not isinstance(k, str):
            raise invalid(f"{path}.{k}", "str key", k)
        out[k] = _copy(v)
    return out


def _validate_providers_layer(
    data: object,
    registry: ProviderRegistry,
    effective_kinds: dict[tuple[str, str], object],
) -> dict:
    if not isinstance(data, dict):
        raise invalid("providers", "dict", data)
    known = registry.known_kinds()
    out: dict = {}
    for pname, pval in data.items():
        path = f"providers.{pname}"
        if not isinstance(pname, str):
            raise invalid(path, "str key", pname)
        if not _NAME_RE.match(pname):
            raise invalid(path, "a name of letters, digits, '_', '.', '-'", pname)
        if not isinstance(pval, dict):
            raise invalid(path, "dict", pval)
        p_out: dict = {}
        for key, value in pval.items():
            sub_path = f"{path}.{key}"
            if not isinstance(key, str):
                raise invalid(sub_path, "str key", key)
            if key in _DEFAULT_SECTION_KINDS:
                eff = effective_kinds.get((pname, key), _DEFAULT_SECTION_KINDS[key])
                p_out[key] = _validate_provider_section(key, value, sub_path, known[key], eff)
            elif key == "setup":
                setup_validator = PROVIDER_SPEC["setup"]
                p_out[key] = _copy(setup_validator(sub_path, value))  # type: ignore[operator]
            else:
                raise _unknown_key(sub_path, key, PROVIDER_SPEC, f"{path}.")
        out[pname] = p_out
    return out


def load_config(path: Path, *, cli: dict[str, object] | None = None) -> Config:
    """Load ``path`` (file or directory), merge LOCAL and ``cli``, return a validated Config."""
    path = Path(path).expanduser().absolute()
    if path.is_dir():
        path = find_config(path, search_parents=False)
    elif not path.is_file():
        raise ConfigError(f"config file {str(path)!r} does not exist", code="CFG_NOT_FOUND")
    root = path.parent

    shadow = root / SHADOW_NAME
    if shadow.exists():
        raise ConfigError(
            f"{SHADOW_NAME} in the project root would shadow the solouvm library",
            code="CFG_INVALID",
            file=shadow,
            hint=f"rename {SHADOW_NAME}; configuration lives in {CONF_NAME}",
        )

    layers: list[tuple[str, dict, Path | None]] = [
        ("conf", _execute(path, "CFG"), path),
    ]
    local_path = root / LOCAL_NAME
    if local_path.is_file():
        layers.append(("local", _execute(local_path, "LOCAL"), local_path))
    if cli:
        layers.append(("cli", cli, None))

    registry, effective_kinds = _collect_plugins_and_kinds(layers, root)

    merged = _copy(DEFAULTS)
    sources = dict.fromkeys(_leaves(DEFAULTS), "default")
    for label, data, file in layers:
        checked = _with_file(
            file,
            _validate,
            data,
            SPEC,
            "",
            registry=registry,
            effective_kinds=effective_kinds,
        )
        merged = deep_merge(merged, checked)
        sources.update(dict.fromkeys(_leaves(checked), label))

    # Re-validate merged built-in provider sections in case a layer switched kind from plugin
    # back to a built-in kind leaving plugin-only keys behind.
    if merged["providers"]:
        _with_file(
            path,
            _validate_providers_layer,
            merged["providers"],
            registry,
            effective_kinds,
        )

    for key in REQUIRED:
        if merged[key] is None:
            raise ConfigError(f"missing required key '{key}'", code="CFG_INVALID", file=path)
    return _with_file(path, _build, root, merged, sources)


# ---------------------------------------------------------------- 执行配置文件


def _execute(path: Path, name: str) -> dict:
    """Run a config file with runpy and return its ``name`` dict."""
    root = str(path.parent)
    # 配置文件可以 import 项目里的其他模块
    sys.path.insert(0, root)
    try:
        namespace = runpy.run_path(str(path), run_name="__solouvm_config__")
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        last = traceback.format_exception_only(type(exc), exc)[-1].strip()
        raise ConfigError(
            f"{path.name} raised an exception: {last}",
            code="CFG_LOAD_FAILED",
            file=path,
            line=_user_line(exc, path),
        ) from exc
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(root)
    if name not in namespace:
        raise ConfigError(f"{path.name} must define {name}", code="CFG_LOAD_FAILED", file=path)
    data = namespace[name]
    if not isinstance(data, dict):
        raise ConfigError(
            f"{name} must be a dict, got {type(data).__name__}", code="CFG_INVALID", file=path
        )
    return data


def _user_line(exc: BaseException, path: Path) -> int | None:
    if isinstance(exc, SyntaxError) and exc.lineno and exc.filename == str(path):
        return exc.lineno
    line = None
    for frame in traceback.extract_tb(exc.__traceback__):
        if frame.filename == str(path):
            line = frame.lineno
    return line


# ---------------------------------------------------------------- 校验与合并


def _validate(
    data: dict,
    spec: dict,
    prefix: str,
    *,
    registry: ProviderRegistry | None = None,
    effective_kinds: dict[tuple[str, str], object] | None = None,
) -> dict:
    """Check one layer against ``spec``: unknown keys and value types; returns a copy."""
    out: dict = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if not isinstance(key, str):
            raise invalid(path, "str key", key)
        if key == "providers" and prefix == "" and registry is not None:
            out[key] = _validate_providers_layer(value, registry, effective_kinds or {})
            continue
        if key in spec:
            sub = spec[key]
        elif ANY_KEY in spec:
            if not _NAME_RE.match(key):
                raise invalid(path, "a name of letters, digits, '_', '.', '-'", key)
            sub = spec[ANY_KEY]
        else:
            raise _unknown_key(path, key, spec, prefix)
        if isinstance(sub, dict):
            if not isinstance(value, dict):
                raise invalid(path, "dict", value)
            out[key] = _validate(value, sub, f"{path}.")
        else:
            out[key] = _copy(sub(path, value))  # type: ignore[operator]
    return out


def _unknown_key(path: str, key: str, spec: dict, prefix: str) -> ConfigError:
    valid = [k for k in spec if k != ANY_KEY]
    matches = get_close_matches(key, valid, n=1)
    message = f"unknown key '{path}'"
    if matches:
        message += f"; did you mean '{prefix}{matches[0]}'?"
    return ConfigError(
        message,
        code="CFG_UNKNOWN_KEY",
        hint=f"valid keys{' under ' + prefix.rstrip('.') if prefix else ''}: {', '.join(valid)}",
    )


def _with_file(file: Path | None, func, *args, **kwargs):
    """Call ``func``; attach ``file`` to ConfigErrors that do not name a file yet."""
    try:
        return func(*args, **kwargs)
    except ConfigError as exc:
        if exc.file is None and file is not None:
            exc.file = str(file)
        raise


def _copy(value: object) -> object:
    # 只复制 dict / list；Waiver 等对象原样共享（它们是 frozen 的）
    if isinstance(value, dict):
        return {k: _copy(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_copy(v) for v in value]
    return value


def _leaves(data: dict, prefix: str = "") -> list[str]:
    out: list[str] = []
    for key, value in data.items():
        if isinstance(value, dict) and value:
            out.extend(_leaves(value, f"{prefix}{key}."))
        else:
            out.append(f"{prefix}{key}")
    return out


# ---------------------------------------------------------------- 构造 Config


def _build(root: Path, merged: dict, sources: dict[str, str]) -> Config:
    simulator = merged["simulator"]
    _check_backend(simulator, merged["sim"])
    check = CheckConfig(**merged["check"])
    if check.where == "local":
        # check 的各项只在 check.where=local 时属于"只在 A 上使用"（config.md §2.11）
        check.slang = _expand("check.slang", check.slang)
        check.uvm_src = _expand("check.uvm_src", check.uvm_src)
        check.slang_args = [_expand("check.slang_args", v) for v in check.slang_args]
        check.exclude = [_expand("check.exclude", v) for v in check.exclude]
    providers = _build_providers(merged["providers"])
    if merged["provider"] not in providers:
        raise ConfigError(
            f"provider {merged['provider']!r} is not defined",
            code="CFG_INVALID",
            hint=f"defined providers: {', '.join(sorted(providers))}",
        )
    triage = TriageConfig(**merged["triage"])
    triage.debug_dir = _expand("triage.debug_dir", triage.debug_dir)
    doc = DocConfig(**merged["doc"])
    doc.out = _expand("doc.out", doc.out)
    return Config(
        root=root,
        project=merged["project"] or root.name,
        simulator=simulator,
        top=merged["top"],
        uvm_test=merged["uvm_test"],
        incdirs=merged["incdirs"],
        defines=merged["defines"],
        builds={
            name: BuildConfig(**deep_merge(BUILD_DEFAULTS, value))
            for name, value in merged["builds"].items()
        },
        regress=RegressConfig(**merged["regress"]),
        run=RunConfig(**merged["run"]),
        checks=_build_checks(merged["checks"]),
        job=JobHooks(**merged["job"]),
        sim=merged["sim"],
        providers=providers,
        provider=merged["provider"],
        check=check,
        case_dirs=merged["case_dirs"],
        system_doc=[_expand("system_doc", v) for v in merged["system_doc"]],
        triage=triage,
        doc=doc,
        history=HistoryConfig(**merged["history"]),
        limits=LimitsConfig(**merged["limits"]),
        plugins=merged["plugins"],
        uvm=UvmConfig(**merged["uvm"]),
        sources=sources,
    )


def _expand(path: str, value: str) -> str:
    try:
        return expand_env(value)
    except ConfigError as exc:
        raise ConfigError(f"{path}: {exc.message}", code="CFG_INVALID", hint=exc.hint) from None


def _build_checks(data: dict) -> Checks:
    try:
        return Checks(**data)
    except ConfigError as exc:
        # model 层报 CASE_INVALID；在 CFG 里出现时归为配置错误
        raise ConfigError(exc.message, code="CFG_INVALID", hint=exc.hint) from None


def _check_backend(simulator: str, sim: dict) -> None:
    """Validate ``sim`` against the backend; list commands are allowed (config.md §2.7)."""
    overrides = {
        key: shlex.join(value)
        if isinstance(value, list)
        and key not in _REGEX_LIST_FIELDS
        and all(isinstance(v, str) for v in value)
        else value
        for key, value in sim.items()
    }
    get_backend(simulator, overrides)


def _is_str_list(val: object) -> bool:
    return isinstance(val, list) and all(isinstance(item, str) for item in val)


def _split_plugin_fields(
    raw: dict,
    field_checks: dict[str, Callable[[object], bool]],
) -> tuple[dict[str, object], dict[str, object]]:
    known: dict[str, object] = {"kind": raw.get("kind")}
    options: dict[str, object] = {}
    for k, v in raw.items():
        if k == "kind":
            continue
        check = field_checks.get(k)
        if check is not None and check(v):
            known[k] = v
        else:
            options[k] = v
    return known, options


def _build_transport_cfg(name: str, raw: dict) -> TransportConfig:
    kind = raw.get("kind", "local")
    prefix = f"providers.{name}.transport"
    if kind in BUILTIN_TRANSPORT_KINDS:
        transport = TransportConfig(**raw)
    else:
        known, options = _split_plugin_fields(
            raw,
            {
                "host": lambda v: isinstance(v, str),
                "workdir": lambda v: isinstance(v, str),
                "ssh_opts": lambda v: isinstance(v, str) or _is_str_list(v),
            },
        )
        transport = TransportConfig(**known, options=options)  # type: ignore[arg-type]

    if transport.host is not None:
        transport.host = _expand(f"{prefix}.host", transport.host)
    if transport.workdir is not None:
        transport.workdir = _expand(f"{prefix}.workdir", transport.workdir)
    if isinstance(transport.ssh_opts, str):
        transport.ssh_opts = shlex.split(_expand(f"{prefix}.ssh_opts", transport.ssh_opts))
    else:
        transport.ssh_opts = [_expand(f"{prefix}.ssh_opts", v) for v in transport.ssh_opts]
    return transport


def _build_launcher_cfg(raw: dict) -> LauncherConfig:
    kind = raw.get("kind", "make")
    if kind in BUILTIN_LAUNCHER_KINDS:
        return LauncherConfig(**raw)
    known, options = _split_plugin_fields(
        raw,
        {
            "queue": lambda v: isinstance(v, str),
            "resources": lambda v: isinstance(v, str),
            "extra": _is_str_list,
        },
    )
    return LauncherConfig(**known, options=options)  # type: ignore[arg-type]


def _build_sync_cfg(raw: dict) -> SyncConfig:
    kind = raw.get("kind", "none")
    if kind in BUILTIN_SYNC_KINDS:
        return SyncConfig(**raw)
    known, options = _split_plugin_fields(
        raw,
        {
            "remote": lambda v: isinstance(v, str),
            "branch": lambda v: isinstance(v, str),
            "delete": lambda v: isinstance(v, bool),
            "exclude": _is_str_list,
        },
    )
    return SyncConfig(**known, options=options)  # type: ignore[arg-type]


def _build_providers(data: dict) -> dict[str, ProviderConfig]:
    providers: dict[str, ProviderConfig] = {}
    for name, value in data.items():
        providers[name] = ProviderConfig(
            name=name,
            transport=_build_transport_cfg(name, value.get("transport", {})),
            launcher=_build_launcher_cfg(value.get("launcher", {})),
            sync=_build_sync_cfg(value.get("sync", {})),
            setup=value.get("setup", ""),
        )
    # 内置的 local provider 永远存在（config.md §2.8）
    providers.setdefault("local", ProviderConfig(name="local"))
    return providers
