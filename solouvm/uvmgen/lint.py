"""UVM style linter enforcing ``doc/uvm-style.md`` rules (U-08)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.errors import ConfigError
from solouvm.uvmgen.pkgfiles import find_svh_inconsistencies
from solouvm.uvmgen.regions import compute_checksum, parse_header

if TYPE_CHECKING:
    from solouvm.collect.files import FileList

_LOWER_NAME_RE = re.compile(r"[a-z][a-z0-9_]*\.(?:sv|svh)\Z", re.ASCII)
_CLASS_RE = re.compile(r"\bclass\s+(?:automatic\s+|static\s+)?([A-Za-z_]\w*)\b")
_SV_TOKEN_RE = re.compile(
    r'(?P<str>"(?:\\.|[^"\\])*")'
    r"|(?P<line>//[^\n]*)"
    r"|(?P<block>/\*.*?\*/)",
    re.DOTALL,
)
_DISPLAY_RE = re.compile(r"\$display\b")
_FINISH_RE = re.compile(r"\$finish\b")
_BARE_DELAY_RE = re.compile(r"(?<!\w)#\s*\d+(?:\.\d+)?(?:\s*(?:fs|ps|ns|us|ms|s|step))?\b")
_DIRECT_NEW_RE = re.compile(r"\b([A-Za-z_]\w*)::new\s*\(")
_VIF_SET_RE = re.compile(r'\buvm_config_db\b[^;]*?\bset\s*\([^;]*?"vif"', re.DOTALL)
_UVM_MACRO_RE = re.compile(r"`(uvm_(?:info|warning|error|fatal))\s*\(")
_MSG_ID_RE = re.compile(r"[A-Z][A-Z0-9_]*(?:\.[A-Z][A-Z0-9_]*)*\Z", re.ASCII)
_SVH_PROBLEM_RE = re.compile(r"^(?P<file>[^:]+):(?P<line>\d+):\s*(?P<msg>.+)$")

_AGENT_SUFFIXES = (
    "_if.sv",
    "_pkg.sv",
    "_files.svh",
    "_txn.sv",
    "_cfg.sv",
    "_drv.sv",
    "_mon.sv",
    "_sqr.sv",
    "_cov.sv",
    "_agent.sv",
    "_seq.sv",
)
_ENV_SUFFIXES = (
    "_env_pkg.sv",
    "_env_files.svh",
    "_env_cfg.sv",
    "_scb.sv",
    "_ref.sv",
    "_vsqr.sv",
    "_cov.sv",
    "_env.sv",
    "_vseq.sv",
)
_TEST_SUFFIXES = (
    "_test_pkg.sv",
    "_test_files.svh",
    "_test.sv",
)

__all__ = ["StyleIssue", "lint_style"]


@dataclass(frozen=True)
class StyleIssue:
    """Single style violation reported by :func:`lint_style`."""

    file: str
    line: int
    severity: str  # "error" or "warning"
    message: str
    rule: str = ""

    def format(self) -> str:
        return f"{self.file}:{self.line}: {self.severity}: {self.message}"

    def __str__(self) -> str:
        return self.format()


def _blank_preserving_newlines(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def _strip_comments(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        if match.group("str") is not None:
            return match.group(0)
        return _blank_preserving_newlines(match.group(0))

    return _SV_TOKEN_RE.sub(repl, text)


def _strip_comments_and_strings(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        if match.group("str") is not None:
            raw = match.group(0)
            if "\n" not in raw:
                return '""'
            return '"' + _blank_preserving_newlines(raw[1:-1]) + '"'
        return _blank_preserving_newlines(match.group(0))

    return _SV_TOKEN_RE.sub(repl, text)


def _line_at(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _split_macro_args(text: str, open_paren_end: int) -> list[str]:
    """Extract top-level comma-separated arguments starting just after '('."""
    depth = 1
    in_str = False
    escaped = False
    args: list[str] = []
    cur: list[str] = []
    idx = open_paren_end
    length = len(text)
    while idx < length and depth > 0:
        ch = text[idx]
        if in_str:
            cur.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
                cur.append(ch)
            elif ch == "(":
                depth += 1
                cur.append(ch)
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    break
                cur.append(ch)
            elif ch == "," and depth == 1:
                args.append("".join(cur).strip())
                cur = []
                idx += 1
                continue
            else:
                cur.append(ch)
        idx += 1
    tail = "".join(cur).strip()
    if tail or args:
        args.append(tail)
    return args


def _check_naming(rel: Path, code_no_str: str) -> list[StyleIssue]:
    issues: list[StyleIssue] = []
    rel_str = rel.as_posix()
    name = rel.name
    if not _LOWER_NAME_RE.fullmatch(name):
        issues.append(
            StyleIssue(
                file=rel_str,
                line=1,
                severity="error",
                message=f"file name {name!r} must be lowercase snake_case ending in .sv or .svh",
                rule="naming",
            )
        )

    parts = rel.parts
    if len(parts) >= 4 and parts[0] == "verif":
        group, comp = parts[1], parts[2]
        if group == "agents":
            if not (name.startswith(f"{comp}_") and name.endswith(_AGENT_SUFFIXES)):
                issues.append(
                    StyleIssue(
                        file=rel_str,
                        line=1,
                        severity="warning",
                        message=f"agent file {name!r} does not follow <agent>_<suffix> naming",
                        rule="naming",
                    )
                )
            elif name.endswith("_seq.sv") and (len(parts) < 5 or parts[3] != "seq"):
                issues.append(
                    StyleIssue(
                        file=rel_str,
                        line=1,
                        severity="warning",
                        message=f"sequence file {name!r} should live under seq/",
                        rule="naming",
                    )
                )
        elif group == "envs":
            if not (name.startswith(f"{comp}_") and name.endswith(_ENV_SUFFIXES)):
                issues.append(
                    StyleIssue(
                        file=rel_str,
                        line=1,
                        severity="warning",
                        message=f"env file {name!r} does not follow <env>_<suffix> naming",
                        rule="naming",
                    )
                )
            elif name.endswith("_vseq.sv") and (len(parts) < 5 or parts[3] != "vseq"):
                issues.append(
                    StyleIssue(
                        file=rel_str,
                        line=1,
                        severity="warning",
                        message=f"virtual sequence file {name!r} should live under vseq/",
                        rule="naming",
                    )
                )
        elif group == "tests" and not (
            name.startswith(f"{comp}_") and name.endswith(_TEST_SUFFIXES)
        ):
            issues.append(
                StyleIssue(
                    file=rel_str,
                    line=1,
                    severity="warning",
                    message=f"test file {name!r} does not follow <env>_<suffix> naming",
                    rule="naming",
                )
            )

    if (
        rel.suffix == ".sv"
        and not name.endswith(("_pkg.sv", "_if.sv"))
        and parts[:2] != ("verif", "tb")
    ):
        for match in _CLASS_RE.finditer(code_no_str):
            cls_name = match.group(1)
            if cls_name != rel.stem:
                issues.append(
                    StyleIssue(
                        file=rel_str,
                        line=_line_at(code_no_str, match.start()),
                        severity="error",
                        message=f"class name {cls_name!r} does not match file name {rel.name!r}",
                        rule="naming",
                    )
                )
    return issues


def _expected_id_prefix(rel: Path) -> str | None:
    parts = rel.parts
    if len(parts) >= 3 and parts[0] == "verif" and parts[1] in ("agents", "envs", "tests"):
        if rel.name.endswith(("_if.sv", "_pkg.sv")) or rel.suffix != ".sv":
            return None
        return rel.stem.upper()
    return None


def _check_sv_content(rel: Path, text: str, *, is_tb_scope: bool) -> list[StyleIssue]:
    issues: list[StyleIssue] = []
    rel_str = rel.as_posix()
    code_with_str = _strip_comments(text)
    code_no_str = _strip_comments_and_strings(text)

    issues.extend(_check_naming(rel, code_no_str))

    if rel.suffix == ".sv":
        header = parse_header(text)
        if header is not None:
            try:
                actual_cs = compute_checksum(text)
            except ConfigError as exc:
                issues.append(
                    StyleIssue(
                        file=rel_str,
                        line=1,
                        severity="error",
                        message=exc.message,
                        rule="regions",
                    )
                )
            else:
                if actual_cs.lower() != header[3].lower():
                    issues.append(
                        StyleIssue(
                            file=rel_str,
                            line=1,
                            severity="warning",
                            message=(
                                "file modified outside solouvm:user regions "
                                f"(checksum mismatch: expected {header[3]}, got {actual_cs})"
                            ),
                            rule="checksum",
                        )
                    )

    for match in _DISPLAY_RE.finditer(code_no_str):
        issues.append(
            StyleIssue(
                file=rel_str,
                line=_line_at(code_no_str, match.start()),
                severity="error",
                message="forbidden '$display' (use UVM reporting macros)",
                rule="forbidden",
            )
        )
    for match in _FINISH_RE.finditer(code_no_str):
        issues.append(
            StyleIssue(
                file=rel_str,
                line=_line_at(code_no_str, match.start()),
                severity="error",
                message="forbidden '$finish' (use UVM objections)",
                rule="forbidden",
            )
        )
    if is_tb_scope:
        for match in _BARE_DELAY_RE.finditer(code_no_str):
            issues.append(
                StyleIssue(
                    file=rel_str,
                    line=_line_at(code_no_str, match.start()),
                    severity="error",
                    message=(
                        f"forbidden bare delay {match.group(0)!r} in tb (use clocking blocks)"
                    ),
                    rule="forbidden",
                )
            )
        for match in _VIF_SET_RE.finditer(code_with_str):
            issues.append(
                StyleIssue(
                    file=rel_str,
                    line=_line_at(code_with_str, match.start()),
                    severity="error",
                    message='uvm_config_db::set(..., "vif", ...) is only allowed in verif/tb/',
                    rule="config_db",
                )
            )

    for match in _DIRECT_NEW_RE.finditer(code_no_str):
        issues.append(
            StyleIssue(
                file=rel_str,
                line=_line_at(code_no_str, match.start()),
                severity="error",
                message=f"direct '{match.group(1)}::new()' used instead of type_id::create",
                rule="factory",
            )
        )

    expected_prefix = _expected_id_prefix(rel)
    for match in _UVM_MACRO_RE.finditer(code_with_str):
        macro = match.group(1)
        lineno = _line_at(code_with_str, match.start())
        args = _split_macro_args(code_with_str, match.end())
        required_args = 3 if macro == "uvm_info" else 2
        if not args or not args[0] or len(args) < required_args:
            issues.append(
                StyleIssue(
                    file=rel_str,
                    line=lineno,
                    severity="error",
                    message=f"missing message ID in `{macro}",
                    rule="msg_id",
                )
            )
            continue
        raw_id = args[0]
        if not (len(raw_id) >= 2 and raw_id.startswith('"') and raw_id.endswith('"')):
            issues.append(
                StyleIssue(
                    file=rel_str,
                    line=lineno,
                    severity="warning",
                    message=f"UVM message ID {raw_id!r} in `{macro} should be a string literal",
                    rule="msg_id",
                )
            )
            continue
        msg_id = raw_id[1:-1]
        if not msg_id.strip():
            issues.append(
                StyleIssue(
                    file=rel_str,
                    line=lineno,
                    severity="error",
                    message=f"empty message ID in `{macro}",
                    rule="msg_id",
                )
            )
        elif not _MSG_ID_RE.fullmatch(msg_id):
            issues.append(
                StyleIssue(
                    file=rel_str,
                    line=lineno,
                    severity="error",
                    message=(
                        f"invalid UVM message ID {msg_id!r} in `{macro} "
                        "(expected UPPERCASE or UPPERCASE.SUB)"
                    ),
                    rule="msg_id",
                )
            )
        elif expected_prefix is not None and not (
            msg_id == expected_prefix or msg_id.startswith(f"{expected_prefix}.")
        ):
            issues.append(
                StyleIssue(
                    file=rel_str,
                    line=lineno,
                    severity="error",
                    message=(
                        f"UVM message ID {msg_id!r} in `{macro} does not match "
                        f"component prefix {expected_prefix!r}"
                    ),
                    rule="msg_id",
                )
            )

    return issues


def lint_style(root: Path, *, files: FileList | None = None) -> list[StyleIssue]:
    """Lint UVM files under ``root`` against ``doc/uvm-style.md`` rules."""
    root_path = Path(root)
    verif_dir = root_path / "verif"

    candidates: dict[Path, bool] = {}  # rel_path -> is_tb_scope
    if verif_dir.is_dir():
        for path in sorted(verif_dir.rglob("*"), key=lambda p: p.as_posix()):
            if not path.is_file() or path.suffix not in (".sv", ".svh"):
                continue
            rel = path.relative_to(root_path)
            is_tb = rel.parts[:2] != ("verif", "tb")
            candidates[rel] = is_tb

    if files is not None:
        for group_name, is_tb in (("tb", True), ("top", False)):
            for raw in files.groups.get(group_name, []):
                candidate = Path(raw)
                if candidate.is_absolute():
                    try:
                        rel = candidate.resolve().relative_to(root_path.resolve())
                    except ValueError:
                        continue
                else:
                    rel = candidate
                if rel.suffix in (".sv", ".svh") and (root_path / rel).is_file():
                    candidates.setdefault(rel, is_tb)

    issues: list[StyleIssue] = []
    for rel, is_tb in sorted(candidates.items(), key=lambda item: item[0].as_posix()):
        text = (root_path / rel).read_text(encoding="utf-8")
        issues.extend(_check_sv_content(rel, text, is_tb_scope=is_tb))

    for problem in find_svh_inconsistencies(root_path):
        match = _SVH_PROBLEM_RE.match(problem)
        if match is not None:
            issues.append(
                StyleIssue(
                    file=match.group("file"),
                    line=int(match.group("line")),
                    severity="error",
                    message=match.group("msg"),
                    rule="svh",
                )
            )
        else:
            file_part, _, msg_part = problem.partition(": ")
            issues.append(
                StyleIssue(
                    file=file_part,
                    line=1,
                    severity="error",
                    message=msg_part or problem,
                    rule="svh",
                )
            )

    issues.sort(key=lambda item: (item.file, item.line, item.severity, item.message))
    return issues
