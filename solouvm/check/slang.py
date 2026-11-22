"""Layer 1 fast compile: slang full lint check and diagnostic formatting (E-01..E-03, E-09)."""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.collect.files import GROUPS
from solouvm.config.load import expand_env
from solouvm.errors import ConfigError, InfraError
from solouvm.gen.shell import check_no_newlines, relpath, sh_quote
from solouvm.packages import package_sync_files
from solouvm.providers.base import log_command
from solouvm.providers.transport_local import LocalTransport

if TYPE_CHECKING:
    from solouvm.collect.files import FileEntry, FileList
    from solouvm.config.schema import Config
    from solouvm.providers.base import Provider, Transport

__all__ = [
    "CHECK_REL_DIR",
    "VALID_FORMATS",
    "VALID_WHERE",
    "CheckDiagnostic",
    "CheckResult",
    "format_check_path",
    "format_check_result",
    "generate_check_dotf",
    "is_missing_binary_output",
    "is_remote_provider",
    "normalize_diag_path",
    "parse_slang_diagnostics",
    "parse_text_diagnostics",
    "resolve_check_where",
    "resolve_in_root",
    "run_slang",
]

CHECK_REL_DIR = ".solouvm/check"
VALID_WHERE = frozenset({"local", "remote", "auto"})
VALID_FORMATS = frozenset({"text", "gcc", "json"})

_TEXT_DIAG_RE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*"
    r"(?P<severity>fatal\s+error|fatal|error|warning):\s*(?P<message>.+?)\s*$",
    re.IGNORECASE,
)

_MISSING_BIN_PATTERNS: tuple[str, ...] = (
    "command not found",
    "not found",
    "no such file or directory",
    "permission denied",
    "cannot execute",
)


@dataclass(frozen=True)
class CheckDiagnostic:
    """Single diagnostic emitted by ``slang`` or simulator fast-compile checks."""

    file: str
    line: int
    col: int
    severity: str
    message: str
    hint: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize diagnostic to a JSON-compatible dict."""
        return {
            "file": self.file,
            "line": self.line,
            "col": self.col,
            "severity": self.severity,
            "message": self.message,
            "hint": self.hint,
        }


@dataclass
class CheckResult:
    """Aggregated outcome across ``solouvm check`` layers."""

    ok: bool
    unavailable: bool = False
    where: str = "local"
    files_checked: int = 0
    duration_s: float = 0.0
    diagnostics: list[CheckDiagnostic] = field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    layers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    incremental_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        """Serialize result to a deterministic JSON-compatible dict."""
        return {
            "schema": 1,
            "ok": self.ok,
            "unavailable": self.unavailable,
            "where": self.where,
            "files_checked": self.files_checked,
            "duration_s": round(self.duration_s, 3),
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "layers": list(self.layers),
            "warnings": list(self.warnings),
            "incremental_files": list(self.incremental_files),
            "diagnostics": [diag.to_dict() for diag in self.diagnostics],
        }


def resolve_in_root(root: Path, resolved_root: Path, raw: str) -> tuple[str, bool]:
    """Return ``(root_rel_or_raw, is_outside_root)`` for a local path string."""
    norm_root = Path(os.path.normpath(root.absolute()))
    candidate = Path(os.path.normpath(raw if os.path.isabs(raw) else norm_root / raw))
    try:
        return candidate.relative_to(norm_root).as_posix(), False
    except ValueError:
        pass
    try:
        return candidate.resolve().relative_to(resolved_root).as_posix(), False
    except ValueError:
        return raw, True


def format_check_path(
    root: Path,
    resolved_root: Path,
    raw: str,
    *,
    expand_local: bool,
    outside: bool | None = None,
    base_rel: str = CHECK_REL_DIR,
) -> str:
    """Format a file or incdir path relative to ``base_rel`` (preserving outside_root/$VAR)."""
    check_no_newlines(raw, field="filelist path")
    value = raw
    if "$" in value:
        if not expand_local:
            return value
        value = expand_env(value)
        root_rel, is_outside = resolve_in_root(root, resolved_root, value)
        if is_outside:
            return Path(os.path.normpath(value)).as_posix()
        return relpath(root_rel, base_rel)
    if outside is True:
        return value
    if os.path.isabs(value):
        root_rel, is_outside = resolve_in_root(root, resolved_root, value)
        if is_outside:
            return value
        return relpath(root_rel, base_rel)
    norm_rel = posixpath.normpath(value.replace("\\", "/"))
    return relpath(norm_rel, base_rel)


def normalize_diag_path(raw_file: str, root: Path) -> str:
    """Convert diagnostic file paths (e.g. relative to ``.solouvm/check``) back to root-relative."""
    text = raw_file.strip().replace("\\", "/")
    if not text:
        return ""
    norm_root = Path(os.path.normpath(root.absolute()))
    resolved_root = norm_root.resolve()
    check_dir = norm_root / CHECK_REL_DIR

    p = Path(text)
    if p.is_absolute():
        norm_p = Path(os.path.normpath(str(p)))
        try:
            return norm_p.relative_to(norm_root).as_posix()
        except ValueError:
            pass
        try:
            return norm_p.resolve().relative_to(resolved_root).as_posix()
        except ValueError:
            return norm_p.as_posix()

    if text.startswith("../") or text == "..":
        from_check = Path(os.path.normpath(check_dir / text))
        try:
            return from_check.relative_to(norm_root).as_posix()
        except ValueError:
            pass
        try:
            return from_check.resolve().relative_to(resolved_root).as_posix()
        except ValueError:
            rel_raw = os.path.relpath(str(from_check), str(norm_root))
            try:
                rel_res = os.path.relpath(str(from_check.resolve()), str(resolved_root))
            except OSError:
                rel_res = rel_raw
            best = min((rel_raw, rel_res), key=lambda r: (len(Path(r).parts), len(r)))
            return posixpath.normpath(best.replace("\\", "/"))

    if (check_dir / text).is_file() and not (norm_root / text).is_file():
        return posixpath.normpath(f"{CHECK_REL_DIR}/{text}")
    return posixpath.normpath(text)


def is_remote_provider(provider: Provider) -> bool:
    """Return True when ``provider`` targets a remote execution host B."""
    transport = provider.transport
    if not isinstance(transport, LocalTransport) and getattr(transport, "kind", None) != "local":
        return True
    return getattr(provider.sync, "kind", "none") != "none"


def _find_local_binary(binary: str, root: Path) -> str | None:
    if not binary.strip():
        return None
    hit = shutil.which(binary)
    if hit is not None:
        return os.path.abspath(hit)
    if not os.path.isabs(binary) and ("/" in binary or os.sep in binary):
        candidate = root / binary
        hit = shutil.which(str(candidate))
        if hit is not None:
            return os.path.abspath(hit)
    return None


def _resolve_outside_candidate(raw: str, root: Path) -> Path:
    if os.path.isabs(raw):
        return Path(raw)
    return Path(os.path.normpath(root / raw))


def _outside_exists_locally(entry: FileEntry, root: Path) -> bool:
    if "$" in entry.path:
        try:
            expanded = expand_env(entry.path)
        except ConfigError:
            return False
        return _resolve_outside_candidate(expanded, root).is_file()
    return _resolve_outside_candidate(entry.path, root).is_file()


def _expand_if_needed(value: str) -> str:
    return expand_env(value) if "$" in value else value


def resolve_check_where(
    *,
    config: Config,
    files: FileList,
    provider: Provider,
    where: str | None = None,
) -> tuple[str, str | None, list[str]]:
    """Select execution side (``local`` or ``remote``), local binary path, and warnings."""
    requested = where if where is not None else config.check.where
    if requested not in VALID_WHERE:
        raise ConfigError(
            f"invalid check.where {requested!r}; expected one of: local, remote, auto",
            code="CFG_INVALID",
        )

    root = Path(os.path.normpath(Path(config.root).absolute()))
    remote_capable = is_remote_provider(provider)
    warnings: list[str] = []

    if requested == "local":
        slang_cmd = _expand_if_needed(config.check.slang)
        local_bin = _find_local_binary(slang_cmd, root)
        if local_bin is None:
            raise InfraError(
                f"slang executable {slang_cmd!r} not found on local host",
                code="CHECK_UNAVAILABLE",
                hint="install slang on PATH, set check.slang, or use --where remote",
            )
        return "local", local_bin, warnings

    if requested == "remote":
        if not remote_capable:
            raise InfraError(
                f"remote check requested, but provider {provider.name!r} has no remote transport",
                code="CHECK_UNAVAILABLE",
                hint="select a remote provider with --provider <name> or use --where local",
            )
        return "remote", None, warnings

    # auto mode: prefer local when slang is available and all files are accessible on A
    local_bin: str | None = None
    try:
        slang_cmd = _expand_if_needed(config.check.slang)
    except ConfigError:
        slang_cmd = ""
    if slang_cmd:
        local_bin = _find_local_binary(slang_cmd, root)

    outside_entries = files.outside_root
    if outside_entries:
        if remote_capable:
            warnings.append("file list contains outside-root paths; running check on remote host B")
            return "remote", None, warnings
        if local_bin is None:
            raise InfraError(
                f"slang executable {config.check.slang!r} not found locally and "
                f"provider {provider.name!r} is local",
                code="CHECK_UNAVAILABLE",
                hint="install slang on PATH, set check.slang, or configure a remote provider",
            )
        missing_outside = [e.path for e in outside_entries if not _outside_exists_locally(e, root)]
        if missing_outside:
            raise InfraError(
                f"outside-root files missing on local host ({missing_outside[0]}) and "
                f"provider {provider.name!r} is local",
                code="CHECK_UNAVAILABLE",
                hint="configure a remote provider where outside-root paths exist",
            )
        return "local", local_bin, warnings

    if local_bin is None:
        if remote_capable:
            return "remote", None, warnings
        raise InfraError(
            f"slang executable {config.check.slang!r} not found on local host",
            code="CHECK_UNAVAILABLE",
            hint="install slang on PATH, set check.slang, or configure a remote provider",
        )
    return "local", local_bin, warnings


def _is_excluded(path: str, patterns: Sequence[str]) -> bool:
    norm_path = path.replace("\\", "/")
    for pat in patterns:
        norm_pat = pat.replace("\\", "/")
        if fnmatch(norm_path, norm_pat):
            return True
        if norm_pat.startswith("**/") and fnmatch(norm_path, norm_pat[3:]):
            return True
        if norm_pat.endswith("/**"):
            prefix = norm_pat[:-2]
            if norm_path == norm_pat[:-3] or norm_path.startswith(prefix):
                return True
    return False


def _dedupe_directives(lines: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if line.startswith(("+define+", "+incdir+")):
            if line in seen:
                continue
            seen.add(line)
        out.append(line)
    return out


def _resolve_uvm_lines(
    *,
    config: Config,
    root: Path,
    resolved_root: Path,
    provider: Provider,
    expand_local: bool,
) -> list[str]:
    raw_uvm = config.check.uvm_src.strip()
    if not raw_uvm:
        return []
    check_no_newlines(raw_uvm, field="check.uvm_src")

    if expand_local:
        if "$" in raw_uvm:
            try:
                uvm_dir_str = expand_env(raw_uvm)
            except ConfigError:
                is_default = raw_uvm == "${UVM_HOME}/src" or config.sources.get(
                    "check.uvm_src"
                ) in (None, "default")
                if is_default:
                    return []
                raise
        else:
            uvm_dir_str = raw_uvm

        uvm_dir = Path(uvm_dir_str) if os.path.isabs(uvm_dir_str) else root / uvm_dir_str
        if not (uvm_dir / "uvm_pkg.sv").is_file():
            return []
        root_rel, is_outside = resolve_in_root(root, resolved_root, str(uvm_dir))
        if is_outside:
            formatted = Path(os.path.normpath(str(uvm_dir))).as_posix()
        else:
            formatted = relpath(root_rel, CHECK_REL_DIR)
        pkg_path = posixpath.join(formatted, "uvm_pkg.sv")
        return [f"+incdir+{formatted}", pkg_path]

    # Remote execution: preserve ${VAR}; verify literal paths via transport.
    if "$" in raw_uvm:
        explicit = config.sources.get("check.uvm_src") in ("conf", "local", "cli") or (
            raw_uvm != "${UVM_HOME}/src"
        )
        if not explicit:
            return []
        formatted = raw_uvm.rstrip("/")
        return [f"+incdir+{formatted}", f"{formatted}/uvm_pkg.sv"]

    uvm_pkg_probe = posixpath.join(raw_uvm.rstrip("/"), "uvm_pkg.sv")
    if not provider.transport.exists(uvm_pkg_probe):
        return []
    formatted = format_check_path(root, resolved_root, raw_uvm, expand_local=False)
    return [f"+incdir+{formatted}", posixpath.join(formatted, "uvm_pkg.sv")]


def generate_check_dotf(
    *,
    config: Config,
    files: FileList,
    provider: Provider,
    expand_local: bool,
) -> tuple[Path, int]:
    """Generate ``.solouvm/check/check.f`` and return ``(path, files_checked)``."""
    root = Path(os.path.normpath(Path(config.root).absolute()))
    resolved_root = root.resolve()
    check_dir = root / CHECK_REL_DIR
    check_dir.mkdir(parents=True, exist_ok=True)

    exclude_pats = [_expand_if_needed(pat) if expand_local else pat for pat in config.check.exclude]
    outside_map = {entry.path: entry.outside_root for entry in files.entries}

    lines: list[str] = []
    for define in config.defines:
        val = _expand_if_needed(define) if expand_local else define
        check_no_newlines(val, field="defines")
        lines.append(f"+define+{val}")
    for incdir in config.incdirs:
        formatted = format_check_path(root, resolved_root, incdir, expand_local=expand_local)
        lines.append(f"+incdir+{formatted}")

    lines.extend(
        _resolve_uvm_lines(
            config=config,
            root=root,
            resolved_root=resolved_root,
            provider=provider,
            expand_local=expand_local,
        )
    )

    files_checked = 0
    for group in GROUPS:
        for define in files.defines.get(group, []):
            val = _expand_if_needed(define) if expand_local else define
            check_no_newlines(val, field=f"files.defines[{group!r}]")
            lines.append(f"+define+{val}")
        for incdir in files.incdirs.get(group, []):
            formatted = format_check_path(root, resolved_root, incdir, expand_local=expand_local)
            lines.append(f"+incdir+{formatted}")
        for lib in files.libs.get(group, []):
            check_no_newlines(lib, field=f"files.libs[{group!r}]")
            if lib.startswith("-v "):
                target = lib[3:]
                if _is_excluded(target, exclude_pats):
                    continue
                formatted = format_check_path(
                    root, resolved_root, target, expand_local=expand_local
                )
                lines.append(f"-v {formatted}")
            elif lib.startswith("-y "):
                formatted = format_check_path(
                    root, resolved_root, lib[3:], expand_local=expand_local
                )
                lines.append(f"-y {formatted}")
            else:
                lines.append(lib)
        for file_path in files.groups.get(group, []):
            if _is_excluded(file_path, exclude_pats):
                continue
            formatted = format_check_path(
                root,
                resolved_root,
                file_path,
                expand_local=expand_local,
                outside=outside_map.get(file_path),
            )
            lines.append(formatted)
            files_checked += 1

    deduped = _dedupe_directives(lines)
    dotf_path = check_dir / "check.f"
    dotf_path.write_text("\n".join(deduped) + "\n" if deduped else "", encoding="utf-8")
    return dotf_path, files_checked


def _normalize_severity(raw: object) -> str | None:
    text = str(raw or "error").strip().lower()
    if text in ("ignored", "none", ""):
        return None
    if text in ("fatal", "fatal error", "err", "error"):
        return "error"
    if text in ("warn", "warning"):
        return "warning"
    if text in ("note", "info"):
        return "note"
    return "error"


def _parse_json_diag_item(item: object, root: Path) -> CheckDiagnostic | None:
    if not isinstance(item, dict):
        return None
    severity = _normalize_severity(item.get("severity") or item.get("level") or item.get("kind"))
    if severity is None or severity == "note":
        return None

    loc = item.get("location")
    raw_file = ""
    raw_line: object = 0
    raw_col: object = 0
    if isinstance(loc, dict):
        raw_file = str(
            loc.get("file") or loc.get("fileName") or loc.get("filename") or loc.get("path") or ""
        )
        raw_line = loc.get("line") or loc.get("lineno") or 0
        raw_col = loc.get("column") or loc.get("col") or 0
    elif isinstance(loc, str) and ":" in loc:
        parts = loc.rsplit(":", 2)
        raw_file = parts[0]
        if len(parts) >= 2 and parts[1].isdigit():
            raw_line = int(parts[1])
        if len(parts) == 3 and parts[2].isdigit():
            raw_col = int(parts[2])

    if not raw_file:
        raw_file = str(
            item.get("file")
            or item.get("fileName")
            or item.get("filename")
            or item.get("path")
            or ""
        )
    if not raw_line:
        raw_line = item.get("line") or item.get("lineno") or 0
    if not raw_col:
        raw_col = item.get("col") or item.get("column") or 0

    try:
        line = int(raw_line)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        line = 0
    try:
        col = int(raw_col)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        col = 0

    message = str(item.get("message") or item.get("msg") or item.get("text") or "").strip()
    hint_val = item.get("hint")
    hint = str(hint_val) if isinstance(hint_val, str) and hint_val.strip() else None
    if hint is None and severity == "error" and _needs_uvm_hint(message):
        hint = _UVM_SRC_HINT
    return CheckDiagnostic(
        file=normalize_diag_path(raw_file, root),
        line=line,
        col=col,
        severity=severity,
        message=message,
        hint=hint,
    )


_UVM_SRC_HINT = "set UVM_HOME or configure check.uvm_src to point to your UVM source directory"


def _needs_uvm_hint(message: str) -> bool:
    low = message.lower()
    return "unknown package 'uvm_pkg'" in low or (
        "uvm_macros.svh" in low and ("not found" in low or "no such file" in low)
    )


def _try_parse_json_diagnostics(text: str, root: Path) -> list[CheckDiagnostic] | None:
    stripped = text.strip()
    if not stripped:
        return None
    parsed: object = None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        # Support JSON-lines diagnostic streams if emitted line by line.
        lines = [ln.strip() for ln in stripped.splitlines() if ln.strip()]
        if len(lines) >= 2 and all(ln.startswith("{") and ln.endswith("}") for ln in lines):
            items: list[object] = []
            all_ok = True
            for ln in lines:
                try:
                    items.append(json.loads(ln))
                except json.JSONDecodeError:
                    all_ok = False
                    break
            if all_ok:
                parsed = items
        if parsed is None and stripped.startswith(("[", "{")):
            # Slang >= 12 appends "Build succeeded/failed: ..." summary after the JSON array.
            try:
                parsed, _ = json.JSONDecoder().raw_decode(stripped)
            except json.JSONDecodeError:
                return None
        if parsed is None:
            return None

    if isinstance(parsed, dict):
        if isinstance(parsed.get("diagnostics"), list):
            raw_items = parsed["diagnostics"]
        elif "severity" in parsed or "message" in parsed:
            raw_items = [parsed]
        else:
            return None
    elif isinstance(parsed, list):
        raw_items = parsed
    else:
        return None

    diags: list[CheckDiagnostic] = []
    for item in raw_items:
        diag = _parse_json_diag_item(item, root)
        if diag is not None:
            diags.append(diag)
    return diags


def parse_text_diagnostics(text: str, root: Path) -> list[CheckDiagnostic]:
    """Parse ``file:line[:col]: error|warning: message`` diagnostic lines."""
    diags: list[CheckDiagnostic] = []
    for raw_line in text.splitlines():
        line_str = raw_line.strip()
        if not line_str:
            continue
        match = _TEXT_DIAG_RE.match(line_str)
        if match is None:
            continue
        severity = _normalize_severity(match.group("severity"))
        if severity is None or severity == "note":
            continue
        col_str = match.group("col")
        msg_str = match.group("message").strip()
        diags.append(
            CheckDiagnostic(
                file=normalize_diag_path(match.group("file"), root),
                line=int(match.group("line")),
                col=int(col_str) if col_str is not None else 0,
                severity=severity,
                message=msg_str,
                hint=_UVM_SRC_HINT if severity == "error" and _needs_uvm_hint(msg_str) else None,
            )
        )
    return diags


def parse_slang_diagnostics(stdout: str, stderr: str, root: Path) -> list[CheckDiagnostic]:
    """Parse ``slang --diag-json -`` output, falling back to text diagnostics cleanly."""
    json_diags = _try_parse_json_diagnostics(stdout, root)
    if json_diags is not None:
        if stderr.strip():
            extra = parse_text_diagnostics(stderr, root)
            seen = {(d.file, d.line, d.col, d.severity, d.message) for d in json_diags}
            for diag in extra:
                key = (diag.file, diag.line, diag.col, diag.severity, diag.message)
                if key not in seen:
                    seen.add(key)
                    json_diags.append(diag)
        return json_diags

    combined = "\n".join(part for part in (stdout, stderr) if part.strip())
    return parse_text_diagnostics(combined, root)


def is_missing_binary_output(rc: int, text: str, binary: str = "") -> bool:
    """Return True when ``rc`` and tool output indicate a missing/non-executable binary."""
    if rc == 127:
        return True
    if rc == 0:
        return False
    lower = text.lower()
    if any(pat in lower for pat in _MISSING_BIN_PATTERNS):
        if not binary:
            return True
        bin_name = Path(binary).name.lower()
        return bin_name in lower or "command not found" in lower
    return False


def _read_transport_text(transport: Transport, path: str) -> str:
    if not transport.exists(path):
        return ""
    return "\n".join(line for _, line in transport.read_lines(path))


def _exec_slang_remote(
    *,
    provider: Provider,
    argv: list[str],
) -> tuple[int, str, str]:
    transport = provider.transport
    if provider.setup:
        quoted = " ".join(sh_quote(part, field="slang") for part in argv)
        exec_argv = ["bash", "-c", f"{provider.setup} && {quoted}"]
    else:
        exec_argv = argv

    exec_capture = getattr(transport, "exec_capture", None)
    if callable(exec_capture):
        return exec_capture(exec_argv, cwd=CHECK_REL_DIR, check=False)

    exec_output = getattr(transport, "exec_output", None)
    if callable(exec_output):
        rc, out = exec_output(exec_argv, cwd=CHECK_REL_DIR, check=False)
        return rc, str(out), ""

    cmd_str = " ".join(sh_quote(part, field="slang") for part in argv)
    if provider.setup:
        wrapped = f"{provider.setup} && {cmd_str} > slang.stdout 2> slang.stderr"
    else:
        wrapped = f"{cmd_str} > slang.stdout 2> slang.stderr"
    rc = transport.exec(["bash", "-c", wrapped], cwd=CHECK_REL_DIR, check=False)
    stdout = _read_transport_text(transport, f"{CHECK_REL_DIR}/slang.stdout")
    stderr = _read_transport_text(transport, f"{CHECK_REL_DIR}/slang.stderr")
    return rc, stdout, stderr


def run_slang(
    *,
    config: Config,
    files: FileList,
    provider: Provider,
    where: str | None = None,
) -> CheckResult:
    """Run Layer 1 ``slang`` full syntax/semantic check locally or remotely."""
    effective_where, local_bin, warnings = resolve_check_where(
        config=config,
        files=files,
        provider=provider,
        where=where,
    )
    expand_local = effective_where == "local"
    root = Path(os.path.normpath(Path(config.root).absolute()))

    dotf_path, files_checked = generate_check_dotf(
        config=config,
        files=files,
        provider=provider,
        expand_local=expand_local,
    )
    _ = dotf_path

    if files_checked == 0:
        warnings.append("no source files left to check after exclusions")
        return CheckResult(
            ok=True,
            unavailable=False,
            where=effective_where,
            files_checked=0,
            duration_s=0.0,
            diagnostics=[],
            error_count=0,
            warning_count=0,
            layers=["slang"],
            warnings=warnings,
        )

    slang_bin = local_bin if local_bin is not None else config.check.slang
    slang_args = [
        _expand_if_needed(arg) if expand_local else arg for arg in config.check.slang_args
    ]
    for arg in slang_args:
        check_no_newlines(arg, field="check.slang_args")

    argv = [slang_bin, "-f", "check.f", "--lint-only", "--diag-json", "-", *slang_args]

    start = time.monotonic()
    if effective_where == "local":
        check_dir = root / CHECK_REL_DIR
        log_command(argv)
        try:
            proc = subprocess.run(
                argv,
                cwd=str(check_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise InfraError(
                f"failed to execute slang {slang_bin!r}: {exc}",
                code="CHECK_UNAVAILABLE",
                hint="verify check.slang path and executable permissions",
            ) from exc
        rc, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    else:
        push_list = sorted({f"{CHECK_REL_DIR}/check.f", *package_sync_files(root, config)})
        provider.sync.push(provider.transport, str(root), push_list)
        rc, stdout, stderr = _exec_slang_remote(provider=provider, argv=argv)
    duration_s = time.monotonic() - start

    diagnostics = parse_slang_diagnostics(stdout, stderr, root)
    error_count = sum(1 for d in diagnostics if d.severity == "error")
    warning_count = sum(1 for d in diagnostics if d.severity == "warning")

    if rc != 0 and error_count == 0:
        combined = "\n".join(part for part in (stderr, stdout) if part.strip())
        if is_missing_binary_output(rc, combined, slang_bin):
            raise InfraError(
                f"slang executable {config.check.slang!r} is unavailable on {effective_where} host",
                code="CHECK_UNAVAILABLE",
                hint="install slang on PATH, set check.slang, or configure provider.setup",
            )
        if rc == 255 and "ssh" in combined.lower():
            raise InfraError(
                f"ssh transport failed while running slang: {combined.strip()}",
                code="PROVIDER_FAILED",
            )
        fallback_msg = (
            combined.splitlines()[0].strip() if combined.strip() else f"slang failed with exit {rc}"
        )
        diagnostics.append(
            CheckDiagnostic(file="", line=0, col=0, severity="error", message=fallback_msg)
        )
        error_count = 1

    ok = rc == 0 and error_count == 0
    return CheckResult(
        ok=ok,
        unavailable=False,
        where=effective_where,
        files_checked=files_checked,
        duration_s=duration_s,
        diagnostics=diagnostics,
        error_count=error_count,
        warning_count=warning_count,
        layers=["slang"],
        warnings=warnings,
    )


def _format_diag_line(diag: CheckDiagnostic, *, gcc_mode: bool) -> str:
    if diag.file and diag.line > 0:
        loc = f"{diag.file}:{diag.line}:{diag.col}" if diag.col > 0 else f"{diag.file}:{diag.line}"
    elif diag.file:
        loc = diag.file
    else:
        loc = "<check>" if gcc_mode else ""
    prefix = f"{loc}: " if loc else ""
    return f"{prefix}{diag.severity}: {diag.message}"


def format_check_result(result: CheckResult, fmt: str = "text") -> str:
    """Format a :class:`CheckResult` in ``text``, ``gcc``, or ``json`` format."""
    if fmt not in VALID_FORMATS:
        raise ConfigError(
            f"unsupported check format {fmt!r}; expected one of: text, gcc, json",
            code="CFG_INVALID",
        )

    if fmt == "json":
        return json.dumps(result.to_dict(), indent=2, ensure_ascii=False) + "\n"

    if fmt == "gcc":
        return "\n".join(_format_diag_line(diag, gcc_mode=True) for diag in result.diagnostics)

    lines: list[str] = []
    for diag in result.diagnostics:
        lines.append(_format_diag_line(diag, gcc_mode=False))
        if diag.hint:
            lines.append(f"hint: {diag.hint}")

    for warn in result.warnings:
        text = warn if warn.startswith("warning:") else f"warning: {warn}"
        lines.append(text)

    if lines:
        lines.append("")

    if result.unavailable:
        lines.append("check: unavailable")
        return "\n".join(lines)

    err_word = "1 error" if result.error_count == 1 else f"{result.error_count} errors"
    warn_word = "1 warning" if result.warning_count == 1 else f"{result.warning_count} warnings"
    layer_label = (
        "slang"
        if result.layers == ["slang"]
        else ("+".join(result.layers) if result.layers else "check")
    )
    lines.append(
        f"{layer_label}: {err_word}, {warn_word} in {result.duration_s:.1f}s "
        f"({result.files_checked} files)"
    )
    if result.incremental_files:
        lines.append(f"incremental RTL compiled ({len(result.incremental_files)} files)")
    return "\n".join(lines)
