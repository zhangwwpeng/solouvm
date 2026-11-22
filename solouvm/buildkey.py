"""Build-key calculation and mtime/size/sha1 file cache (architecture.md §4.2, D-05, D-06)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.collect.files import GROUPS
from solouvm.errors import ConfigError

if TYPE_CHECKING:
    from solouvm.backends.base import Backend
    from solouvm.collect.files import FileList
    from solouvm.config.schema import Config

__all__ = [
    "BUILDKEY_LEN",
    "BuildKeyResult",
    "compute_build_key",
    "compute_build_keys",
    "strip_sv_comments",
]

BUILDKEY_LEN = 12
_CACHE_SCHEMA = 1
_CACHE_REL = Path(".solouvm") / "cache" / "hash.json"
_CHUNK_SIZE = 65536
_SHA1_HEX_LEN = 40
_SV_TOKEN_RE = re.compile(r"\x22(?:\x5c.|[^\x22\x5c\n])*\x22|//[^\n]*|/\*[\s\S]*?\*/")
_SV_INCLUDE_RE = re.compile(r"\x60\s*include\s*\x22([^\x22\r\n]+)\x22")


def strip_sv_comments(text: str, *, blank_strings: bool = False) -> str:
    """Strip ``//`` and ``/* ... */`` comments while respecting SV string literals."""

    def _repl(match: re.Match[str]) -> str:
        tok = match.group(0)
        if tok.startswith('"'):
            return '""' if blank_strings else tok
        nl_count = tok.count("\n")
        return "\n" * nl_count if nl_count else " "

    return _SV_TOKEN_RE.sub(_repl, text)


def _scan_sv_includes(path: Path) -> list[str]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    stripped = strip_sv_comments(raw, blank_strings=False)
    return [m.group(1).strip() for m in _SV_INCLUDE_RE.finditer(stripped) if m.group(1).strip()]


@dataclass
class BuildKeyResult:
    """Computed 12-char build keys per mode and any non-reusable reasons."""

    keys: dict[str, str]
    non_reusable: dict[str, list[str]]


class _HashCache:
    """Persistent stat-keyed sha1 cache under ``.solouvm/cache/hash.json``."""

    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.normpath(root.absolute()))
        self.resolved_root = self.root.resolve()
        self.path = self.root / _CACHE_REL
        self.entries: dict[str, dict[str, object]] = {}
        self.dirty = False
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            self.dirty = True
            return
        if not isinstance(data, dict) or data.get("schema") != _CACHE_SCHEMA:
            self.dirty = True
            return
        raw_entries = data.get("entries")
        if not isinstance(raw_entries, dict):
            self.dirty = True
            return
        for key, entry in raw_entries.items():
            if isinstance(key, str) and _is_valid_entry(entry):
                self.entries[key] = entry
            else:
                self.dirty = True

    def _cache_key(self, target: Path) -> str:
        norm = Path(os.path.normpath(target.absolute()))
        try:
            return norm.relative_to(self.root).as_posix()
        except ValueError:
            pass
        try:
            return norm.resolve().relative_to(self.resolved_root).as_posix()
        except ValueError:
            return str(norm)

    def file_sha1(self, target: Path) -> str:
        st = target.stat()
        key = self._cache_key(target)
        cached = self.entries.get(key)
        if (
            cached is not None
            and cached["mtime_ns"] == st.st_mtime_ns
            and cached["ctime_ns"] == st.st_ctime_ns
            and cached["size"] == st.st_size
            and cached["ino"] == st.st_ino
        ):
            return str(cached["sha1"])
        digest = _sha1_file(target)
        self.entries[key] = {
            "mtime_ns": st.st_mtime_ns,
            "ctime_ns": st.st_ctime_ns,
            "size": st.st_size,
            "ino": st.st_ino,
            "sha1": digest,
        }
        self.dirty = True
        return digest

    def save(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"schema": _CACHE_SCHEMA, "entries": self.entries},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent),
            prefix=".hash.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload + "\n")
            os.replace(tmp_name, self.path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        self.dirty = False


def _is_valid_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    mtime = entry.get("mtime_ns")
    ctime = entry.get("ctime_ns")
    size = entry.get("size")
    ino = entry.get("ino")
    sha1 = entry.get("sha1")
    return (
        isinstance(mtime, int)
        and not isinstance(mtime, bool)
        and isinstance(ctime, int)
        and not isinstance(ctime, bool)
        and isinstance(size, int)
        and not isinstance(size, bool)
        and isinstance(ino, int)
        and not isinstance(ino, bool)
        and isinstance(sha1, str)
        and len(sha1) == _SHA1_HEX_LEN
    )


def _sha1_file(path: Path) -> str:
    hasher = hashlib.sha1()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def _feed(hasher: hashlib._Hash, *parts: object) -> None:
    line = json.dumps(parts, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    hasher.update(line.encode("utf-8") + b"\n")


def _resolve_path(root: Path, resolved_root: Path, raw: str) -> tuple[Path, bool]:
    norm_root = Path(os.path.normpath(root.absolute()))
    path = Path(os.path.normpath(raw if os.path.isabs(raw) else norm_root / raw))
    try:
        path.relative_to(norm_root)
    except ValueError:
        pass
    else:
        return path, False
    try:
        path.resolve().relative_to(resolved_root)
    except ValueError:
        return path, True
    else:
        return path, False


def _iter_dir_files(directory: Path) -> list[tuple[str, Path]]:
    hits: list[tuple[str, Path]] = []
    for dirpath, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames.sort()
        base = Path(dirpath)
        for name in sorted(filenames):
            full = base / name
            if full.is_file():
                rel = full.relative_to(directory).as_posix()
                hits.append((rel, full))
    hits.sort(key=lambda item: item[0])
    return hits


def _hash_directory(
    hasher: hashlib._Hash,
    cache: _HashCache,
    reasons: list[str],
    *,
    tag: str,
    label: str,
    directory: Path,
) -> None:
    try:
        entries = _iter_dir_files(directory)
    except OSError as exc:
        reasons.append(f"unreadable {label} on A ({exc})")
        return
    for rel, full in entries:
        try:
            digest = cache.file_sha1(full)
        except OSError as exc:
            reasons.append(f"unreadable file in {label} on A ({rel}: {exc})")
            continue
        _feed(hasher, tag, label, rel, digest)


def _dedupe_preserve(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _resolve_include(
    inc_name: str,
    including_file: Path,
    incdir_paths: list[Path],
    root: Path,
) -> Path | None:
    if os.path.isabs(inc_name):
        cand = Path(os.path.normpath(inc_name))
        return cand if cand.is_file() else None
    for base in (including_file.parent, *incdir_paths, root):
        cand = Path(os.path.normpath(base / inc_name))
        if cand.is_file():
            return cand
    return None


def compute_build_keys(
    *,
    config: Config,
    files: FileList,
    backend: Backend,
    modes: Iterable[str],
    run_id: str,
    sim_version: str | None = None,
    solouvm_version: str = "0.1.0",
    provider: str | None = None,
    provider_setup: str | None = None,
    save_cache: bool = True,
) -> BuildKeyResult:
    """Compute 12-char sha1 build keys for ``modes`` and cache file hashes on A."""
    mode_list = sorted(set(modes))
    for mode in mode_list:
        if mode not in config.builds:
            raise ConfigError(f"unknown build mode {mode!r}", code="CFG_INVALID")

    root = Path(os.path.normpath(Path(config.root).absolute()))
    resolved_root = root.resolve()
    cache = _HashCache(root)

    if provider_setup is None:
        active_provider = provider if provider is not None else config.provider
        provider_cfg = config.providers.get(active_provider)
        eff_provider_setup = provider_cfg.setup if provider_cfg is not None else ""
    else:
        eff_provider_setup = provider_setup

    all_groups = list(GROUPS) + sorted(set(files.groups) - set(GROUPS))
    outside_map = {entry.path: entry.outside_root for entry in files.entries}

    keys: dict[str, str] = {}
    non_reusable: dict[str, list[str]] = {}
    includes_cache: dict[Path, list[str]] = {}

    def _includes_of(path: Path) -> list[str]:
        if path not in includes_cache:
            includes_cache[path] = _scan_sv_includes(path)
        return includes_cache[path]

    for mode in mode_list:
        build_cfg = config.builds[mode]
        hasher = hashlib.sha1()
        reasons: list[str] = []
        source_paths: list[Path] = []

        _feed(
            hasher,
            "meta",
            {
                "mode": mode,
                "backend_name": backend.name,
                "backend_build": backend.build,
                "backend_cov_build": backend.cov_build if build_cfg.coverage else "",
                "top": config.top,
                "global_defines": list(config.defines),
                "global_incdirs": list(config.incdirs),
                "mode_defines": list(build_cfg.defines),
                "mode_incdirs": list(build_cfg.incdirs),
                "compile_opts": list(build_cfg.compile_opts),
                "coverage": build_cfg.coverage,
                "provider_setup": eff_provider_setup,
                "sim_version": sim_version or "",
                "solouvm_version": solouvm_version,
            },
        )

        for group in all_groups:
            _feed(
                hasher,
                "filelist_group",
                group,
                files.groups.get(group, []),
                files.incdirs.get(group, []),
                files.defines.get(group, []),
                files.libs.get(group, []),
            )

        for raw_file in files.files:
            if "$" in raw_file:
                reasons.append(f"file path uses B-side env var: {raw_file}")
                continue
            full, is_outside = _resolve_path(root, resolved_root, raw_file)
            if outside_map.get(raw_file, False) or (is_outside and os.path.isabs(raw_file)):
                reasons.append(f"file is outside project root: {raw_file}")
                if full.is_file():
                    source_paths.append(full)
                    with contextlib.suppress(OSError):
                        _feed(hasher, "file", raw_file, cache.file_sha1(full))
                continue
            if not full.is_file():
                reasons.append(f"file not found on A: {raw_file}")
                continue
            source_paths.append(full)
            try:
                _feed(hasher, "file", raw_file, cache.file_sha1(full))
            except OSError as exc:
                reasons.append(f"unreadable file on A ({raw_file}: {exc})")

        incdirs = _dedupe_preserve(
            [
                *config.incdirs,
                *build_cfg.incdirs,
                *(d for g in all_groups for d in files.incdirs.get(g, [])),
            ]
        )
        incdir_paths: list[Path] = []
        for incdir in incdirs:
            if "$" in incdir:
                reasons.append(f"incdir uses B-side env var: {incdir}")
                continue
            full, is_outside = _resolve_path(root, resolved_root, incdir)
            if is_outside and os.path.isabs(incdir):
                reasons.append(f"incdir is outside project root: {incdir}")
                if full.is_dir():
                    incdir_paths.append(full)
                continue
            if not full.is_dir():
                reasons.append(f"incdir not found on A: {incdir}")
                continue
            incdir_paths.append(full)
            _hash_directory(hasher, cache, reasons, tag="incdir_file", label=incdir, directory=full)

        for group in all_groups:
            for item in files.libs.get(group, []):
                if item.startswith("-v "):
                    target = item[3:]
                    if "$" in target:
                        reasons.append(f"-v library uses B-side env var: {target}")
                        continue
                    full, is_outside = _resolve_path(root, resolved_root, target)
                    if is_outside and os.path.isabs(target):
                        reasons.append(f"-v library is outside project root: {target}")
                        continue
                    if not full.is_file():
                        reasons.append(f"-v library not found on A: {target}")
                        continue
                    source_paths.append(full)
                    try:
                        _feed(hasher, "lib_v", target, cache.file_sha1(full))
                    except OSError as exc:
                        reasons.append(f"unreadable -v library on A ({target}: {exc})")
                elif item.startswith("-y "):
                    target = item[3:]
                    if "$" in target:
                        reasons.append(f"-y directory uses B-side env var: {target}")
                        continue
                    full, is_outside = _resolve_path(root, resolved_root, target)
                    if is_outside and os.path.isabs(target):
                        reasons.append(f"-y directory is outside project root: {target}")
                        continue
                    if not full.is_dir():
                        reasons.append(f"-y directory not found on A: {target}")
                        continue
                    _hash_directory(
                        hasher, cache, reasons, tag="lib_y_file", label=target, directory=full
                    )

        # Recursively hash SV `include "..." headers referenced by source files
        seen_includes: set[Path] = set()
        queue: list[Path] = list(source_paths)
        resolved_inc_digests: list[tuple[str, str]] = []
        while queue:
            src_file = queue.pop(0)
            for inc_name in _includes_of(src_file):
                if "$" in inc_name:
                    reasons.append(f"include path uses B-side env var: {inc_name}")
                    continue
                inc_full = _resolve_include(inc_name, src_file, incdir_paths, root)
                if inc_full is None or inc_full in seen_includes:
                    continue
                seen_includes.add(inc_full)
                queue.append(inc_full)
                _, inc_outside = _resolve_path(root, resolved_root, str(inc_full))
                if inc_outside and os.path.isabs(inc_name):
                    reasons.append(f"included file is outside project root: {inc_name}")
                try:
                    digest = cache.file_sha1(inc_full)
                except OSError as exc:
                    reasons.append(f"unreadable included file on A ({inc_name}: {exc})")
                    continue
                resolved_inc_digests.append((cache._cache_key(inc_full), digest))
        for inc_key, digest in sorted(resolved_inc_digests):
            _feed(hasher, "include_file", inc_key, digest)

        if reasons:
            unique_reasons = _dedupe_preserve(reasons)
            non_reusable[mode] = unique_reasons
            _feed(hasher, "non_reusable_run_id", run_id)

        keys[mode] = hasher.hexdigest()[:BUILDKEY_LEN]

    if save_cache:
        cache.save()
    return BuildKeyResult(keys=keys, non_reusable=non_reusable)


def compute_build_key(
    *,
    config: Config,
    files: FileList,
    backend: Backend,
    mode: str,
    run_id: str,
    sim_version: str | None = None,
    solouvm_version: str = "0.1.0",
    provider: str | None = None,
    provider_setup: str | None = None,
) -> str:
    """Convenience wrapper returning the single build key for ``mode``."""
    return compute_build_keys(
        config=config,
        files=files,
        backend=backend,
        modes=[mode],
        run_id=run_id,
        sim_version=sim_version,
        solouvm_version=solouvm_version,
        provider=provider,
        provider_setup=provider_setup,
    ).keys[mode]
