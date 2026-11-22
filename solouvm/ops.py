"""Operational helpers: validate, config view, schema, doctor and help (M1-M4)."""

from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import sys
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import asdict, dataclass, is_dataclass
from difflib import get_close_matches
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.backends import resolve_sim_backend
from solouvm.buildkey import strip_sv_comments
from solouvm.collect.cases import (
    CaseSource,
    _case_files,
    _load_file,
)
from solouvm.collect.files import (
    GROUPS,
    PACKAGE_GROUPS,
    FileList,
    load_project_filelists,
)
from solouvm.config.load import expand_env, load_config
from solouvm.errors import ERROR_CODES, ConfigError, SolouvmError
from solouvm.gen.shell import sh_quote
from solouvm.model import validate_records
from solouvm.packages import (
    ResolvedPackage,
    check_host_depends,
    collect_from_resolved,
    resolve_packages,
)
from solouvm.uvmgen.pkgfiles import find_svh_inconsistencies

if TYPE_CHECKING:
    from solouvm.backends.base import Backend
    from solouvm.config.schema import Config
    from solouvm.providers.base import Provider

__all__ = [
    "HELP_TOPICS",
    "SCHEMAS",
    "DoctorCheck",
    "ValidationIssue",
    "ValidationReport",
    "format_doctor",
    "format_show_config",
    "get_schema",
    "run_doctor",
    "show_config",
    "show_help",
    "validate_project",
]

_BASH_DEFAULT_ENV_RE = re.compile(r"\$\{\w+:-[^}]*\}")
_SV_DECL_RE = re.compile(
    r"^\s*(?:module|package)\s+(?:automatic\s+|static\s+)?([A-Za-z_][A-Za-z0-9_$]*)\b",
    re.MULTILINE,
)
_SVH_LOC_RE = re.compile(r"^(.+?):(\d+):\s*(.+)$")
_SVH_NOLINE_RE = re.compile(r"^(.+?):\s*(.+)$")


@dataclass(frozen=True)
class ValidationIssue:
    """Single error or warning reported by ``solouvm validate``."""

    file: str | None
    line: int | None
    severity: str
    message: str
    code: str | None = None
    req: str | None = None
    hint: str | None = None

    def format_line(self) -> str:
        """Format issue as ``file:line: severity: message``."""
        prefix = ""
        if self.file:
            prefix = f"{self.file}:{self.line}: " if self.line is not None else f"{self.file}: "
        return f"{prefix}{self.severity}: {self.message}"

    def to_dict(self) -> dict[str, object]:
        return {
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "message": self.message,
            "code": self.code,
            "req": self.req,
            "hint": self.hint,
        }


@dataclass
class ValidationReport:
    """Aggregated validation results from ``validate_project``."""

    issues: list[ValidationIssue]

    @property
    def errors(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [issue for issue in self.issues if issue.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def format_text(self) -> str:
        lines = [issue.format_line() for issue in self.issues]
        if lines:
            lines.append("")
        n_err = len(self.errors)
        n_warn = len(self.warnings)
        err_word = "error" if n_err == 1 else "errors"
        warn_word = "warning" if n_warn == 1 else "warnings"
        lines.append(f"{n_err} {err_word}, {n_warn} {warn_word}")
        return "\n".join(lines)


def _rel_display(root: Path, raw_file: str | Path | None) -> str | None:
    if raw_file is None:
        return None
    path = Path(raw_file)
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return str(raw_file)


def _check_dotf_defaults(
    root: Path,
    raw_spec: dict | list,
    issues: list[ValidationIssue],
    *,
    base_dir: Path | None = None,
    file_label: str = "filelist.py",
    allowed_groups: tuple[str, ...] = GROUPS,
) -> None:
    """Warn/error on ``${VAR:-default}`` inside filelist entries or imported ``.f`` files."""
    eff_base = Path(base_dir) if base_dir is not None else root
    if isinstance(raw_spec, (list, tuple)):
        groups = {"rtl": list(raw_spec)} if "rtl" in allowed_groups else {}
    elif isinstance(raw_spec, dict):
        groups = {
            g: list(raw_spec.get(g, []))
            for g in allowed_groups
            if isinstance(raw_spec.get(g), Sequence)
        }
    else:
        return

    seen_entries: set[str] = set()
    visited_dotf: set[Path] = set()

    def scan_dotf(dotf_path: Path) -> None:
        resolved = dotf_path.resolve()
        if resolved in visited_dotf or not dotf_path.is_file():
            return
        visited_dotf.add(resolved)
        rel = _rel_display(root, dotf_path) or str(dotf_path)
        for lineno, line in enumerate(
            dotf_path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
        ):
            clean = line.split("//", 1)[0].split("#", 1)[0].strip()
            if not clean:
                continue
            if _BASH_DEFAULT_ENV_RE.search(clean):
                issues.append(
                    ValidationIssue(
                        file=rel,
                        line=lineno,
                        severity="error",
                        message=(
                            "default-value syntax '${VAR:-default}' is not allowed in .f entries "
                            "(use '${VAR}' for simulator expansion; config.md §2.11)"
                        ),
                        code="CFG_INVALID",
                        req="C-03",
                    )
                )
            tokens = clean.split()
            idx = 0
            while idx < len(tokens):
                tok = tokens[idx]
                if tok in ("-f", "-F") and idx + 1 < len(tokens):
                    nxt = tokens[idx + 1]
                    base = dotf_path.parent if tok == "-F" else eff_base
                    candidate = Path(nxt) if Path(nxt).is_absolute() else base / nxt
                    scan_dotf(candidate)
                    idx += 2
                else:
                    idx += 1

    for group_entries in groups.values():
        for entry in group_entries:
            if not isinstance(entry, str):
                continue
            if _BASH_DEFAULT_ENV_RE.search(entry):
                issues.append(
                    ValidationIssue(
                        file=file_label,
                        line=None,
                        severity="error",
                        message=(
                            f"default-value syntax '${{VAR:-default}}' in {entry!r} is not "
                            "allowed in filelists (use '${{VAR}}' only; config.md §2.11)"
                        ),
                        code="CFG_INVALID",
                        req="C-03",
                    )
                )
            norm_entry = entry.strip()
            if norm_entry and not norm_entry.startswith(("+", "-")) and "*" not in norm_entry:
                if norm_entry in seen_entries:
                    issues.append(
                        ValidationIssue(
                            file=file_label,
                            line=None,
                            severity="warning",
                            message=f"duplicate filelist entry {entry!r} (F-08)",
                            req="F-08",
                        )
                    )
                seen_entries.add(norm_entry)
            try:
                tokens = shlex.split(entry)
            except ValueError:
                continue
            idx = 0
            while idx < len(tokens):
                if tokens[idx] in ("-f", "-F") and idx + 1 < len(tokens):
                    sub = Path(tokens[idx + 1])
                    if not sub.is_absolute():
                        sub = eff_base / sub
                    scan_dotf(sub)
                    idx += 2
                else:
                    idx += 1


def _check_duplicate_sv_modules(
    root: Path, file_list: FileList, issues: list[ValidationIssue]
) -> None:
    """Detect duplicate SV module or package declarations across collected files (F-08)."""
    defs: dict[str, list[str]] = defaultdict(list)
    for rel_path in file_list.files:
        if "$" in rel_path:
            continue
        candidate = Path(rel_path) if Path(rel_path).is_absolute() else root / rel_path
        if not candidate.is_file():
            continue
        text = candidate.read_text(encoding="utf-8", errors="replace")
        stripped = strip_sv_comments(text, blank_strings=True)
        for match in _SV_DECL_RE.finditer(stripped):
            name = match.group(1)
            if rel_path not in defs[name]:
                defs[name].append(rel_path)

    for name in sorted(defs):
        locations = defs[name]
        if len(locations) > 1:
            issues.append(
                ValidationIssue(
                    file=locations[1],
                    line=None,
                    severity="warning",
                    message=(
                        f"duplicate module/package '{name}' defined in multiple files: "
                        + ", ".join(locations)
                        + " (F-08)"
                    ),
                    req="F-08",
                )
            )


def _collect_cases_resilient(config: Config, issues: list[ValidationIssue]) -> list[CaseSource]:
    """Collect cases across all files without aborting on the first broken case file."""
    root = Path(config.root)
    try:
        case_files = _case_files(config)
    except ConfigError as exc:
        issues.append(
            ValidationIssue(
                file=_rel_display(root, exc.file),
                line=exc.line,
                severity="error",
                message=exc.message,
                code=exc.code,
                hint=exc.hint,
            )
        )
        return []

    seen_names: dict[str, tuple[str | None, int | None]] = {}
    collected: list[CaseSource] = []

    for path in case_files:
        rel_path = _rel_display(root, path) or str(path)
        try:
            items = _load_file(path, root)
        except SolouvmError as exc:
            issues.append(
                ValidationIssue(
                    file=_rel_display(root, exc.file) or rel_path,
                    line=exc.line,
                    severity="error",
                    message=exc.message,
                    code=exc.code,
                    hint=exc.hint,
                )
            )
            continue

        for cs in items:
            case = cs.case
            line = cs.line
            if case.name in seen_names:
                prev_file, prev_line = seen_names[case.name]
                where = f"{prev_file}:{prev_line}" if prev_line else str(prev_file)
                issues.append(
                    ValidationIssue(
                        file=rel_path,
                        line=line,
                        severity="error",
                        message=(
                            f"duplicate case name {case.name!r} (previously defined at {where})"
                        ),
                        code="CASE_INVALID",
                    )
                )
                continue
            seen_names[case.name] = (rel_path, line)

            if case.build not in config.builds:
                issues.append(
                    ValidationIssue(
                        file=rel_path,
                        line=line,
                        severity="error",
                        message=(
                            f"Case {case.name!r} references unknown build mode {case.build!r} "
                            f"(defined: {', '.join(sorted(config.builds))})"
                        ),
                        code="CASE_INVALID",
                    )
                )
            collected.append(cs)

    return collected


def validate_project(
    root_or_conf: Path, *, cli: dict[str, object] | None = None
) -> ValidationReport:
    """Validate configuration, file lists, cases, records and SVH consistency (Z-10)."""
    issues: list[ValidationIssue] = []
    target = Path(root_or_conf).expanduser().absolute()
    base_root = target.parent if target.is_file() else target

    try:
        config = load_config(target, cli=cli)
    except SolouvmError as exc:
        issues.append(
            ValidationIssue(
                file=_rel_display(base_root, exc.file),
                line=exc.line,
                severity="error",
                message=exc.message,
                code=exc.code,
                hint=exc.hint,
            )
        )
        return ValidationReport(issues=issues)

    root = Path(config.root)
    pkgs: list[ResolvedPackage] = []
    pkg_error = False
    if config.packages:
        try:
            pkgs = resolve_packages(config, auto_fetch=False)
        except SolouvmError as exc:
            pkg_error = True
            issues.append(
                ValidationIssue(
                    file=_rel_display(root, exc.file),
                    line=exc.line,
                    severity="error",
                    message=exc.message,
                    code=exc.code,
                    hint=exc.hint,
                )
            )

    try:
        project_fls = load_project_filelists(root, config.filelist_dirs)
    except SolouvmError as exc:
        issues.append(
            ValidationIssue(
                file=_rel_display(root, exc.file) or "filelist.py",
                line=exc.line,
                severity="error",
                message=exc.message,
                code=exc.code,
                hint=exc.hint,
            )
        )
    else:
        for pkg in pkgs:
            _check_dotf_defaults(
                root,
                pkg.filelist.spec,
                issues,
                base_dir=pkg.root_dir,
                file_label=_rel_display(root, pkg.filelist.path) or str(pkg.filelist.path),
                allowed_groups=PACKAGE_GROUPS,
            )
        for pfl in project_fls:
            fl_label = "filelist.py" if pfl.rel_dir == "." else f"{pfl.rel_dir}/filelist.py"
            if not pkg_error:
                try:
                    check_host_depends(pfl.meta, {pkg.name for pkg in pkgs})
                except SolouvmError as exc:
                    issues.append(
                        ValidationIssue(
                            file=_rel_display(root, exc.file) or fl_label,
                            line=exc.line,
                            severity="error",
                            message=exc.message,
                            code=exc.code,
                            hint=exc.hint,
                        )
                    )
            _check_dotf_defaults(
                root,
                pfl.meta.spec,
                issues,
                base_dir=pfl.root_dir,
                file_label=fl_label,
            )
        if not pkg_error:
            try:
                file_list = collect_from_resolved(root, pkgs, project_fls)
            except SolouvmError as exc:
                issues.append(
                    ValidationIssue(
                        file=_rel_display(root, exc.file) or "filelist.py",
                        line=exc.line,
                        severity="error",
                        message=exc.message,
                        code=exc.code,
                        hint=exc.hint,
                    )
                )
            else:
                _check_duplicate_sv_modules(root, file_list, issues)

    for cs in _collect_cases_resilient(config, issues):
        rel = _rel_display(root, cs.file)
        case = cs.case
        try:
            validate_records(case)
        except SolouvmError as exc:
            issues.append(
                ValidationIssue(
                    file=rel,
                    line=cs.line,
                    severity="error",
                    message=exc.message,
                    code=exc.code,
                    hint=exc.hint,
                )
            )

        if case.stage in ("ready", "signoff"):
            missing: list[str] = []
            if not str(case.intent).strip():
                missing.append("intent")
            if not str(case.method).strip():
                missing.append("method")
            if not case.spec_refs:
                missing.append("spec_refs")
            if not case.expect:
                missing.append("expect")
            if missing:
                issues.append(
                    ValidationIssue(
                        file=rel,
                        line=cs.line,
                        severity="error",
                        message=(
                            f"Case {case.name!r} is stage {case.stage!r} but missing "
                            + ", ".join(repr(m) for m in missing)
                            + " (AI-02)"
                        ),
                        code="CASE_INVALID",
                        req="AI-02",
                    )
                )
            if case.expect_fail is True:
                issues.append(
                    ValidationIssue(
                        file=rel,
                        line=cs.line,
                        severity="warning",
                        message=(
                            f"Case {case.name!r} uses expect_fail=True; name the expected error "
                            "(J-20)"
                        ),
                        req="J-20",
                    )
                )

    for msg in find_svh_inconsistencies(root):
        m_loc = _SVH_LOC_RE.match(msg)
        if m_loc:
            issues.append(
                ValidationIssue(
                    file=m_loc.group(1),
                    line=int(m_loc.group(2)),
                    severity="error",
                    message=f"{m_loc.group(3)} (U-11)",
                    code="CASE_INVALID",
                    req="U-11",
                )
            )
            continue
        m_nol = _SVH_NOLINE_RE.match(msg)
        if m_nol:
            issues.append(
                ValidationIssue(
                    file=m_nol.group(1),
                    line=None,
                    severity="error",
                    message=f"{m_nol.group(2)} (U-11)",
                    code="CASE_INVALID",
                    req="U-11",
                )
            )
        else:
            issues.append(
                ValidationIssue(
                    file=None,
                    line=None,
                    severity="error",
                    message=f"{msg} (U-11)",
                    code="CASE_INVALID",
                    req="U-11",
                )
            )

    return ValidationReport(issues=issues)


# ---------------------------------------------------------------- config (C-08)


def _serialize_config_value(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {k: _serialize_config_value(v) for k, v in asdict(value).items()}
    if isinstance(value, tuple):
        return [_serialize_config_value(v) for v in value]
    if isinstance(value, list):
        return [_serialize_config_value(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _serialize_config_value(v) for k, v in value.items()}
    return value


def _flatten_config(obj: object, prefix: str = "") -> list[tuple[str, object]]:
    """Flatten config dataclasses/dicts to ``(dot_key, serialized_value)`` pairs."""
    if is_dataclass(obj) and not isinstance(obj, type):
        raw_dict = {f.name: getattr(obj, f.name) for f in obj.__dataclass_fields__.values()}
    elif isinstance(obj, dict):
        raw_dict = obj
    else:
        return [(prefix, _serialize_config_value(obj))]

    if not raw_dict and prefix:
        return [(prefix, {})]

    out: list[tuple[str, object]] = []
    for k, v in raw_dict.items():
        if prefix == "" and k in ("root", "sources"):
            continue
        sub_key = f"{prefix}.{k}" if prefix else str(k)
        if (is_dataclass(v) and not isinstance(v, type)) or (isinstance(v, dict) and v):
            out.extend(_flatten_config(v, sub_key))
        else:
            out.append((sub_key, _serialize_config_value(v)))
    return out


def _lookup_source(sources: dict[str, str], dot_key: str) -> str:
    if dot_key in sources:
        return sources[dot_key]
    cur = dot_key
    while "." in cur:
        cur = cur.rsplit(".", 1)[0]
        if cur in sources:
            return sources[cur]
    return "default"


def show_config(config: Config, key: str | None = None) -> list[dict[str, object]]:
    """Return flattened config entries with their resolved ``source`` (C-08)."""
    entries: list[dict[str, object]] = []
    for dot_key, val in _flatten_config(config):
        entries.append(
            {
                "key": dot_key,
                "value": val,
                "source": _lookup_source(config.sources, dot_key),
            }
        )

    if key is None:
        return entries

    matched = [
        item for item in entries if item["key"] == key or str(item["key"]).startswith(f"{key}.")
    ]
    if not matched:
        all_keys = [str(item["key"]) for item in entries]
        close = get_close_matches(key, all_keys, n=1)
        hint = f"did you mean {close[0]!r}?" if close else None
        raise ConfigError(f"unknown config key {key!r}", code="CFG_UNKNOWN_KEY", hint=hint)
    return matched


def format_show_config(entries: Sequence[dict[str, object]]) -> str:
    """Format config entries into a readable aligned text block."""
    if not entries:
        return "(no config entries)"
    key_w = max(len(str(e["key"])) for e in entries)
    labels = [f"[{e['source']}]" for e in entries]
    src_w = max(len(lbl) for lbl in labels)
    lines = [
        f"{e['key']!s:<{key_w}}  {lbl:<{src_w}}  {e['value']!r}"
        for e, lbl in zip(entries, labels, strict=True)
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------- schema (Z-11)

_SCHEMA_DESCRIPTIONS: dict[str, str] = {
    "config": "Merged project configuration (solouvm.conf.py / solouvm.local.py)",
    "case": "Test case and Suite definitions (case.py / cases/*.py)",
    "records": "Case note records (Bug, Todo, Log, Note, Triage)",
    "plan": "Generated regression plan (.solouvm/runs/<run_id>/plan.json)",
    "results": "Judged run results (.solouvm/runs/<run_id>/results.json)",
    "facts": "B-side execution facts file (job.facts)",
    "packet": "Triage context packet and tasks.json structure",
    "output": "CLI --json response and error envelope (formats.md §9)",
}

SCHEMAS: dict[str, dict[str, object]] = {
    "config": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "solouvm.config",
        "type": "object",
        "required": ["simulator", "top"],
        "properties": {
            "project": {"type": ["string", "null"]},
            "simulator": {"type": "string"},
            "top": {"type": "string"},
            "uvm_test": {"type": ["string", "null"]},
            "incdirs": {"type": "array", "items": {"type": "string"}},
            "defines": {"type": "array", "items": {"type": "string"}},
            "builds": {"type": "object"},
            "regress": {"type": "object"},
            "run": {"type": "object"},
            "checks": {"type": "object"},
            "job": {"type": "object"},
            "sim": {"type": "object"},
            "providers": {"type": "object"},
            "provider": {"type": "string"},
            "check": {"type": "object"},
            "case_dirs": {"type": "array", "items": {"type": "string"}},
            "filelist_dirs": {"type": "array", "items": {"type": "string"}},
            "system_doc": {"type": "array", "items": {"type": "string"}},
            "triage": {"type": "object"},
            "doc": {"type": "object"},
            "history": {"type": "object"},
            "limits": {"type": "object"},
            "plugins": {"type": "array", "items": {"type": "string"}},
            "packages": {"type": "object"},
            "uvm": {"type": "object"},
        },
    },
    "case": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "solouvm.case",
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string", "pattern": "^(?!.*__)[A-Za-z0-9_.-]+$"},
            "uvm_test": {"type": ["string", "null"]},
            "params": {"type": "object"},
            "opts": {"type": "array", "items": {"type": "string"}},
            "env": {"type": "object"},
            "build": {"type": "string"},
            "seeds": {"type": ["string", "array", "null"]},
            "timeout": {"type": ["string", "null"]},
            "hang": {"type": ["string", "null"]},
            "retry": {"type": ["integer", "null"], "minimum": 0},
            "checks": {"type": "object"},
            "expect_fail": {"type": ["boolean", "string"]},
            "tags": {"type": "array", "items": {"type": "string"}},
            "owner": {"type": ["string", "null"]},
            "stage": {"enum": ["planned", "wip", "ready", "signoff", "deprecated"]},
            "intent": {"type": "string"},
            "method": {"type": "string"},
            "spec_refs": {"type": "array", "items": {"type": "string"}},
            "expect": {"type": "array", "items": {"type": "object"}},
            "doc": {"type": "string"},
            "notes": {"type": "array"},
        },
    },
    "records": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "solouvm.records",
        "oneOf": [
            {
                "title": "Bug",
                "type": "object",
                "required": ["id", "title", "status"],
                "properties": {
                    "id": {"type": "integer", "minimum": 1},
                    "title": {"type": "string"},
                    "status": {
                        "enum": ["open", "fixed", "verified", "wontfix", "invalid", "duplicate"]
                    },
                    "found": {"type": ["string", "null"], "format": "date"},
                    "fixed": {"type": ["string", "null"], "format": "date"},
                    "verified": {"type": ["string", "null"], "format": "date"},
                    "seed": {"type": ["integer", "null"]},
                    "signature": {"type": ["string", "null"]},
                    "run": {"type": ["string", "null"]},
                    "job": {"type": ["string", "null"]},
                    "reporter": {"type": ["string", "null"]},
                    "owner": {"type": ["string", "null"]},
                    "link": {"type": ["string", "null"]},
                    "body": {"type": "string"},
                    "author": {"type": ["string", "null"]},
                    "at": {"type": ["string", "null"]},
                },
            },
            {
                "title": "Todo",
                "type": "object",
                "required": ["title"],
                "properties": {
                    "id": {"type": ["integer", "null"]},
                    "title": {"type": "string"},
                    "owner": {"type": ["string", "null"]},
                    "due": {"type": ["string", "null"], "format": "date"},
                    "done": {"type": "boolean"},
                    "author": {"type": ["string", "null"]},
                    "at": {"type": ["string", "null"]},
                },
            },
            {
                "title": "Log",
                "type": "object",
                "required": ["date", "text"],
                "properties": {
                    "date": {"type": "string", "format": "date"},
                    "text": {"type": "string"},
                    "author": {"type": ["string", "null"]},
                    "at": {"type": ["string", "null"]},
                },
            },
            {
                "title": "Note",
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string"},
                    "date": {"type": ["string", "null"], "format": "date"},
                    "author": {"type": ["string", "null"]},
                    "at": {"type": ["string", "null"]},
                },
            },
            {
                "title": "Triage",
                "type": "object",
                "required": [
                    "task",
                    "verdict",
                    "confidence",
                    "summary",
                    "evidence",
                    "judged_status",
                ],
                "properties": {
                    "task": {"type": "string"},
                    "verdict": {
                        "enum": [
                            "real_pass",
                            "false_pass",
                            "design_bug",
                            "tb_bug",
                            "spec_unclear",
                            "env_issue",
                            "unknown",
                        ]
                    },
                    "confidence": {"enum": ["high", "medium", "low"]},
                    "summary": {"type": "string", "maxLength": 200},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "judged_status": {"type": "object"},
                    "doc": {"type": ["string", "null"]},
                    "bugs": {"type": "array", "items": {"type": "string"}},
                    "review": {"type": ["object", "null"]},
                    "author": {"type": ["string", "null"]},
                    "at": {"type": ["string", "null"]},
                },
            },
        ],
    },
    "plan": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "solouvm.plan",
        "type": "object",
        "required": ["schema", "run_id", "created_at", "provider", "selection", "builds", "jobs"],
        "properties": {
            "schema": {"type": "integer", "const": 1},
            "run_id": {"type": "string"},
            "created_at": {"type": "string", "format": "date-time"},
            "parent_run": {"type": ["string", "null"]},
            "provider": {"type": "string"},
            "selection": {"type": "object"},
            "builds": {"type": "object"},
            "jobs": {"type": "array", "items": {"type": "object"}},
        },
    },
    "results": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "solouvm.results",
        "type": "object",
        "required": [
            "schema",
            "run_id",
            "judged_at",
            "partial",
            "summary",
            "jobs",
            "signatures",
            "waivers",
            "hints",
        ],
        "properties": {
            "schema": {"type": "integer", "const": 1},
            "run_id": {"type": "string"},
            "judged_at": {"type": "string", "format": "date-time"},
            "partial": {"type": "boolean"},
            "summary": {"type": "object"},
            "jobs": {"type": "array", "items": {"type": "object"}},
            "signatures": {"type": "array", "items": {"type": "object"}},
            "waivers": {"type": "object"},
            "hints": {"type": "array", "items": {"type": "string"}},
        },
    },
    "facts": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "solouvm.job_facts",
        "type": "object",
        "required": [
            "schema",
            "generator",
            "kind",
            "run_id",
            "job_id",
            "try",
            "host",
            "pid",
            "timeout_impl",
            "started_at",
            "state",
        ],
        "properties": {
            "schema": {"type": "integer", "const": 1},
            "generator": {"type": "string"},
            "kind": {"enum": ["build", "job", "check"]},
            "run_id": {"type": "string"},
            "job_id": {"type": "string"},
            "try": {"type": "integer", "minimum": 1},
            "host": {"type": "string"},
            "pid": {"type": "integer"},
            "timeout_impl": {"enum": ["timeout", "gtimeout", "builtin"]},
            "started_at": {"type": "string", "format": "date-time"},
            "state": {"enum": ["running", "done", "interrupted"]},
            "rc": {"type": ["integer", "null"]},
            "signal": {"type": ["string", "null"]},
            "timed_out": {"type": "boolean"},
            "hung": {"type": "boolean"},
            "finished_at": {"type": ["string", "null"], "format": "date-time"},
            "duration_s": {"type": ["number", "null"]},
        },
    },
    "packet": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "solouvm.triage_tasks",
        "type": "object",
        "required": ["schema", "run_id", "policy", "tasks"],
        "properties": {
            "schema": {"type": "integer", "const": 1},
            "run_id": {"type": "string"},
            "policy": {"type": "object"},
            "tasks": {"type": "array", "items": {"type": "object"}},
        },
    },
    "output": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "solouvm.cli_json_envelope",
        "type": "object",
        "required": ["schema", "command", "ok"],
        "properties": {
            "schema": {"type": "integer", "const": 1},
            "command": {"type": "string"},
            "ok": {"type": "boolean"},
            "data": {},
            "truncated": {"type": ["object", "null"]},
            "next": {"type": "array", "items": {"type": "string"}},
            "error": {
                "type": "object",
                "required": ["code", "message", "file", "line", "hint"],
                "properties": {
                    "code": {"enum": sorted(ERROR_CODES)},
                    "message": {"type": "string"},
                    "file": {"type": ["string", "null"]},
                    "line": {"type": ["integer", "null"]},
                    "hint": {"type": ["string", "null"]},
                },
            },
        },
    },
}


def get_schema(name: str | None = None) -> dict[str, object]:
    """Return available schema names or the JSON Schema dict for ``name`` (Z-11)."""
    if name is None:
        return {
            "available": sorted(SCHEMAS),
            "descriptions": dict(sorted(_SCHEMA_DESCRIPTIONS.items())),
        }
    if name not in SCHEMAS:
        raise ConfigError(
            f"unknown schema {name!r}",
            code="CFG_INVALID",
            hint=f"available schemas: {', '.join(sorted(SCHEMAS))}",
        )
    return copy_schema(SCHEMAS[name])


def copy_schema(data: dict[str, object]) -> dict[str, object]:
    import copy

    return copy.deepcopy(data)


# ---------------------------------------------------------------- doctor (Z-02)


@dataclass(frozen=True)
class DoctorCheck:
    """Single environment readiness check result reported by ``solouvm doctor``."""

    name: str
    location: str
    ok: bool
    detail: str
    hint: str | None = None
    required: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "location": self.location,
            "ok": self.ok,
            "detail": self.detail,
            "hint": self.hint,
            "required": self.required,
        }


def _backend_for(config: Config) -> Backend:
    return resolve_sim_backend(config.simulator, config.sim)


def _extract_tool_cmd(template: str) -> str | None:
    try:
        tokens = shlex.split(template)
    except ValueError:
        return None
    if not tokens:
        return None
    for idx, tok in enumerate(tokens):
        if tok in ("&&", "||", ";"):
            continue
        if idx > 0 and tokens[idx - 1] in ("&&", "||", ";"):
            continue
        if "=" in tok and not tok.startswith(("+", "-", "{")):
            continue
        if tok in ("bash", "sh") and len(tokens) > idx + 1:
            return tok
        return tok
    return tokens[0]


def run_doctor(config: Config, provider: Provider) -> tuple[list[DoctorCheck], bool]:
    """Run A-side and B-side environment checks for the active provider (Z-02)."""
    checks: list[DoctorCheck] = []
    root = Path(config.root)
    pcfg = config.providers[provider.name]

    # A-side checks
    py_ver = platform.python_version()
    py_ok = sys.version_info >= (3, 10)
    checks.append(
        DoctorCheck(
            name="python",
            location="A",
            ok=py_ok,
            detail=f"Python {py_ver}",
            hint=None if py_ok else "install Python >= 3.10",
        )
    )

    vr = validate_project(root)
    checks.append(
        DoctorCheck(
            name="config",
            location="A",
            ok=vr.ok,
            detail=f"{len(vr.errors)} errors, {len(vr.warnings)} warnings",
            hint=None if vr.ok else "run 'solouvm validate' for details",
        )
    )

    make_path = shutil.which("make")
    checks.append(
        DoctorCheck(
            name="make",
            location="A",
            ok=make_path is not None,
            detail=make_path or "not found in PATH",
            hint=None if make_path else "install GNU Make on A",
        )
    )

    if pcfg.sync.kind == "rsync":
        rsync_path = shutil.which("rsync")
        checks.append(
            DoctorCheck(
                name="rsync",
                location="A",
                ok=rsync_path is not None,
                detail=rsync_path or "not found in PATH",
                hint=None if rsync_path else "install rsync on A",
            )
        )
    elif pcfg.sync.kind == "git":
        git_path = shutil.which("git")
        checks.append(
            DoctorCheck(
                name="git",
                location="A",
                ok=git_path is not None,
                detail=git_path or "not found in PATH",
                hint=None if git_path else "install git on A",
            )
        )

    # slang check on A when where=local or auto
    slang_bin = config.check.slang
    try:
        expanded_slang = expand_env(slang_bin) if "$" in slang_bin else slang_bin
    except ConfigError:
        expanded_slang = slang_bin
    a_slang = shutil.which(expanded_slang)
    checks.append(
        DoctorCheck(
            name="slang",
            location="A",
            ok=a_slang is not None,
            detail=a_slang or f"{expanded_slang} not found in PATH",
            hint="install slang or configure check.slang / check.where='remote'",
            required=(config.check.where == "local"),
        )
    )

    # B-side checks via provider transport
    def _b_exec(script: str) -> tuple[int, str]:
        try:
            rc, out = provider.transport.exec_output(["bash", "-c", script], cwd=".", check=False)
        except Exception as exc:
            return 255, str(exc)
        return rc, out.strip()

    if pcfg.transport.kind == "ssh":
        rc, out = _b_exec("hostname && pwd")
        checks.append(
            DoctorCheck(
                name="transport",
                location="B",
                ok=rc == 0,
                detail=out.replace("\n", " ") if rc == 0 else f"ssh check failed (rc={rc}): {out}",
                hint=None if rc == 0 else "check ssh host, keys, workdir and ~/.ssh/config",
            )
        )
        if rc != 0:
            all_ok = all(c.ok for c in checks if c.required)
            return checks, all_ok

    rc, out = _b_exec("test -w . && command -v bash && command -v make")
    checks.append(
        DoctorCheck(
            name="b_tools_and_workdir",
            location="B",
            ok=rc == 0,
            detail="bash, make and writable workdir verified" if rc == 0 else (out or f"rc={rc}"),
            hint=None if rc == 0 else "ensure bash, make exist on B and workdir is writable",
        )
    )

    rc_to, out_to = _b_exec(
        "if command -v timeout >/dev/null 2>&1; then echo timeout; "
        "elif command -v gtimeout >/dev/null 2>&1; then echo gtimeout; "
        "else echo builtin; fi"
    )
    checks.append(
        DoctorCheck(
            name="timeout_impl",
            location="B",
            ok=rc_to == 0,
            detail=out_to or "builtin",
            hint=None,
            required=False,
        )
    )

    if provider.setup:
        rc_setup, out_setup = _b_exec(provider.setup)
        checks.append(
            DoctorCheck(
                name="setup",
                location="B",
                ok=rc_setup == 0,
                detail="setup succeeded" if rc_setup == 0 else f"setup failed: {out_setup}",
                hint=None if rc_setup == 0 else "fix provider 'setup' script in config",
            )
        )

    backend = _backend_for(config)
    sim_tool = _extract_tool_cmd(backend.build)
    if backend.name == "fake":
        sim_detail = "built-in fake_sim.sh (bash)"
        sim_ok = shutil.which("bash") is not None
        if pcfg.transport.kind == "ssh":
            rc_bash, _ = _b_exec("command -v bash")
            sim_ok = rc_bash == 0
        checks.append(DoctorCheck(name="simulator", location="B", ok=sim_ok, detail=sim_detail))
    elif sim_tool and not sim_tool.startswith("{"):
        setup_prefix = f"{provider.setup} && " if provider.setup else ""
        rc_sim, out_sim = _b_exec(f"{setup_prefix}command -v {sh_quote(sim_tool)}")
        checks.append(
            DoctorCheck(
                name="simulator",
                location="B",
                ok=rc_sim == 0,
                detail=out_sim if rc_sim == 0 else f"{sim_tool!r} not found in B PATH",
                hint=None if rc_sim == 0 else f"add {sim_tool!r} to PATH or provider.setup",
            )
        )

    if config.check.where in ("remote", "auto") and not a_slang:
        rc_sl, out_sl = _b_exec(f"command -v {sh_quote(slang_bin)}")
        checks.append(
            DoctorCheck(
                name="slang",
                location="B",
                ok=rc_sl == 0,
                detail=out_sl if rc_sl == 0 else f"{slang_bin!r} not found on B",
                hint="install slang on A or B",
                required=(config.check.where == "remote"),
            )
        )

    if pcfg.launcher.kind == "lsf":
        rc_lsf, out_lsf = _b_exec("command -v bsub && command -v bjobs && command -v bkill")
        checks.append(
            DoctorCheck(
                name="lsf",
                location="B",
                ok=rc_lsf == 0,
                detail="bsub/bjobs/bkill available"
                if rc_lsf == 0
                else f"missing LSF tools: {out_lsf}",
                hint=None if rc_lsf == 0 else "ensure LSF tools (bsub/bjobs/bkill) are in PATH",
            )
        )
        if rc_lsf == 0 and pcfg.launcher.queue:
            rc_q, out_q = _b_exec(f"bqueues {sh_quote(pcfg.launcher.queue)}")
            checks.append(
                DoctorCheck(
                    name="lsf_queue",
                    location="B",
                    ok=rc_q == 0,
                    detail=f"queue {pcfg.launcher.queue!r} found" if rc_q == 0 else out_q,
                    hint=None if rc_q == 0 else f"verify LSF queue {pcfg.launcher.queue!r}",
                    required=False,
                )
            )

    all_ok = all(c.ok for c in checks if c.required)
    return checks, all_ok


def format_doctor(checks: Sequence[DoctorCheck]) -> str:
    """Format doctor check table for human terminal output."""
    lines: list[str] = []
    for chk in checks:
        mark = "OK" if chk.ok else ("FAIL" if chk.required else "WARN")
        lines.append(f"[{mark}] ({chk.location}) {chk.name}: {chk.detail}")
        if not chk.ok and chk.hint:
            lines.append(f"      hint: {chk.hint}")
    passed = sum(1 for c in checks if c.ok)
    lines.append("")
    lines.append(f"{passed}/{len(checks)} checks passed")
    return "\n".join(lines)


# ---------------------------------------------------------------- help (Z-12)

HELP_TOPICS: dict[str, str] = {
    "architecture": "architecture.md",
    "cli": "cli.md",
    "config": "config.md",
    "formats": "formats.md",
    "verdict": "verdict.md",
    "fast-compile": "fast-compile.md",
    "ai-workflow": "ai-workflow.md",
    "uvm-style": "uvm-style.md",
    "requirements": "requirements.md",
}

_FALLBACK_SUMMARIES: dict[str, str] = {
    "architecture": "# Architecture\nA-side Python controller + B-side generated Makefile/bash.\n",
    "cli": "# CLI Reference\nRun 'solouvm --help' or pass '--json' on any command.\n",
    "config": (
        "# Configuration\nConfigure solouvm.conf.py (CFG), solouvm.local.py (LOCAL), "
        "filelist.py, case.py, tbspec.py.\n"
    ),
    "formats": (
        "# Formats & Error Codes\nSee schema 1 formats: job.facts, plan.json, env.json, "
        "results.json, tasks.json.\n"
    ),
    "verdict": (
        "# Verdict Rules\nFixed priority rule order from 0a (build) through 14 (pass), "
        "expect_fail and triage review.\n"
    ),
    "fast-compile": (
        "# Fast Compile\nslang lint -> fake_top compile_only -> RTL incremental compile.\n"
    ),
    "ai-workflow": (
        "# AI Workflow\nMain agent runs regress & triage plan; --role triage subagents "
        "triage packets and write docs; main reviews.\n"
    ),
    "uvm-style": (
        "# UVM Style Guide\nStyle 1 templates, user code regions // solouvm:user begin/end, "
        "*_files.svh includes.\n"
    ),
    "requirements": (
        "# Requirements\nComplete M1-M4 verification orchestration and UVM generation toolkit.\n"
    ),
}


def show_help(topic: str | None = None, *, root: Path | None = None) -> dict[str, object]:
    """Return offline help topic listing or markdown documentation (Z-12)."""
    if topic is None:
        return {
            "topics": sorted(HELP_TOPICS),
            "usage": "solouvm help <topic>",
        }
    key = topic.lower().removesuffix(".md")
    if key not in HELP_TOPICS:
        close = get_close_matches(key, list(HELP_TOPICS), n=1)
        hint = (
            f"did you mean {close[0]!r}?"
            if close
            else f"available: {', '.join(sorted(HELP_TOPICS))}"
        )
        raise ConfigError(f"unknown help topic {topic!r}", code="CFG_INVALID", hint=hint)

    doc_name = HELP_TOPICS[key]
    package_dir = Path(__file__).resolve().parent
    doc_dirs = [package_dir.parent / "doc", package_dir / "_docs"]
    if root is not None:
        doc_dirs.insert(0, Path(root) / "doc")
    env_doc = os.environ.get("SOLOUVM_DOC_DIR")
    if env_doc:
        doc_dirs.insert(0, Path(env_doc))

    for doc_dir in doc_dirs:
        candidate = doc_dir / doc_name
        if candidate.is_file():
            return {
                "topic": key,
                "file": doc_name,
                "content": candidate.read_text(encoding="utf-8", errors="replace"),
            }
    return {
        "topic": key,
        "file": doc_name,
        "content": _FALLBACK_SUMMARIES[key],
    }
