"""Write records back into user case files (architecture.md §8, M-14, M-15, D30, D53).

This is the only module that edits user Python sources. Every edit follows the same steps:

1. the caller holds the record lock (``record_lock``) around the whole
   read -> allocate id -> write sequence;
2. read the target file and remember its sha256;
3. edit the source text located with ``ast`` (never ``ast.unparse``: keep the user's style);
4. re-read the file right before writing; a different hash means ``RECORD_CONFLICT``;
5. write, reload the file with ``runpy`` and validate; on failure restore the original bytes
   and raise ``RECORD_WRITE_FAILED``.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import os
import re
import runpy
import sys
import tempfile
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import MISSING, fields, is_dataclass
from enum import Enum
from pathlib import Path

from solouvm.collect.cases import extract_cases
from solouvm.errors import RecordError
from solouvm.model import Case, validate_records

LOCK_TIMEOUT_S = 30.0
_LOCK_POLL_S = 0.05
# 单行渲染超过这个宽度就换成多行（与 ruff 的 line-length 一致）
_LINE_MAX = 100
_INDENT = "    "
_COMMENT = re.compile(r"#[^\n]*")


# ---------------------------------------------------------------- 锁


def lock_dir(root: Path) -> Path:
    """Directory used as the record lock (architecture.md §4.1)."""
    return Path(root) / ".solouvm" / "locks" / "records.lock"


@contextmanager
def record_lock(root: Path, *, timeout: float = LOCK_TIMEOUT_S) -> Iterator[None]:
    """Hold the ``mkdir`` record lock under ``root``; ``RECORD_CONFLICT`` after ``timeout`` s.

    The lock is not reentrant: take it once around read / allocate id / write.
    """
    path = lock_dir(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            path.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RecordError(
                    f"could not acquire the record lock within {timeout:g}s",
                    code="RECORD_CONFLICT",
                    file=path,
                    hint="another solouvm process is writing records; if none is running, "
                    f"remove {path}",
                ) from None
            time.sleep(_LOCK_POLL_S)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            path.rmdir()


# ---------------------------------------------------------------- 源码文本工具


def _lines(source: str) -> list[str]:
    # 只按 \n 切（\r\n 也以 \n 结尾），与 ast 的行号一致；str.splitlines 还会按 \x0c 等切
    return re.findall(r"[^\n]*\n|[^\n]+$", source)


def _offset(source: str, lineno: int, col: int) -> int:
    """Convert an ast ``(lineno, col)`` to a str index; ``col`` is a UTF-8 byte offset."""
    lines = _lines(source)
    prefix = sum(len(line) for line in lines[: lineno - 1])
    if lineno - 1 >= len(lines):
        return prefix
    raw = lines[lineno - 1].encode("utf-8")[:col]
    return prefix + len(raw.decode("utf-8"))


def _start(source: str, node: ast.AST) -> int:
    return _offset(source, node.lineno, node.col_offset)  # type: ignore[attr-defined]


def _end(source: str, node: ast.AST) -> int:
    return _offset(source, node.end_lineno, node.end_col_offset)  # type: ignore[attr-defined]


def _line_start(source: str, index: int) -> int:
    return source.rfind("\n", 0, index) + 1


def _leading_ws(source: str, index: int) -> str:
    """Leading whitespace of the line containing ``index``."""
    start = _line_start(source, index)
    line = source[start:]
    return line[: len(line) - len(line.lstrip(" \t"))].split("\n")[0]


def _starts_line(source: str, index: int) -> bool:
    """True if only whitespace precedes ``index`` on its line."""
    return not source[_line_start(source, index) : index].strip()


def _newline(source: str) -> str:
    return "\r\n" if "\r\n" in source else "\n"


def _has_comma(segment: str) -> bool:
    # 两个节点之间只可能有空白、逗号、注释；去掉注释再看逗号
    return "," in _COMMENT.sub("", segment)


def _indent_tail(text: str, indent: str, nl: str) -> str:
    """Prefix every line but the first with ``indent``; join with ``nl``."""
    parts = text.split("\n")
    return nl.join([parts[0]] + [indent + p if p else p for p in parts[1:]])


# ---------------------------------------------------------------- 渲染记录源码


def _field_items(obj: object) -> list[tuple[str, object]]:
    """``(name, value)`` of init fields, skipping values equal to their default."""
    items = []
    for f in fields(obj):  # type: ignore[arg-type]
        if not f.init:
            continue
        value = getattr(obj, f.name)
        if f.default is not MISSING:
            default = f.default
        elif f.default_factory is not MISSING:
            default = f.default_factory()
        else:
            # 必填字段总是写出
            items.append((f.name, value))
            continue
        # 类型也要相同：done=0 与 done=False 相等，但不能省略
        if value != default or type(value) is not type(default):
            items.append((f.name, value))
    return items


def _render_value(value: object) -> str:
    if is_dataclass(value) and not isinstance(value, type):
        return _render_obj(value, multiline=False)
    if isinstance(value, Enum):
        return _render_value(value.value)
    if isinstance(value, list):
        return "[" + ", ".join(_render_value(v) for v in value) + "]"
    if isinstance(value, tuple):
        inner = ", ".join(_render_value(v) for v in value)
        return "(" + inner + ("," if len(value) == 1 else "") + ")"
    if isinstance(value, dict):
        pairs = (f"{_render_value(k)}: {_render_value(v)}" for k, v in value.items())
        return "{" + ", ".join(pairs) + "}"
    return repr(value)


def _render_obj(obj: object, *, multiline: bool) -> str:
    """``Name(k=v, ...)``; multi-line form puts one field per line, 4-space indented."""
    name = type(obj).__name__
    items = [f"{k}={_render_value(v)}" for k, v in _field_items(obj)]
    if not multiline or not items:
        return f"{name}({', '.join(items)})"
    return f"{name}(\n" + "".join(f"{_INDENT}{item},\n" for item in items) + ")"


def _render_record(obj: object, col: int, *, multiline: bool | None = None) -> str:
    """Render ``obj``; pick multi-line when forced or when one line would exceed the limit."""
    if multiline is None:
        single = _render_obj(obj, multiline=False)
        # +1 留给后面的逗号
        if col + len(single) + 1 <= _LINE_MAX:
            return single
        multiline = True
    return _render_obj(obj, multiline=multiline)


def _type_names(value: object) -> set[str]:
    """Names of the solouvm dataclasses used in ``value`` (for import completion)."""
    names: set[str] = set()
    if is_dataclass(value) and not isinstance(value, type):
        if type(value).__module__.split(".")[0] == "solouvm":
            names.add(type(value).__name__)
        for _, v in _field_items(value):
            names |= _type_names(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            names |= _type_names(v)
    elif isinstance(value, dict):
        for k, v in value.items():
            names |= _type_names(k) | _type_names(v)
    return names


# ---------------------------------------------------------------- 定位


def _parse(source: str, path: Path) -> ast.Module:
    try:
        return ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        line = exc.lineno
        where = f" at line {line}" if line else ""
        raise RecordError(
            f"cannot parse case file{where}: {exc.msg}",
            code="RECORD_INVALID",
            file=path,
            line=line,
        ) from None


def _is_case_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "Case") or (
        isinstance(func, ast.Attribute) and func.attr == "Case"
    )


def _case_call_name(node: ast.Call) -> object:
    if node.args and isinstance(node.args[0], ast.Constant):
        return node.args[0].value
    for kw in node.keywords:
        if kw.arg == "name" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    return None


def find_case_node(tree: ast.Module, case_name: str, path: Path | None = None) -> ast.Call:
    """Find the ``Case(name=<case_name>, ...)`` call in ``tree``; ``RECORD_INVALID`` if absent."""
    for node in ast.walk(tree):
        if _is_case_call(node) and _case_call_name(node) == case_name:  # type: ignore[arg-type]
            return node  # type: ignore[return-value]
    where = f" in {path}" if path is not None else ""
    raise RecordError(
        f"case {case_name!r} not found{where}",
        code="RECORD_INVALID",
        file=path,
        hint='the case must be written as Case(name="...", ...) with a literal name',
    )


def _notes_keyword(call: ast.Call, case_name: str, path: Path) -> ast.List | None:
    for kw in call.keywords:
        if kw.arg == "notes":
            if not isinstance(kw.value, ast.List):
                raise RecordError(
                    f"case {case_name!r}: 'notes' must be a list literal to write records",
                    code="RECORD_INVALID",
                    file=path,
                    line=kw.value.lineno,
                )
            return kw.value
    return None


# ---------------------------------------------------------------- 改写源码文本


def _column(source: str, index: int) -> int:
    return index - _line_start(source, index)


def _insert_into_list(
    source: str,
    lst: ast.List,
    render: Callable[[int], str],
    base_indent: str,
    *,
    expand_empty: bool,
) -> str:
    """Append an element to the list literal ``lst``.

    ``render(col)`` returns the element text (lines after the first unindented);
    ``base_indent`` is the indentation of the line that owns the list (``notes=`` / ``CASES =``).
    """
    nl = _newline(source)
    close = _end(source, lst) - 1  # ']' 的下标
    if lst.elts:
        last = lst.elts[-1]
        last_end = _end(source, last)
        if lst.lineno != lst.end_lineno and _starts_line(source, close):
            # 多行列表：在 ']' 那一行之前插一整行，缩进与最后一个元素相同
            last_start = _start(source, last)
            if _starts_line(source, last_start):
                indent = _leading_ws(source, last_start)
            else:
                indent = base_indent + _INDENT
            text = _indent_tail(render(len(indent)), indent, nl)
            at = _line_start(source, close)
            new = source[:at] + indent + text + "," + nl + source[at:]
            if not _has_comma(source[last_end:close]):
                new = new[:last_end] + "," + new[last_end:]
            return new
        # 单行列表：紧跟最后一个元素插入 ", <元素>"
        text = _indent_tail(render(_column(source, last_end) + 2), base_indent + _INDENT, nl)
        return source[:last_end] + ", " + text + source[last_end:]
    opening = _start(source, lst)
    if expand_empty:
        indent = base_indent + _INDENT
        text = _indent_tail(render(len(indent)), indent, nl)
        block = "[" + nl + indent + text + "," + nl + base_indent + "]"
        return source[:opening] + block + source[close + 1 :]
    text = _indent_tail(render(_column(source, opening) + 1), base_indent + _INDENT, nl)
    return source[: opening + 1] + text + source[close:]


def _add_notes_keyword(source: str, call: ast.Call, record: object) -> str:
    """Append ``notes=[<record>]`` to the argument list of ``call``."""
    nl = _newline(source)
    spans: list[tuple[ast.AST, ast.AST]] = [(a, a) for a in call.args]
    spans += [(kw, kw.value) for kw in call.keywords]
    first, last = max(spans, key=lambda s: (s[1].end_lineno, s[1].end_col_offset))  # type: ignore[attr-defined]
    last_end = _end(source, last)
    close = _end(source, call) - 1  # ')' 的下标
    if call.lineno != call.end_lineno and _starts_line(source, close):
        start = _start(source, first)
        if _starts_line(source, start):
            indent = _leading_ws(source, start)
        else:
            indent = _leading_ws(source, close) + _INDENT
        inner = indent + _INDENT
        text = _indent_tail(_render_record(record, len(inner)), inner, nl)
        block = f"{indent}notes=[{nl}{inner}{text},{nl}{indent}],{nl}"
        at = _line_start(source, close)
        new = source[:at] + block + source[at:]
        if not _has_comma(source[last_end:close]):
            new = new[:last_end] + "," + new[last_end:]
        return new
    col = _column(source, last_end) + len(", notes=[")
    indent = _leading_ws(source, last_end) + _INDENT
    text = _indent_tail(_render_record(record, col), indent, nl)
    return source[:last_end] + f", notes=[{text}]" + source[last_end:]


def _ensure_imports(source: str, names: set[str], path: Path) -> str:
    """Make sure every name in ``names`` is imported (``from solouvm import ...``)."""
    tree = _parse(source, path)
    present: set[str] = set()
    target: ast.ImportFrom | None = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*" and isinstance(node, ast.ImportFrom):
                    if node.module == "solouvm" and node.level == 0:
                        return source
                    continue
                present.add(alias.asname or alias.name.split(".")[0])
        if (
            target is None
            and isinstance(node, ast.ImportFrom)
            and node in tree.body
            and node.module == "solouvm"
            and node.level == 0
        ):
            target = node
    missing = sorted(names - present)
    if not missing:
        return source
    nl = _newline(source)
    if target is not None:
        # 按字母序插进已有的 import 行；从后往前改，前面的偏移不受影响
        edits: list[tuple[int, str]] = []
        for name in missing:
            after = next((a for a in target.names if a.name > name), None)
            if after is not None:
                edits.append((_start(source, after), f"{name}, "))
            else:
                edits.append((_end(source, target.names[-1]), f", {name}"))
        for at, text in sorted(edits, key=lambda e: e[0], reverse=True):
            source = source[:at] + text + source[at:]
        return source
    line = f"from solouvm import {', '.join(missing)}{nl}"
    first = next((n for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))), None)
    if first is None and tree.body and _is_docstring(tree.body[0]):
        first = tree.body[0]
    if first is None:
        return line + source
    at = source.find("\n", _end(source, first))
    if at < 0:
        return source + nl + line
    return source[: at + 1] + line + source[at + 1 :]


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _call_type(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


_NOTHING = object()


def _call_field(node: ast.Call, cls: type, name: str) -> object:
    """Literal value of field ``name`` in a record call (keyword or positional)."""
    expr: ast.AST | None = None
    for kw in node.keywords:
        if kw.arg == name:
            expr = kw.value
    if expr is None:
        order = [f.name for f in fields(cls) if f.init]
        if name in order and order.index(name) < len(node.args):
            expr = node.args[order.index(name)]
    if expr is None:
        return _NOTHING
    try:
        return ast.literal_eval(expr)
    except ValueError:
        return _NOTHING


def _record_key(record: object) -> tuple[str, object]:
    if type(record).__name__ == "Triage":
        return "task", record.task  # type: ignore[attr-defined]
    value = getattr(record, "id", None)
    if value is None:
        raise RecordError(
            f"cannot replace a {type(record).__name__} record without 'id'",
            code="RECORD_INVALID",
        )
    return "id", value


def _replace_in_notes(
    source: str, call: ast.Call, record: object, case_name: str, path: Path
) -> str:
    nl = _newline(source)
    cls = type(record)
    key, value = _record_key(record)
    lst = _notes_keyword(call, case_name, path)
    match = None
    for elt in lst.elts if lst is not None else []:
        if _call_type(elt) == cls.__name__ and _call_field(elt, cls, key) == value:  # type: ignore[arg-type]
            match = elt
            break
    if match is None:
        raise RecordError(
            f"case {case_name!r} has no {cls.__name__} record with {key}={value!r}",
            code="RECORD_INVALID",
            file=path,
        )
    start, end = _start(source, match), _end(source, match)
    if match.lineno != match.end_lineno:
        multiline: bool | None = True
    elif not _starts_line(source, start):
        # 原记录嵌在一行中间（单行写法），不拆开用户的单行
        multiline = False
    else:
        multiline = None
    text = _render_record(record, _column(source, start), multiline=multiline)
    text = _indent_tail(text, _leading_ws(source, start), nl)
    return source[:start] + text + source[end:]


# ---------------------------------------------------------------- 写回流程


def _digest(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _load_cases(path: Path) -> list[Case]:
    """Run a case file with runpy and return the cases it exports (CASES / cases() / CASE)."""
    root = str(path.parent)
    sys.path.insert(0, root)
    try:
        namespace = runpy.run_path(str(path), run_name="__solouvm_case__")
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(root)
    return extract_cases(namespace, path)


def _reload(path: Path, case_name: str) -> Case:
    """Reload ``path`` and validate the records of ``case_name``."""
    cases = [c for c in _load_cases(path) if c.name == case_name]
    if not cases:
        raise RecordError(f"case {case_name!r} is missing after the write", file=path)
    validate_records(cases[0])
    return cases[0]


def _rewrite(
    path: Path,
    edit: Callable[[str], str],
    check: Callable[[Path], None],
    *,
    create: bool = False,
) -> None:
    """Steps 2-5 of architecture.md §8; the caller holds the record lock (step 1)."""
    original = _read(path)
    if original is None and not create:
        raise RecordError("case file not found", code="RECORD_INVALID", file=path)
    try:
        source = "" if original is None else original.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RecordError(
            f"case file is not valid UTF-8: {exc}", code="RECORD_INVALID", file=path
        ) from None
    new = edit(source)
    # 第 4 步：写之前复核，读与写之间被改动就放弃
    if _digest(_read(path)) != _digest(original):
        raise RecordError(
            "case file changed while the record was being written; nothing was written",
            code="RECORD_CONFLICT",
            file=path,
            hint="run the command again",
        )
    _write_atomic(path, new.encode("utf-8"))
    try:
        check(path)
    except BaseException as exc:
        # 第 5 步失败：恢复原文件（新建的文件直接删掉）
        if original is None:
            with contextlib.suppress(OSError):
                path.unlink()
        else:
            _write_atomic(path, original)
        if isinstance(exc, KeyboardInterrupt):
            raise
        last = traceback.format_exception_only(type(exc), exc)[-1].strip()
        raise RecordError(
            f"validation after write failed, file restored: {last}",
            code="RECORD_WRITE_FAILED",
            file=path,
        ) from exc


def _check_record(case_name: str, record: object) -> Callable[[Path], None]:
    def check(path: Path) -> None:
        case = _reload(path, case_name)
        if not any(type(n) is type(record) and n == record for n in case.notes):
            raise RecordError(f"record not found in case {case_name!r} after the write")

    return check


# ---------------------------------------------------------------- 公开接口


def append_record(path: Path, case_name: str, record: object) -> None:
    """Append ``record`` to ``notes=[...]`` of case ``case_name`` in ``path``.

    The caller must hold ``record_lock`` (ids are allocated under the same lock).
    """
    path = Path(path)

    def edit(source: str) -> str:
        tree = _parse(source, path)
        call = find_case_node(tree, case_name, path)
        lst = _notes_keyword(call, case_name, path)
        if lst is None:
            new = _add_notes_keyword(source, call, record)
        else:
            owner = _start(source, lst)
            new = _insert_into_list(
                source,
                lst,
                lambda col: _render_record(record, col),
                _leading_ws(source, owner),
                expand_empty=call.lineno != call.end_lineno,
            )
        return _ensure_imports(new, _type_names(record), path)

    _rewrite(path, edit, _check_record(case_name, record))


def replace_record(path: Path, case_name: str, record: object) -> None:
    """Replace the record of the same type and id (``task`` for Triage) with ``record``.

    The caller must hold ``record_lock``.
    """
    path = Path(path)

    def edit(source: str) -> str:
        tree = _parse(source, path)
        call = find_case_node(tree, case_name, path)
        new = _replace_in_notes(source, call, record, case_name, path)
        return _ensure_imports(new, _type_names(record), path)

    _rewrite(path, edit, _check_record(case_name, record))


def _find_cases_list(tree: ast.Module, path: Path) -> ast.List:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == "CASES" for t in targets) and isinstance(
            node.value, ast.List
        ):
            return node.value
    raise RecordError(
        "case file must define CASES = [...] as a list literal to add a case",
        code="RECORD_INVALID",
        file=path,
    )


def add_case(path: Path, case: Case) -> None:
    """Register a new case (``new test``).

    ``path`` is ``case.py`` (append to ``CASES = [...]``; the file is created when missing) or a
    ``case_dirs`` directory (create ``<dir>/<name>.py`` with ``CASE = Case(...)``).
    The caller must hold ``record_lock``.
    """
    path = Path(path)
    names = _type_names(case)
    nl = "\n"

    def check(target: Path) -> None:
        _reload(target, case.name)

    if path.is_dir():
        target = path / f"{case.name}.py"
        if target.exists():
            raise RecordError(
                f"case file for {case.name!r} already exists", code="RECORD_INVALID", file=target
            )
        body = _render_obj(case, multiline=True)
        content = f"from solouvm import {', '.join(sorted(names))}{nl}{nl}CASE = {body}{nl}"
        _rewrite(target, lambda _source: content, check, create=True)
        return

    def edit(source: str) -> str:
        if not source.strip():
            body = _indent_tail(_render_obj(case, multiline=True), _INDENT, nl)
            imports = f"from solouvm import {', '.join(sorted(names))}"
            return f"{source}{imports}{nl}{nl}CASES = [{nl}{_INDENT}{body},{nl}]{nl}"
        tree = _parse(source, path)
        for node in ast.walk(tree):
            if _is_case_call(node) and _case_call_name(node) == case.name:  # type: ignore[arg-type]
                raise RecordError(
                    f"case {case.name!r} already exists",
                    code="RECORD_INVALID",
                    file=path,
                    line=node.lineno,  # type: ignore[attr-defined]
                )
        lst = _find_cases_list(tree, path)
        new = _insert_into_list(
            source,
            lst,
            lambda _col: _render_obj(case, multiline=True),
            _leading_ws(source, _start(source, lst)),
            expand_empty=True,
        )
        return _ensure_imports(new, names, path)

    _rewrite(path, edit, check, create=True)
