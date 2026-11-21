"""Collect the ordered, grouped compile file list from ``filelist.py`` (config.md §4)."""

from __future__ import annotations

import contextlib
import glob
import os
import re
import runpy
import shlex
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from solouvm.collect.dotf import join_base, walk_dotf
from solouvm.errors import ConfigError

GROUPS = ("rtl", "tb", "top")  # 合并清单按此顺序拼接（F-04）
FILELIST_NAME = "filelist.py"

_MAGIC_RE = re.compile(r"[*?[]")


@dataclass
class FileEntry:
    path: str
    group: str
    source: str  # 来自哪个条目或哪个 .f，供 solouvm files 显示（F-10）
    outside_root: bool = False  # 根外绝对路径：B 上必须存在相同路径（F-07）


@dataclass
class FileList:
    groups: dict[str, list[str]]
    incdirs: dict[str, list[str]]
    defines: dict[str, list[str]]
    entries: list[FileEntry]
    # -v / -y / +libext+：M1 只记录不展开，每项是一条完整的 .f 写法，如 "-v lib.v"
    libs: dict[str, list[str]] = field(default_factory=dict)

    @property
    def files(self) -> list[str]:
        """All files in rtl -> tb -> top order (the merged ``filelist.f``)."""
        return [path for group in GROUPS for path in self.groups.get(group, [])]

    @property
    def outside_root(self) -> list[FileEntry]:
        return [entry for entry in self.entries if entry.outside_root]


def load_filelist(root: Path) -> dict | list:
    """Execute ``<root>/filelist.py`` and return ``files()`` or ``FILES``."""
    path = Path(root) / FILELIST_NAME
    if not path.is_file():
        raise ConfigError(f"{FILELIST_NAME} not found in {root}", code="FILES_MISSING")
    sys.path.insert(0, str(path.parent))
    try:
        namespace = runpy.run_path(str(path), run_name="__solouvm_filelist__")
        func = namespace.get("files")
        if callable(func):
            return func()
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        last = traceback.format_exception_only(type(exc), exc)[-1].strip()
        raise ConfigError(
            f"{FILELIST_NAME} raised an exception: {last}", code="CFG_LOAD_FAILED", file=path
        ) from exc
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(str(path.parent))
    if "FILES" not in namespace:
        raise ConfigError(
            f"{FILELIST_NAME} must define FILES or files()", code="CFG_LOAD_FAILED", file=path
        )
    return namespace["FILES"]


def collect_files(root: Path, spec: dict | list) -> FileList:
    """Expand ``spec`` (``FILES``) into a :class:`FileList`; paths are relative to ``root``."""
    return _Collector(Path(root)).run(spec)


class _Collector:
    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.normpath(root.absolute()))
        self.result = FileList(
            groups={g: [] for g in GROUPS},
            incdirs={g: [] for g in GROUPS},
            defines={g: [] for g in GROUPS},
            entries=[],
            libs={g: [] for g in GROUPS},
        )
        self.seen: set[str] = set()
        self.missing: list[str] = []

    def run(self, spec: dict | list) -> FileList:
        for group, entries in _groups(spec).items():
            for entry in entries:
                self._entry(group, entry)
        if self.missing:
            raise ConfigError(
                "file list entries matched nothing:\n" + "\n".join(self.missing),
                code="FILES_MISSING",
            )
        return self.result

    # ------------------------------------------------------------ filelist.py 里的一个条目

    def _entry(self, group: str, entry: object) -> None:
        if not isinstance(entry, str) or not entry.strip():
            raise ConfigError(
                f"FILES[{group!r}]: entries must be non-empty strings, got {entry!r}",
                code="CFG_INVALID",
            )
        try:
            tokens = shlex.split(entry)
        except ValueError as exc:
            raise ConfigError(f"FILES[{group!r}]: cannot parse {entry!r}: {exc}") from None
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if token in ("-f", "-F", "-v", "-y"):
                if i + 1 >= len(tokens):
                    raise ConfigError(
                        f"FILES[{group!r}]: option {token} in {entry!r} needs an argument"
                    )
                arg = join_base(self.root, tokens[i + 1])
                i += 2
                if token in ("-v", "-y"):
                    self._lib(group, f"{token} {self._norm(arg)[0]}")
                else:
                    self._dotf(group, entry, Path(arg), relative_to_dotf=token == "-F")
                continue
            i += 1
            if token.startswith(("+incdir+", "+define+", "+libext+")):
                self._plus(group, token, self.root)
            elif token.startswith(("-", "+")):
                raise ConfigError(f"FILES[{group!r}]: unsupported option {token!r} in {entry!r}")
            else:
                self._path(group, token, source=entry, label=f"{group}: {entry}", base=self.root)

    def _dotf(self, group: str, entry: str, path: Path, *, relative_to_dotf: bool) -> None:
        try:
            items = walk_dotf(path, relative_to_dotf=relative_to_dotf)
        except ConfigError as exc:
            if exc.code != "FILES_MISSING":
                raise
            self.missing.append(f"{group}: {entry} ({exc.message})")
            return
        i = 0
        while i < len(items):
            token, dotf, joined = items[i]
            source = self._norm(str(dotf))[0]
            if token in ("-v", "-y"):
                self._lib(group, f"{token} {self._norm(items[i + 1][0])[0]}")
                i += 2
                continue
            i += 1
            if token.startswith("+"):
                # walk_dotf 已把 +incdir+ 的目录拼好了基准目录
                self._plus(group, token, None)
            else:
                self._path(
                    group,
                    token,
                    source=source,
                    label=f"{group}: {token} (from {source})",
                    written_absolute=not joined,
                )

    # ------------------------------------------------------------ 分类

    def _plus(self, group: str, token: str, base: Path | None) -> None:
        if token.startswith("+incdir+"):
            for item in filter(None, token.removeprefix("+incdir+").split("+")):
                path = join_base(base, item) if base is not None else item
                _append_unique(self.result.incdirs[group], self._norm(path)[0])
        elif token.startswith("+define+"):
            for item in filter(None, token.removeprefix("+define+").split("+")):
                _append_unique(self.result.defines[group], item)
        else:
            self._lib(group, token)

    def _lib(self, group: str, item: str) -> None:
        _append_unique(self.result.libs[group], item)

    def _path(
        self,
        group: str,
        token: str,
        *,
        source: str,
        label: str,
        base: Path | None = None,
        written_absolute: bool | None = None,
    ) -> None:
        if "$" in token:
            # 环境变量原样保留，由 B 上的仿真器展开；A 无法检查是否存在
            self._add(group, token, source, outside=False)
            return
        if _MAGIC_RE.search(token):
            if base is not None and not os.path.isabs(token):
                pattern = os.path.join(glob.escape(str(base)), token)
            else:
                pattern = token
            hits = [h for h in glob.glob(pattern, recursive=True) if os.path.isfile(h)]
            if not hits:
                self.missing.append(label)
                return
            # glob 顺序不保证：按完整相对路径排序（N-11）
            for path, outside in sorted(self._norm(h) for h in hits):
                self._add(group, path, source, outside=outside)
            return
        full = join_base(base, token) if base is not None else token
        path, outside = self._norm(full)
        if written_absolute is None:
            written_absolute = os.path.isabs(token)
        # 用户直接写的根外绝对路径要求 B 上存在，A 上不一定有，不检查（F-07）
        if not (outside and written_absolute) and not os.path.isfile(full):
            self.missing.append(label)
            return
        self._add(group, path, source, outside=outside)

    def _add(self, group: str, path: str, source: str, *, outside: bool) -> None:
        if path in self.seen:
            return  # 同一文件只保留第一次（连同第一次的 source）
        self.seen.add(path)
        self.result.groups[group].append(path)
        self.result.entries.append(FileEntry(path, group, source, outside))

    def _norm(self, raw: str) -> tuple[str, bool]:
        """Return (path, outside_root): in-root paths become root-relative POSIX paths."""
        if "$" in raw:
            return raw, False
        path = Path(os.path.normpath(raw if os.path.isabs(raw) else self.root / raw))
        try:
            return path.relative_to(self.root).as_posix(), False
        except ValueError:
            pass
        try:
            # 符号链接（如 macOS 的 /var -> /private/var）展开后再判断一次
            return path.resolve().relative_to(self.root.resolve()).as_posix(), False
        except ValueError:
            return str(path), True


def _groups(spec: object) -> dict[str, list]:
    if isinstance(spec, (list, tuple)):
        return {"rtl": list(spec)}
    if not isinstance(spec, dict):
        raise ConfigError(
            f"FILES must be a dict of groups or a list, got {type(spec).__name__}",
            code="CFG_INVALID",
        )
    bad = [g for g in spec if g not in GROUPS]
    if bad:
        raise ConfigError(
            f"unknown FILES group(s): {', '.join(map(repr, bad))}; "
            f"valid groups: {', '.join(GROUPS)}",
            code="CFG_INVALID",
        )
    out: dict[str, list] = {}
    for group in GROUPS:
        entries = spec.get(group, [])
        if isinstance(entries, str) or not isinstance(entries, (list, tuple)):
            raise ConfigError(
                f"FILES[{group!r}] must be a list of strings, got {entries!r}", code="CFG_INVALID"
            )
        out[group] = list(entries)
    return out


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)
