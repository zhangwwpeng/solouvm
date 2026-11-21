"""Collect ``Case`` and ``Suite`` definitions from ``case.py`` and ``case_dirs`` (config.md §5)."""

from __future__ import annotations

import ast
import contextlib
import runpy
import sys
import traceback
from collections import defaultdict
from dataclasses import dataclass
from difflib import get_close_matches
from pathlib import Path

from solouvm.config.schema import Config
from solouvm.errors import ConfigError
from solouvm.model import Case, Suite, validate_records

CASE_FILE_NAME = "case.py"

__all__ = [
    "CaseSource",
    "case_file_for",
    "collect_cases",
    "collect_cases_with_sources",
    "find_case_line",
]


@dataclass
class CaseSource:
    case: Case
    file: Path
    line: int | None


def _is_case_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "Case") or (
        isinstance(func, ast.Attribute) and func.attr == "Case"
    )


def _case_call_name(node: ast.Call) -> str | None:
    for kw in node.keywords:
        if (
            kw.arg == "name"
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            return kw.value.value
    if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def _collect_case_lines(source: str) -> dict[str, list[int]]:
    lines: dict[str, list[int]] = defaultdict(list)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return lines
    calls = [node for node in ast.walk(tree) if _is_case_call(node)]
    calls.sort(key=lambda node: (node.lineno, node.col_offset))
    for call in calls:
        name = _case_call_name(call)
        if name is not None:
            lines[name].append(call.lineno)
    return lines


def find_case_line(source: str, name: str) -> int | None:
    """Return the 1-based line number of ``Case(name=...)`` in ``source``, or ``None``."""
    hits = _collect_case_lines(source).get(name)
    return hits[0] if hits else None


def _user_line(exc: BaseException, path: Path) -> int | None:
    resolved = path.resolve()
    if (
        isinstance(exc, SyntaxError)
        and exc.lineno
        and exc.filename
        and (exc.filename == str(path) or Path(exc.filename).resolve() == resolved)
    ):
        return exc.lineno
    line = None
    for frame in traceback.extract_tb(exc.__traceback__):
        if frame.filename == str(path) or Path(frame.filename).resolve() == resolved:
            line = frame.lineno
    return line


def _case_files(config: Config) -> list[Path]:
    root = Path(config.root)
    candidates: list[Path] = []
    case_py = root / CASE_FILE_NAME
    if case_py.is_file():
        candidates.append(case_py)
    for entry in config.case_dirs:
        dir_path = Path(entry)
        if not dir_path.is_absolute():
            dir_path = root / dir_path
        if dir_path.is_dir():
            candidates.extend(sorted(dir_path.glob("*.py")))

    seen: set[Path] = set()
    files: list[Path] = []
    for path in candidates:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            files.append(path)

    if not files:
        raise ConfigError(
            "no case file found (expected case.py or case_dirs)",
            code="CFG_NOT_FOUND",
        )
    return files


def _expand_export(name: str, value: object, path: Path) -> list[Case]:
    if not isinstance(value, (list, tuple)):
        raise ConfigError(
            f"{path.name}: {name} must be a list or tuple, got {type(value).__name__}",
            code="CASE_INVALID",
            file=path,
        )
    out: list[Case] = []
    for item in value:
        if isinstance(item, Case):
            out.append(item)
        elif isinstance(item, Suite):
            out.extend(item.expand())
        else:
            raise ConfigError(
                f"{path.name}: {name} items must be Case or Suite, got {type(item).__name__}",
                code="CASE_INVALID",
                file=path,
            )
    return out


def _extract_cases(namespace: dict[str, object], path: Path) -> list[Case]:
    found_any = False
    cases: list[Case] = []

    if "CASES" in namespace and namespace["CASES"] is not None:
        found_any = True
        exported = namespace["CASES"]
        if callable(exported):
            exported = exported()
        cases.extend(_expand_export("CASES", exported, path))
    elif callable(namespace.get("cases")):
        found_any = True
        cases.extend(_expand_export("cases()", namespace["cases"](), path))  # type: ignore[operator]

    if "SUITES" in namespace and namespace["SUITES"] is not None:
        found_any = True
        suites = namespace["SUITES"]
        if callable(suites):
            suites = suites()
        cases.extend(_expand_export("SUITES", suites, path))

    if "CASE" in namespace and namespace["CASE"] is not None:
        found_any = True
        single = namespace["CASE"]
        if isinstance(single, Case):
            cases.append(single)
        elif isinstance(single, Suite):
            cases.extend(single.expand())
        else:
            raise ConfigError(
                f"{path.name}: CASE must be a Case or Suite, got {type(single).__name__}",
                code="CASE_INVALID",
                file=path,
            )

    if not found_any:
        raise ConfigError(
            f"{path.name} must define CASES, cases(), CASE, or SUITES",
            code="CFG_LOAD_FAILED",
            file=path,
        )
    return cases


def _load_file(path: Path, root: Path) -> list[CaseSource]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            f"cannot read {path.name}: {exc}",
            code="CFG_LOAD_FAILED",
            file=path,
        ) from exc

    extra_paths: list[str] = []
    for candidate in (str(path.parent), str(root)):
        if candidate not in extra_paths:
            extra_paths.append(candidate)

    for entry in reversed(extra_paths):
        sys.path.insert(0, entry)
    try:
        namespace = runpy.run_path(str(path), run_name="__solouvm_case__")
        cases = _extract_cases(namespace, path)
    except ConfigError as exc:
        if exc.file is None:
            exc.file = str(path)
            if exc.line is None:
                exc.line = _user_line(exc, path)
        raise
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
        for entry in extra_paths:
            with contextlib.suppress(ValueError):
                sys.path.remove(entry)

    line_map = _collect_case_lines(source)
    out: list[CaseSource] = []
    for case in cases:
        hits = line_map.get(case.name)
        lineno = hits.pop(0) if hits else None
        out.append(CaseSource(case=case, file=path, line=lineno))
    return out


def collect_cases_with_sources(config: Config) -> list[CaseSource]:
    """Load, expand, and validate all cases from ``case.py`` and ``config.case_dirs``."""
    root = Path(config.root)
    sources: list[CaseSource] = []
    for path in _case_files(config):
        sources.extend(_load_file(path, root))

    seen: dict[str, CaseSource] = {}
    known_builds = ", ".join(sorted(config.builds))
    for cs in sources:
        case = cs.case
        if case.name in seen:
            first = seen[case.name]
            loc = f"{first.file}:{first.line}" if first.line is not None else str(first.file)
            raise ConfigError(
                f"duplicate case name {case.name!r} (first defined at {loc})",
                code="CASE_INVALID",
                file=cs.file,
                line=cs.line,
            )
        seen[case.name] = cs
        if case.build not in config.builds:
            raise ConfigError(
                f"case {case.name!r}: unknown build mode {case.build!r}; "
                f"defined builds: {known_builds}",
                code="CASE_INVALID",
                file=cs.file,
                line=cs.line,
            )
        try:
            validate_records(case)
        except ConfigError as exc:
            if exc.file is None:
                exc.file = str(cs.file)
                if exc.line is None:
                    exc.line = cs.line
            raise
    return sources


def collect_cases(config: Config) -> list[Case]:
    """Return all validated :class:`Case` objects for ``config``."""
    return [cs.case for cs in collect_cases_with_sources(config)]


def case_file_for(config: Config, case_name: str) -> Path:
    """Return the source file defining ``case_name``; raise ``CASE_INVALID`` if missing."""
    sources = collect_cases_with_sources(config)
    for cs in sources:
        if cs.case.name == case_name:
            return cs.file
    names = [cs.case.name for cs in sources]
    matches = get_close_matches(case_name, names, n=1)
    hint = f"did you mean {matches[0]!r}?" if matches else None
    raise ConfigError(f"case {case_name!r} not found", code="CASE_INVALID", hint=hint)
