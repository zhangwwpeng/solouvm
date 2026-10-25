"""Collecting user data: ``filelist.py`` (sources) and ``case.py`` (test cases)."""

from __future__ import annotations

import glob
import runpy
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .config import Config
from .errors import ConfigError
from .model import Case
from .util import relpath


def load_filelist(cfg: Config) -> list[str]:
    """Return project-relative source paths, globs already expanded."""
    path = cfg.filelist_file
    if not path.is_file():
        raise ConfigError(
            f"filelist module not found: {path}",
            hint="create filelist.py returning a list of paths",
        )
    payload = _load_module_list(path, "FILES", "files")
    entries = _flatten_str(payload, source=path)

    resolved: list[str] = []
    missing: list[str] = []
    for entry in entries:
        expanded = _expand_entry(cfg.root, entry)
        if not expanded:
            missing.append(entry)
            continue
        resolved.extend(relpath(Path(item), cfg.root) for item in expanded)

    if missing:
        raise ConfigError(
            "filelist entries match no files: " + ", ".join(sorted(set(missing))),
            hint="paths and globs are resolved relative to the project root",
        )
    return _dedupe(resolved)


def load_cases(cfg: Config) -> list[Case]:
    """Return the declared cases, project defaults applied."""
    path = cfg.case_file
    if not path.is_file():
        raise ConfigError(
            f"case module not found: {path}",
            hint="create case.py returning a list of solouvm.Case objects",
        )
    payload = _load_module_list(path, "CASES", "cases")
    if not isinstance(payload, (list, tuple)):
        raise ConfigError(f"{path} must provide a list of cases, got {type(payload).__name__}")

    defaults = cfg.case_defaults()
    cases: list[Case] = []
    seen: dict[str, int] = {}
    for item in payload:
        case = _to_case(item, path)
        if case.name in seen:
            raise ConfigError(
                f"{path}: duplicate case name {case.name!r}",
                hint="case names must be unique, they become case_id prefixes",
            )
        seen[case.name] = 1
        cases.append(case.with_defaults(defaults))
    return cases


def _to_case(item: Any, source: Path) -> Case:
    if isinstance(item, Case):
        return item
    if isinstance(item, dict):
        try:
            return Case(**item)
        except TypeError as exc:
            raise ConfigError(f"{source}: invalid case dict {item!r}: {exc}") from None
    if isinstance(item, str):
        return Case(name=item)
    raise ConfigError(
        f"{source}: expected Case (or dict), got {type(item).__name__}",
        hint="write case.py returning [Case(name='smoke'), ...]",
    )


def _load_module_list(path: Path, attr: str, func: str) -> Any:
    namespace = _run_module(path)
    if attr in namespace:
        return namespace[attr]
    if func in namespace and callable(namespace[func]):
        value = namespace[func]()
        return value
    raise ConfigError(
        f"{path} must define {attr} = [...] or a {func}() function returning a list",
        hint="the module-level list is the documented interface",
    )


def _run_module(path: Path) -> dict[str, Any]:
    previous = list(sys.path)
    sys.path.insert(0, str(path.parent))
    try:
        return runpy.run_path(str(path), run_name=f"solouvm_user_{path.stem}")
    except ConfigError:
        raise
    except Exception as exc:  # pragma: no cover - surfaced verbatim
        raise ConfigError(f"failed to load {path}: {type(exc).__name__}: {exc}") from exc
    finally:
        sys.path[:] = previous


def _flatten_str(payload: Any, *, source: Path) -> list[str]:
    if isinstance(payload, str):
        raise ConfigError(f"{source} must return a list of paths, not a single string")
    if not isinstance(payload, (list, tuple)):
        raise ConfigError(f"{source} must return a list, got {type(payload).__name__}")
    out: list[str] = []
    for item in payload:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{source}: invalid file entry {item!r}")
        out.append(item.strip())
    return out


def _expand_entry(root: Path, entry: str) -> list[str]:
    pattern = entry if Path(entry).is_absolute() else str(root / entry)
    if any(ch in entry for ch in "*?["):
        return sorted(glob.glob(pattern, recursive=True))
    candidate = Path(pattern)
    return [str(candidate)] if candidate.is_file() else []


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out
