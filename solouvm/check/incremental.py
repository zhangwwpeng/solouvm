"""Layer 3 fast compile: incremental RTL change detection and propagation (E-06..E-08)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.check.faketop import (
    ensure_check_scripts,
    parse_simulator_diagnostics,
    run_check_job,
)
from solouvm.check.slang import (
    CHECK_REL_DIR,
    CheckDiagnostic,
    format_check_path,
    is_missing_binary_output,
    resolve_in_root,
)
from solouvm.errors import ConfigError, InfraError
from solouvm.gen.shell import check_no_newlines

if TYPE_CHECKING:
    from solouvm.backends.base import Backend
    from solouvm.collect.files import FileList
    from solouvm.config.schema import Config
    from solouvm.providers.base import Provider

__all__ = [
    "FAST_STATE_REL",
    "FAST_STATE_SCHEMA",
    "INCREMENTAL_BACKENDS",
    "IncrementalPlan",
    "IncrementalResult",
    "compute_options_hash",
    "plan_rtl_incremental",
    "read_fast_state",
    "run_incremental_rtl",
    "write_fast_state",
]

FAST_STATE_SCHEMA = 1
FAST_STATE_REL = Path(CHECK_REL_DIR) / "fast_state.json"
INCREMENTAL_BACKENDS = frozenset({"vcs", "xrun"})

_CHUNK_SIZE = 65536
_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_PACKAGE_RE = re.compile(r"^\s*package\s+(?:automatic\s+|static\s+)?(\w+)\s*;", re.MULTILINE)
_IMPORT_RE = re.compile(r"\bimport\s+(\w+)\s*::")
_INCLUDE_STR_RE = re.compile(r'`include\s+"([^"\n]+)"')
_INCLUDE_MACRO_RE = re.compile(r'`include\s+(?!")')
_MACRO_DEFINE_RE = re.compile(r"^\s*`(?:define|undef)\b", re.MULTILINE)


@dataclass
class IncrementalPlan:
    """Computed incremental RTL compile plan against ``fast_state.json``."""

    files_to_compile: list[str]
    changed_files: list[str]
    full_fallback: bool
    fallback_reason: str | None
    new_state: dict[str, object]


@dataclass
class IncrementalResult:
    """Outcome of Layer 3 incremental RTL compilation."""

    ok: bool
    skipped: bool = False
    files_compiled: list[str] = field(default_factory=list)
    full_fallback: bool = False
    fallback_reason: str | None = None
    duration_s: float = 0.0
    diagnostics: list[CheckDiagnostic] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _sha1_file(path: Path) -> str:
    hasher = hashlib.sha1()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def _strip_sv_comments(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", match.group(0))

    return _COMMENT_RE.sub(repl, text)


def read_fast_state(path: Path) -> dict[str, object] | None:
    """Read and validate ``.solouvm/check/fast_state.json``; return None if absent/invalid."""
    state_path = Path(path)
    if not state_path.is_file():
        return None
    try:
        raw_text = state_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    schema = data.get("schema")
    if type(schema) is not int or schema != FAST_STATE_SCHEMA:
        raise ConfigError(
            f"unsupported fast_state schema {schema!r} (expected {FAST_STATE_SCHEMA})",
            code="CFG_INVALID",
            file=state_path,
            hint="delete .solouvm/check/fast_state.json and re-run check",
        )
    if (
        not isinstance(data.get("options_hash"), str)
        or not isinstance(data.get("files"), dict)
        or not isinstance(data.get("deps"), dict)
    ):
        return None
    return data


def write_fast_state(path: Path, state: dict[str, object]) -> None:
    """Atomically write ``fast_state.json`` with deterministic formatting."""
    state_path = Path(path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    fd, tmp_name = tempfile.mkstemp(
        dir=str(state_path.parent),
        prefix=".fast_state.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(tmp_name, state_path)
    except BaseException:
        with contextlib.suppress(OSError):
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        raise


def compute_options_hash(
    config: Config,
    files: FileList,
    backend: Backend,
    provider: Provider,
) -> str:
    """Compute SHA-1 hash of RTL compile options and RTL filelist structure."""
    default_build = config.builds.get("default")
    payload = {
        "backend": backend.name,
        "build_cmd": backend.build,
        "compile_only": backend.compile_only,
        "defines": list(config.defines),
        "incdirs": list(config.incdirs),
        "rtl_defines": list(files.defines.get("rtl", [])),
        "rtl_incdirs": list(files.incdirs.get("rtl", [])),
        "rtl_libs": list(files.libs.get("rtl", [])),
        "rtl_files": list(files.groups.get("rtl", [])),
        "default_defines": list(default_build.defines) if default_build else [],
        "default_incdirs": list(default_build.incdirs) if default_build else [],
        "default_compile_opts": list(default_build.compile_opts) if default_build else [],
        "provider_setup": provider.setup,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _git_dirty_paths(root: Path) -> set[str]:
    if not (root / ".git").exists():
        return set()
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return set()
    if proc.returncode != 0:
        return set()
    dirty: set[str] = set()
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        raw_path = line[3:].strip()
        if " -> " in raw_path:
            raw_path = raw_path.split(" -> ", 1)[1].strip()
        if raw_path:
            dirty.add(raw_path.replace("\\", "/"))
    return dirty


def _resolve_local_file(root: Path, raw: str) -> Path | None:
    if "$" in raw:
        return None
    candidate = Path(raw) if os.path.isabs(raw) else root / raw
    return candidate if candidate.is_file() else None


def _normalize_key(root: Path, resolved_root: Path, path: Path) -> str:
    rel, is_outside = resolve_in_root(root, resolved_root, str(path))
    if not is_outside:
        return rel
    return Path(os.path.normpath(str(path))).as_posix()


def _resolve_include_target(
    inc_name: str,
    including_file: Path,
    incdir_paths: Sequence[Path],
    root: Path,
) -> tuple[list[Path], bool, bool]:
    """Resolve ``inc_name``; return ``(hits, is_ambiguous, is_unresolved)``."""
    search_dirs = [including_file.parent, *incdir_paths, root]
    hits: list[Path] = []
    seen_resolved: set[Path] = set()
    for base in search_dirs:
        candidate = Path(inc_name) if os.path.isabs(inc_name) else base / inc_name
        if not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved not in seen_resolved:
            seen_resolved.add(resolved)
            hits.append(candidate)
    if not hits:
        return [], True, True
    return hits, len(hits) > 1, False


def _scan_file_deps(
    *,
    file_path: Path,
    file_key: str,
    root: Path,
    resolved_root: Path,
    incdir_paths: Sequence[Path],
    files_map: dict[str, dict[str, object]],
    deps_map: dict[str, dict[str, object]],
    prev_files: dict[str, object],
    dirty_hints: set[str],
    changed_set: set[str],
    visited: set[str],
) -> None:
    if file_key in visited:
        return
    visited.add(file_key)

    try:
        st = file_path.stat()
        raw_bytes = file_path.read_bytes()
    except OSError:
        changed_set.add(file_key)
        deps_map[file_key] = {
            "packages": [],
            "imports": [],
            "includes": [],
            "has_macros": False,
            "ambiguous": True,
            "untrackable": True,
        }
        return

    prev_entry = prev_files.get(file_key)
    sha1 = hashlib.sha1(raw_bytes).hexdigest()
    if not isinstance(prev_entry, dict) or prev_entry.get("sha1") != sha1:
        changed_set.add(file_key)

    files_map[file_key] = {
        "mtime": st.st_mtime,
        "size": st.st_size,
        "sha1": sha1,
    }

    raw_text = raw_bytes.decode("utf-8", errors="replace")

    cleaned = _strip_sv_comments(raw_text)
    packages = list(dict.fromkeys(m.group(1) for m in _PACKAGE_RE.finditer(cleaned)))
    imports = list(dict.fromkeys(m.group(1) for m in _IMPORT_RE.finditer(cleaned)))
    has_macros = bool(_MACRO_DEFINE_RE.search(cleaned))
    untrackable = bool(_INCLUDE_MACRO_RE.search(cleaned))
    ambiguous = untrackable

    resolved_includes: list[str] = []
    inc_targets: list[tuple[Path, str]] = []
    for inc_match in _INCLUDE_STR_RE.finditer(cleaned):
        inc_name = inc_match.group(1).strip()
        hits, inc_ambig, inc_unres = _resolve_include_target(
            inc_name, file_path, incdir_paths, root
        )
        if inc_ambig:
            ambiguous = True
        if inc_unres:
            untrackable = True
            continue
        for target in hits:
            inc_key = _normalize_key(root, resolved_root, target)
            if inc_key not in resolved_includes:
                resolved_includes.append(inc_key)
            inc_targets.append((target, inc_key))

    deps_map[file_key] = {
        "packages": packages,
        "imports": imports,
        "includes": resolved_includes,
        "has_macros": has_macros,
        "ambiguous": ambiguous,
        "untrackable": untrackable,
    }

    for target_path, target_key in inc_targets:
        _scan_file_deps(
            file_path=target_path,
            file_key=target_key,
            root=root,
            resolved_root=resolved_root,
            incdir_paths=incdir_paths,
            files_map=files_map,
            deps_map=deps_map,
            prev_files=prev_files,
            dirty_hints=dirty_hints,
            changed_set=changed_set,
            visited=visited,
        )


def _check_prev_deleted_or_changed(
    *,
    prev_files: dict[str, object],
    current_keys: set[str],
    root: Path,
) -> list[str]:
    extra_changed: list[str] = []
    for old_key in prev_files:
        if old_key in current_keys:
            continue
        candidate = Path(old_key) if os.path.isabs(old_key) else root / old_key
        if not candidate.is_file():
            extra_changed.append(old_key)
        else:
            st = candidate.stat()
            old_entry = prev_files.get(old_key)
            if (
                not isinstance(old_entry, dict)
                or old_entry.get("mtime") != st.st_mtime
                or old_entry.get("size") != st.st_size
                or _sha1_file(candidate) != old_entry.get("sha1")
            ):
                extra_changed.append(old_key)
    return extra_changed


def plan_rtl_incremental(
    config: Config,
    files: FileList,
    backend: Backend,
    provider: Provider,
) -> IncrementalPlan:
    """Compute RTL files needing recompilation and the next ``fast_state.json`` snapshot."""
    root = Path(os.path.normpath(Path(config.root).absolute()))
    resolved_root = root.resolve()
    rtl_files = list(files.groups.get("rtl", []))
    options_hash = compute_options_hash(config, files, backend, provider)
    state_path = root / FAST_STATE_REL
    prev_state = read_fast_state(state_path)

    prev_files_raw = prev_state.get("files") if prev_state is not None else None
    prev_deps_raw = prev_state.get("deps") if prev_state is not None else None
    prev_files: dict[str, object] = prev_files_raw if isinstance(prev_files_raw, dict) else {}
    prev_deps: dict[str, object] = prev_deps_raw if isinstance(prev_deps_raw, dict) else {}

    default_build = config.builds.get("default")
    incdir_strs = [
        *config.incdirs,
        *(default_build.incdirs if default_build else []),
        *files.incdirs.get("rtl", []),
    ]
    incdir_paths: list[Path] = []
    for raw_inc in incdir_strs:
        if "$" in raw_inc:
            continue
        inc_p = Path(raw_inc) if os.path.isabs(raw_inc) else root / raw_inc
        if inc_p.is_dir():
            incdir_paths.append(inc_p)

    dirty_hints = _git_dirty_paths(root)
    ordered_rtl = sorted(rtl_files, key=lambda p: 0 if p in dirty_hints else 1)

    new_files: dict[str, dict[str, object]] = {}
    new_deps: dict[str, dict[str, object]] = {}
    changed_set: set[str] = set()
    visited: set[str] = set()
    unreadable_rtl: list[str] = []

    for raw_rtl in ordered_rtl:
        resolved = _resolve_local_file(root, raw_rtl)
        if resolved is None:
            unreadable_rtl.append(raw_rtl)
            continue
        _scan_file_deps(
            file_path=resolved,
            file_key=raw_rtl,
            root=root,
            resolved_root=resolved_root,
            incdir_paths=incdir_paths,
            files_map=new_files,
            deps_map=new_deps,
            prev_files=prev_files,
            dirty_hints=dirty_hints,
            changed_set=changed_set,
            visited=visited,
        )

    new_state: dict[str, object] = {
        "schema": FAST_STATE_SCHEMA,
        "options_hash": options_hash,
        "files": new_files,
        "deps": new_deps,
    }

    if prev_state is None or not prev_files:
        return IncrementalPlan(
            files_to_compile=list(rtl_files),
            changed_files=list(rtl_files),
            full_fallback=True,
            fallback_reason="missing incremental baseline",
            new_state=new_state,
        )

    if prev_state.get("options_hash") != options_hash:
        return IncrementalPlan(
            files_to_compile=list(rtl_files),
            changed_files=sorted(changed_set),
            full_fallback=True,
            fallback_reason="compile options or RTL file list changed",
            new_state=new_state,
        )

    if unreadable_rtl:
        return IncrementalPlan(
            files_to_compile=list(rtl_files),
            changed_files=sorted(changed_set | set(unreadable_rtl)),
            full_fallback=True,
            fallback_reason=f"RTL file unreadable locally ({unreadable_rtl[0]})",
            new_state=new_state,
        )

    vanished_or_extra = _check_prev_deleted_or_changed(
        prev_files=prev_files,
        current_keys=set(new_files),
        root=root,
    )
    if vanished_or_extra:
        return IncrementalPlan(
            files_to_compile=list(rtl_files),
            changed_files=sorted(changed_set | set(vanished_or_extra)),
            full_fallback=True,
            fallback_reason=f"tracked header removed or changed ({vanished_or_extra[0]})",
            new_state=new_state,
        )

    for fkey, dep in new_deps.items():
        if dep.get("untrackable"):
            return IncrementalPlan(
                files_to_compile=list(rtl_files),
                changed_files=sorted(changed_set),
                full_fallback=True,
                fallback_reason=f"untrackable include in {fkey}",
                new_state=new_state,
            )

    if not changed_set:
        return IncrementalPlan(
            files_to_compile=[],
            changed_files=[],
            full_fallback=False,
            fallback_reason=None,
            new_state=new_state,
        )

    # Check fallback rules on changed files (macros or ambiguous headers)
    for cpath in sorted(changed_set):
        cur_dep = new_deps.get(cpath, {})
        prev_dep = prev_deps.get(cpath)
        prev_macros = isinstance(prev_dep, dict) and bool(prev_dep.get("has_macros"))
        prev_ambig = isinstance(prev_dep, dict) and bool(prev_dep.get("ambiguous"))
        if cur_dep.get("has_macros") or prev_macros:
            return IncrementalPlan(
                files_to_compile=list(rtl_files),
                changed_files=sorted(changed_set),
                full_fallback=True,
                fallback_reason=f"macro definitions changed in {cpath}",
                new_state=new_state,
            )
        if cur_dep.get("ambiguous") or prev_ambig:
            return IncrementalPlan(
                files_to_compile=list(rtl_files),
                changed_files=sorted(changed_set),
                full_fallback=True,
                fallback_reason=f"ambiguous include dependencies in {cpath}",
                new_state=new_state,
            )

    # Check if any file with ambiguous includes could depend on a changed header
    rtl_set = set(rtl_files)
    for fkey, dep in new_deps.items():
        if dep.get("ambiguous") and any(c not in rtl_set for c in changed_set):
            return IncrementalPlan(
                files_to_compile=list(rtl_files),
                changed_files=sorted(changed_set),
                full_fallback=True,
                fallback_reason=f"header changed with ambiguous or unresolved includes in {fkey}",
                new_state=new_state,
            )

    # Build reverse dependency maps across all scanned files
    includers_of: dict[str, set[str]] = {}
    importers_of: dict[str, set[str]] = {}
    for fkey, dep in new_deps.items():
        for inc_key in dep.get("includes", ()):  # type: ignore[attr-defined]
            includers_of.setdefault(str(inc_key), set()).add(fkey)
        for pkg_name in dep.get("imports", ()):  # type: ignore[attr-defined]
            importers_of.setdefault(str(pkg_name), set()).add(fkey)

    # If a non-RTL header changed and has no known includers, fall back to full compile
    for cpath in sorted(changed_set):
        if cpath not in rtl_set and not includers_of.get(cpath):
            return IncrementalPlan(
                files_to_compile=list(rtl_files),
                changed_files=sorted(changed_set),
                full_fallback=True,
                fallback_reason=f"header {cpath} has unknown dependents",
                new_state=new_state,
            )

    dirty_nodes: set[str] = set(changed_set)
    queue = sorted(changed_set)
    while queue:
        node = queue.pop(0)
        node_dep = new_deps.get(node, {})
        dependents: set[str] = set(includers_of.get(node, ()))
        for pkg_name in node_dep.get("packages", ()):  # type: ignore[attr-defined]
            dependents.update(importers_of.get(str(pkg_name), ()))
        prev_node_dep = prev_deps.get(node)
        if isinstance(prev_node_dep, dict):
            for pkg_name in prev_node_dep.get("packages", ()):
                dependents.update(importers_of.get(str(pkg_name), ()))
        for dep_file in sorted(dependents):
            if dep_file not in dirty_nodes:
                dirty_nodes.add(dep_file)
                queue.append(dep_file)

    ordered_dirty_rtl = [f for f in rtl_files if f in dirty_nodes]
    return IncrementalPlan(
        files_to_compile=ordered_dirty_rtl,
        changed_files=sorted(changed_set),
        full_fallback=False,
        fallback_reason=None,
        new_state=new_state,
    )


def _write_incremental_dotf(
    *,
    config: Config,
    files: FileList,
    files_to_compile: Sequence[str],
    expand_local: bool,
) -> Path:
    root = Path(os.path.normpath(Path(config.root).absolute()))
    resolved_root = root.resolve()
    check_dir = root / CHECK_REL_DIR
    check_dir.mkdir(parents=True, exist_ok=True)

    default_build = config.builds.get("default")
    outside_map = {entry.path: entry.outside_root for entry in files.entries}
    raw_lines: list[str] = []
    for define in [
        *config.defines,
        *(default_build.defines if default_build else []),
        *files.defines.get("rtl", []),
    ]:
        check_no_newlines(define, field="defines")
        raw_lines.append(f"+define+{define}")
    for incdir in [
        *config.incdirs,
        *(default_build.incdirs if default_build else []),
        *files.incdirs.get("rtl", []),
    ]:
        formatted = format_check_path(root, resolved_root, incdir, expand_local=expand_local)
        raw_lines.append(f"+incdir+{formatted}")
    for lib in files.libs.get("rtl", []):
        check_no_newlines(lib, field="files.libs['rtl']")
        if lib.startswith(("-v ", "-y ")):
            formatted = format_check_path(root, resolved_root, lib[3:], expand_local=expand_local)
            raw_lines.append(f"{lib[:3]}{formatted}")
        else:
            raw_lines.append(lib)
    for rtl_file in files_to_compile:
        raw_lines.append(
            format_check_path(
                root,
                resolved_root,
                rtl_file,
                expand_local=expand_local,
                outside=outside_map.get(rtl_file),
            )
        )

    seen_directives: set[str] = set()
    lines: list[str] = []
    for line in raw_lines:
        if line.startswith(("+define+", "+incdir+")):
            if line in seen_directives:
                continue
            seen_directives.add(line)
        lines.append(line)

    dotf_path = check_dir / "filelist.rtl.inc.f"
    dotf_path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")
    return dotf_path


def run_incremental_rtl(
    *,
    config: Config,
    files: FileList,
    backend: Backend,
    provider: Provider,
    where: str,
) -> IncrementalResult:
    """Run Layer 3 incremental RTL compilation for ``vcs`` / ``xrun``."""
    if backend.name not in INCREMENTAL_BACKENDS:
        return IncrementalResult(
            ok=True,
            skipped=True,
            warnings=[
                f"incremental RTL check is only supported for vcs/xrun "
                f"(got {backend.name!r}); skipping"
            ],
        )

    rtl_files = files.groups.get("rtl", [])
    if not rtl_files:
        return IncrementalResult(
            ok=True,
            skipped=True,
            warnings=["rtl file group is empty; skipping incremental RTL check"],
        )

    root = Path(os.path.normpath(Path(config.root).absolute()))
    plan = plan_rtl_incremental(config, files, backend, provider)
    warnings: list[str] = []
    if plan.full_fallback and plan.fallback_reason:
        warnings.append(f"incremental RTL fallback to full compile: {plan.fallback_reason}")

    if not plan.files_to_compile:
        return IncrementalResult(
            ok=True,
            skipped=False,
            files_compiled=[],
            full_fallback=False,
            duration_s=0.0,
            warnings=warnings,
        )

    _write_incremental_dotf(
        config=config,
        files=files,
        files_to_compile=plan.files_to_compile,
        expand_local=(where == "local"),
    )
    script_files = ensure_check_scripts(root, backend)
    default_build = config.builds.get("default")
    extra_opts = list(default_build.compile_opts) if default_build else []
    if backend.name == "vcs":
        cmd_tokens = ["vlogan", "-full64", "-sverilog", *extra_opts, "-f", "filelist.rtl.inc.f"]
    else:
        cmd_tokens = [
            "xrun",
            "-64bit",
            "-sv",
            "-compile",
            *extra_opts,
            "-f",
            "filelist.rtl.inc.f",
            "-xmlibdirpath",
            ".",
        ]
    primary_bin = cmd_tokens[0]

    push_files = [*script_files, f"{CHECK_REL_DIR}/filelist.rtl.inc.f"]
    rc, log_text, duration_s = run_check_job(
        root=root,
        provider=provider,
        where=where,
        cmd_tokens=cmd_tokens,
        log_name="check_rtl_inc.log",
        push_files=push_files,
    )

    diagnostics = parse_simulator_diagnostics(log_text, root=root, backend=backend)
    error_count = sum(1 for d in diagnostics if d.severity == "error")

    if rc != 0 and error_count == 0:
        if is_missing_binary_output(rc, log_text, primary_bin):
            raise InfraError(
                f"simulator {backend.name!r} command {primary_bin!r} is unavailable",
                code="CHECK_UNAVAILABLE",
                hint="ensure simulator is on PATH or configure provider.setup",
            )
        if any(re.search(pat, log_text) for pat in backend.license_regex):
            raise InfraError(
                f"simulator license failure while running incremental {backend.name!r} check",
                code="CHECK_UNAVAILABLE",
                hint="check EDA license availability or retry later",
            )
        non_empty = [ln.strip() for ln in log_text.splitlines() if ln.strip()]
        msg = (
            non_empty[-1]
            if non_empty
            else f"incremental {backend.name} compile failed with exit {rc}"
        )
        diagnostics.append(CheckDiagnostic(file="", line=0, col=0, severity="error", message=msg))
        error_count = 1

    ok = rc == 0 and error_count == 0
    if ok:
        write_fast_state(root / FAST_STATE_REL, plan.new_state)

    return IncrementalResult(
        ok=ok,
        skipped=False,
        files_compiled=plan.files_to_compile,
        full_fallback=plan.full_fallback,
        fallback_reason=plan.fallback_reason,
        duration_s=duration_s,
        diagnostics=diagnostics,
        warnings=warnings,
    )
