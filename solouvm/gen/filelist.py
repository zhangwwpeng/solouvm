"""Generate grouped and merged ``.f`` filelists relative to build dirs and root (F-04, N-05)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.collect.files import GROUPS
from solouvm.gen.shell import check_no_newlines, relpath

if TYPE_CHECKING:
    from solouvm.collect.files import FileList
    from solouvm.config.schema import Config
    from solouvm.model import Plan

__all__ = ["write_filelists"]


def _resolve_in_root(root: Path, resolved_root: Path, raw: str) -> tuple[str, bool]:
    norm_root = Path(os.path.normpath(root.absolute()))
    path = Path(os.path.normpath(raw if os.path.isabs(raw) else norm_root / raw))
    try:
        return path.relative_to(norm_root).as_posix(), False
    except ValueError:
        pass
    try:
        return path.resolve().relative_to(resolved_root).as_posix(), False
    except ValueError:
        return raw, True


def _format_path(
    root: Path,
    resolved_root: Path,
    raw: str,
    base_rel: str,
    *,
    outside: bool | None = None,
) -> str:
    check_no_newlines(raw, field="filelist path")
    if "$" in raw or outside is True:
        return raw
    root_rel, is_outside = _resolve_in_root(root, resolved_root, raw)
    if is_outside:
        return raw
    if base_rel == ".":
        return root_rel
    return relpath(root_rel, base_rel)


def _dedupe_directives(lines: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        if line.startswith(("+define+", "+incdir+")):
            if line in seen:
                continue
            seen.add(line)
        out.append(line)
    return out


def _build_lines(
    *,
    root: Path,
    resolved_root: Path,
    files: FileList,
    base_rel: str,
    extra_defines: list[str],
    extra_incdirs: list[str],
) -> tuple[list[str], dict[str, list[str]]]:
    outside_map = {entry.path: entry.outside_root for entry in files.entries}
    global_lines: list[str] = []
    for define in extra_defines:
        check_no_newlines(define, field="defines")
        global_lines.append(f"+define+{define}")
    for incdir in extra_incdirs:
        formatted = _format_path(root, resolved_root, incdir, base_rel)
        global_lines.append(f"+incdir+{formatted}")

    per_group: dict[str, list[str]] = {}
    merged: list[str] = [*global_lines]

    for group in GROUPS:
        group_raw: list[str] = []
        for define in files.defines.get(group, []):
            check_no_newlines(define, field=f"files.defines[{group!r}]")
            group_raw.append(f"+define+{define}")
        for incdir in files.incdirs.get(group, []):
            formatted = _format_path(root, resolved_root, incdir, base_rel)
            group_raw.append(f"+incdir+{formatted}")
        for lib in files.libs.get(group, []):
            check_no_newlines(lib, field=f"files.libs[{group!r}]")
            if lib.startswith("-v "):
                formatted = _format_path(root, resolved_root, lib[3:], base_rel)
                group_raw.append(f"-v {formatted}")
            elif lib.startswith("-y "):
                formatted = _format_path(root, resolved_root, lib[3:], base_rel)
                group_raw.append(f"-y {formatted}")
            else:
                group_raw.append(lib)
        for file_path in files.groups.get(group, []):
            group_raw.append(
                _format_path(
                    root,
                    resolved_root,
                    file_path,
                    base_rel,
                    outside=outside_map.get(file_path),
                )
            )
        merged.extend(group_raw)
        if group_raw and global_lines:
            per_group[group] = _dedupe_directives([*global_lines, *group_raw])
        else:
            per_group[group] = _dedupe_directives(group_raw)

    return _dedupe_directives(merged), per_group


def _write_set(
    *,
    root: Path,
    out_rel_dir: str,
    merged_lines: list[str],
    group_lines: dict[str, list[str]],
) -> list[str]:
    out_dir = root / out_rel_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    targets = [("filelist.f", merged_lines)] + [
        (f"filelist.{group}.f", group_lines[group]) for group in GROUPS
    ]
    for filename, lines in targets:
        text = "\n".join(lines) + "\n" if lines else ""
        (out_dir / filename).write_text(text, encoding="utf-8")
        written.append(f"{out_rel_dir}/{filename}")
    return written


def write_filelists(
    root: Path,
    plan: Plan,
    files: FileList,
    build_dirs: dict[str, str],
    *,
    config: Config | None = None,
) -> list[str]:
    """Write ``filelist.f`` and ``filelist.<group>.f`` into each build dir and ``.solouvm/gen``."""
    _ = plan  # retained in signature per architecture/task-13 API
    root_path = Path(os.path.normpath(Path(root).absolute()))
    resolved_root = root_path.resolve()
    written: list[str] = []

    global_defines = list(config.defines) if config is not None else []
    global_incdirs = list(config.incdirs) if config is not None else []

    for mode, rel_build_dir in sorted(build_dirs.items()):
        norm_build_rel = rel_build_dir.replace(os.sep, "/").strip("/")
        build_cfg = config.builds.get(mode) if config is not None else None
        mode_defines = [*global_defines, *(build_cfg.defines if build_cfg is not None else [])]
        mode_incdirs = [*global_incdirs, *(build_cfg.incdirs if build_cfg is not None else [])]
        merged_lines, group_lines = _build_lines(
            root=root_path,
            resolved_root=resolved_root,
            files=files,
            base_rel=norm_build_rel,
            extra_defines=mode_defines,
            extra_incdirs=mode_incdirs,
        )
        written.extend(
            _write_set(
                root=root_path,
                out_rel_dir=norm_build_rel,
                merged_lines=merged_lines,
                group_lines=group_lines,
            )
        )

    gen_merged, gen_groups = _build_lines(
        root=root_path,
        resolved_root=resolved_root,
        files=files,
        base_rel=".",
        extra_defines=global_defines,
        extra_incdirs=global_incdirs,
    )
    written.extend(
        _write_set(
            root=root_path,
            out_rel_dir=".solouvm/gen",
            merged_lines=gen_merged,
            group_lines=gen_groups,
        )
    )
    return sorted(written)
