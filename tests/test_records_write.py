import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from solouvm import Bug, Case, Expect, Triage
from solouvm.errors import RecordError
from solouvm.records import add_case, append_record, find_case_node, record_lock, replace_record
from solouvm.records import write as write_mod
from solouvm.records.write import _load_cases, lock_dir

REPO = Path(__file__).resolve().parents[1]

BASIC = textwrap.dedent(
    """\
    from solouvm import Bug, Case

    CASES = [
        Case(
            name="burst",
            uvm_test="axi_burst_test",
            notes=[
                Bug(id=1, title="wrap error", found="2026-11-02"),
            ],
        ),
        Case(
            name="smoke",
            notes=[],
        ),
        Case(
            name="plain",
            uvm_test="plain_test",
        ),
    ]
    """
)


def _write(tmp_path: Path, text: str, name: str = "case.py") -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _case(path: Path, name: str) -> Case:
    return next(c for c in _load_cases(path) if c.name == name)


def _bug(**kw) -> Bug:
    args = {"id": 2, "title": "new bug", "found": "2026-11-05"}
    args.update(kw)
    return Bug(**args)


def _triage(**kw) -> Triage:
    args = {
        "task": "T1",
        "verdict": "design_bug",
        "confidence": "high",
        "summary": "addr wraps wrong",
        "evidence": ["log:812"],
        "judged_status": {"burst__4417": "fail"},
    }
    args.update(kw)
    return Triage(**args)


def _code(excinfo) -> str:
    return excinfo.value.code


# ---------- find_case_node ----------


def test_find_case_node():
    tree = ast.parse('CASES = [Case(name="a"), Case(name="b")]')
    node = find_case_node(tree, "b")
    assert isinstance(node, ast.Call)
    assert node.keywords[0].value.value == "b"


def test_find_case_node_attribute_form():
    tree = ast.parse('import solouvm\nCASE = solouvm.Case(name="x")')
    assert find_case_node(tree, "x").lineno == 2


def test_find_case_node_missing(tmp_path):
    tree = ast.parse('CASES = [Case(name="a")]')
    with pytest.raises(RecordError) as excinfo:
        find_case_node(tree, "zz", tmp_path / "case.py")
    assert _code(excinfo) == "RECORD_INVALID"
    assert "'zz'" in str(excinfo.value)
    assert "case.py" in str(excinfo.value)


# ---------- append ----------


def test_append_to_existing_notes(tmp_path):
    path = _write(tmp_path, BASIC)
    append_record(path, "burst", _bug())
    text = path.read_text()
    old_lines, new_lines = BASIC.splitlines(), text.splitlines()
    assert len(new_lines) == len(old_lines) + 1
    added = '            Bug(id=2, title="new bug", found="2026-11-05"),'
    assert added.replace('"', "'") in new_lines
    # 除新增行以外原样不变
    assert [line for line in new_lines if line != added.replace('"', "'")] == old_lines


def test_append_reload_fields(tmp_path):
    path = _write(tmp_path, BASIC)
    append_record(path, "burst", _bug(seed=4417, signature="abc", status="open"))
    case = _case(path, "burst")
    assert len(case.notes) == 2
    new = case.notes[-1]
    assert (new.id, new.title, new.seed, new.signature, new.found) == (
        2,
        "new bug",
        4417,
        "abc",
        "2026-11-05",
    )


def test_append_empty_notes(tmp_path):
    path = _write(tmp_path, BASIC)
    append_record(path, "smoke", _bug(id=1))
    text = path.read_text()
    ast.parse(text)
    assert "[," not in text.replace(" ", "").replace("\n", "")
    assert len(_case(path, "smoke").notes) == 1


def test_append_empty_notes_single_line(tmp_path):
    path = _write(tmp_path, 'from solouvm import Bug, Case\nCASES = [Case(name="a", notes=[])]\n')
    append_record(path, "a", _bug(id=1))
    text = path.read_text()
    assert "notes=[Bug(id=1, title='new bug', found='2026-11-05')]" in text
    assert len(_case(path, "a").notes) == 1


def test_append_without_notes(tmp_path):
    path = _write(tmp_path, BASIC)
    append_record(path, "plain", _bug(id=1))
    text = path.read_text()
    assert (
        '        uvm_test="plain_test",\n'
        "        notes=[\n"
        "            Bug(id=1, title='new bug', found='2026-11-05'),\n"
        "        ],\n"
        "    ),\n"
    ) in text
    assert len(_case(path, "plain").notes) == 1


def test_append_without_notes_no_trailing_comma(tmp_path):
    src = 'from solouvm import Case\n\nCASE = Case(\n    name="x",\n    uvm_test="t"\n)\n'
    path = _write(tmp_path, src)
    append_record(path, "x", _bug(id=1))
    text = path.read_text()
    assert '    uvm_test="t",\n    notes=[\n' in text
    assert len(_case(path, "x").notes) == 1


def test_append_without_notes_single_line(tmp_path):
    path = _write(tmp_path, 'from solouvm import Case\nCASES = [Case(name="a")]\n')
    append_record(path, "a", _bug(id=1))
    assert "Case(name=\"a\", notes=[Bug(id=1, title='new bug'" in path.read_text()
    assert len(_case(path, "a").notes) == 1


def test_append_keeps_indent(tmp_path):
    path = _write(tmp_path, BASIC)
    append_record(path, "burst", _bug())
    line = next(x for x in path.read_text().splitlines() if "new bug" in x)
    sibling = next(x for x in BASIC.splitlines() if "wrap error" in x)
    indent = len(sibling) - len(sibling.lstrip())
    assert len(line) - len(line.lstrip()) == indent
    assert line.startswith(" " * indent) and "\t" not in line


def test_append_long_record_is_multiline(tmp_path):
    path = _write(tmp_path, BASIC)
    bug = _bug(body="x" * 120, owner="wp", reporter="alice")
    append_record(path, "burst", bug)
    text = path.read_text()
    assert "            Bug(\n                id=2,\n" in text
    assert "                body='" in text
    assert _case(path, "burst").notes[-1] == bug


def test_append_preserves_crlf(tmp_path):
    path = tmp_path / "case.py"
    path.write_bytes(BASIC.replace("\n", "\r\n").encode())
    append_record(path, "burst", _bug())
    data = path.read_bytes()
    assert b"\n" not in data.replace(b"\r\n", b"")
    assert len(_case(path, "burst").notes) == 2


# ---------- replace ----------


def test_replace_bug_status(tmp_path):
    path = _write(tmp_path, BASIC)
    replace_record(
        path,
        "burst",
        _bug(id=1, title="wrap error", found="2026-11-02", status="fixed", fixed="2026-11-06"),
    )
    text = path.read_text()
    old = '            Bug(id=1, title="wrap error", found="2026-11-02"),\n'
    new = (
        "            Bug(id=1, title='wrap error', status='fixed', found='2026-11-02', "
        "fixed='2026-11-06'),\n"
    )
    assert text == BASIC.replace(old, new)
    bug = _case(path, "burst").notes[0]
    assert (bug.status, bug.fixed) == ("fixed", "2026-11-06")


def test_replace_triage_review(tmp_path):
    src = textwrap.dedent(
        """\
        from solouvm import Case, Triage

        CASES = [
            Case(
                name="burst",
                notes=[
                    Triage(task="T1", verdict="design_bug", confidence="high",
                           summary="addr wraps wrong", evidence=["log:812"],
                           judged_status={"burst__4417": "fail"}),
                ],
            ),
        ]
        """
    )
    path = _write(tmp_path, src)
    review = {"decision": "accepted", "reason": "ok", "author": "wp", "at": "2026-11-07T10:00:00"}
    replace_record(path, "burst", _triage(review=review))
    tri = _case(path, "burst").notes[0]
    assert tri.review == review
    assert tri.summary == "addr wraps wrong"
    # 原记录跨 3 行，替换后仍是多行写法，其他行不变
    text = path.read_text()
    assert text.startswith(src.split("            Triage(")[0])
    assert text.endswith("        ],\n    ),\n]\n")
    assert "            Triage(\n                task='T1',\n" in text


def test_replace_multiline_record(tmp_path):
    src = textwrap.dedent(
        """\
        from solouvm import Bug, Case

        CASE = Case(
            name="burst",
            notes=[
                Bug(
                    id=1,
                    title="wrap error",
                    found="2026-11-02",
                ),
                Bug(id=2, title="other", found="2026-11-03"),
            ],
        )
        """
    )
    path = _write(tmp_path, src)
    replace_record(
        path,
        "burst",
        _bug(id=1, title="wrap error", found="2026-11-02", status="fixed", fixed="2026-11-06"),
    )
    expected = src.replace(
        '            id=1,\n            title="wrap error",\n            found="2026-11-02",\n',
        "            id=1,\n            title='wrap error',\n            status='fixed',\n"
        "            found='2026-11-02',\n            fixed='2026-11-06',\n",
    )
    assert path.read_text() == expected
    notes = _case(path, "burst").notes
    assert [b.status for b in notes] == ["fixed", "open"]


def test_replace_missing_record(tmp_path):
    path = _write(tmp_path, BASIC)
    before = path.read_bytes()
    with pytest.raises(RecordError) as excinfo:
        replace_record(path, "burst", _bug(id=9))
    assert _code(excinfo) == "RECORD_INVALID"
    assert path.read_bytes() == before


def test_replace_with_chinese(tmp_path):
    src = textwrap.dedent(
        """\
        from solouvm import Bug, Case

        CASES = [
            Case(name="burst", doc="验证 AXI", notes=[Bug(id=1, title="地址回绕", found="2026-11-02"), Bug(id=2, title="x", found="2026-11-02")]),
        ]
        """  # noqa: E501
    )
    path = _write(tmp_path, src)
    replace_record(
        path, "burst", _bug(id=2, title="x", found="2026-11-02", status="fixed", fixed="2026-11-06")
    )
    text = path.read_text()
    assert text == src.replace(
        'Bug(id=2, title="x", found="2026-11-02")',
        "Bug(id=2, title='x', status='fixed', found='2026-11-02', fixed='2026-11-06')",
    )
    case = _case(path, "burst")
    assert case.doc == "验证 AXI"
    assert case.notes[0].title == "地址回绕"


def test_append_with_chinese(tmp_path):
    src = (
        "from solouvm import Bug, Case\n"
        'CASES = [Case(name="burst", doc="验证 AXI", notes=[Bug(id=1, title="回绕", '
        'found="2026-11-02")])]\n'
    )
    path = _write(tmp_path, src)
    append_record(path, "burst", _bug(title="中文标题"))
    assert [b.title for b in _case(path, "burst").notes] == ["回绕", "中文标题"]


# ---------- add_case ----------


def test_add_case_to_cases_list(tmp_path):
    path = _write(tmp_path, BASIC)
    add_case(
        path,
        Case(
            name="new_one",
            uvm_test="axi_new_test",
            stage="wip",
            expect=[Expect(scoreboard="AXI_SCB", min=1)],
        ),
    )
    names = [c.name for c in _load_cases(path)]
    assert names == ["burst", "smoke", "plain", "new_one"]
    text = path.read_text()
    assert "    Case(\n        name='new_one',\n" in text
    assert "from solouvm import Bug, Case, Expect\n" in text
    case = _case(path, "new_one")
    assert case.expect[0].scoreboard == "AXI_SCB"


def test_add_case_empty_list(tmp_path):
    path = _write(tmp_path, "from solouvm import Case\n\nCASES = []\n")
    add_case(path, Case(name="a"))
    assert (
        path.read_text()
        == "from solouvm import Case\n\nCASES = [\n    Case(\n        name='a',\n    ),\n]\n"
    )


def test_add_case_creates_case_py(tmp_path):
    path = tmp_path / "case.py"
    add_case(path, Case(name="a", tags=("smoke",)))
    assert [c.tags for c in _load_cases(path)] == [("smoke",)]


def test_add_case_duplicate(tmp_path):
    path = _write(tmp_path, BASIC)
    with pytest.raises(RecordError) as excinfo:
        add_case(path, Case(name="burst"))
    assert _code(excinfo) == "RECORD_INVALID"
    assert path.read_text() == BASIC


def test_add_case_case_dirs(tmp_path):
    cases = tmp_path / "cases"
    cases.mkdir()
    add_case(cases, Case(name="burst", uvm_test="axi_burst_test"))
    target = cases / "burst.py"
    text = target.read_text()
    assert text.startswith("from solouvm import Case\n\nCASE = Case(\n")
    assert _case(target, "burst").uvm_test == "axi_burst_test"
    with pytest.raises(RecordError):
        add_case(cases, Case(name="burst"))


def test_add_case_without_cases_list(tmp_path):
    path = _write(tmp_path, "from solouvm import Case\n\ndef cases():\n    return []\n")
    with pytest.raises(RecordError) as excinfo:
        add_case(path, Case(name="a"))
    assert _code(excinfo) == "RECORD_INVALID"


# ---------- 导入补齐 ----------


def test_import_added_to_existing_line(tmp_path):
    src = 'from solouvm import Case\n\nCASES = [Case(name="smoke"), Case(name="plain")]\n'
    path = _write(tmp_path, src)
    append_record(path, "smoke", _triage())
    assert path.read_text().startswith("from solouvm import Case, Triage\n")
    append_record(path, "plain", _bug(id=1))
    assert path.read_text().startswith("from solouvm import Bug, Case, Triage\n")


def test_import_line_inserted_when_absent(tmp_path):
    src = 'import os\nfrom solouvm.model import Case\n\nCASES = [Case(name="a")]\n'
    path = _write(tmp_path, src)
    append_record(path, "a", _bug(id=1))
    assert path.read_text().startswith("import os\nfrom solouvm import Bug\nfrom solouvm.model")
    assert len(_case(path, "a").notes) == 1


def test_import_not_duplicated(tmp_path):
    path = _write(tmp_path, BASIC)
    append_record(path, "burst", _bug())
    text = path.read_text()
    assert text.count("import") == 1
    assert text.startswith("from solouvm import Bug, Case\n")


# ---------- 冲突、回滚、语法错误 ----------


def test_conflict_between_read_and_write(tmp_path, monkeypatch):
    path = _write(tmp_path, BASIC)
    manual = BASIC + "# edited by hand\n"
    real = write_mod._render_record

    def sneaky(*args, **kwargs):
        path.write_text(manual)
        return real(*args, **kwargs)

    monkeypatch.setattr(write_mod, "_render_record", sneaky)
    with pytest.raises(RecordError) as excinfo:
        append_record(path, "burst", _bug())
    assert _code(excinfo) == "RECORD_CONFLICT"
    assert path.read_text() == manual


def test_rollback_on_invalid_record(tmp_path):
    path = _write(tmp_path, BASIC)
    before = path.read_bytes()
    with pytest.raises(RecordError) as excinfo:
        append_record(path, "burst", Bug(id=2, title="no found date"))
    assert _code(excinfo) == "RECORD_WRITE_FAILED"
    assert path.read_bytes() == before


def test_rollback_on_duplicate_id(tmp_path):
    path = _write(tmp_path, BASIC)
    before = path.read_bytes()
    with pytest.raises(RecordError) as excinfo:
        append_record(path, "burst", _bug(id=1))
    assert _code(excinfo) == "RECORD_WRITE_FAILED"
    assert path.read_bytes() == before


def test_syntax_error(tmp_path):
    path = _write(tmp_path, "from solouvm import Case\n\nCASES = [\n    Case(name='a'\n")
    with pytest.raises(RecordError) as excinfo:
        append_record(path, "a", _bug(id=1))
    assert _code(excinfo) == "RECORD_INVALID"
    assert excinfo.value.line is not None
    assert f"line {excinfo.value.line}" in excinfo.value.message


def test_notes_not_a_list(tmp_path):
    path = _write(tmp_path, 'from solouvm import Case\nN = []\nCASES = [Case(name="a", notes=N)]\n')
    with pytest.raises(RecordError) as excinfo:
        append_record(path, "a", _bug(id=1))
    assert _code(excinfo) == "RECORD_INVALID"


# ---------- 锁与并发 ----------


def test_lock_acquire_release(tmp_path):
    with record_lock(tmp_path):
        assert lock_dir(tmp_path).is_dir()
    assert not lock_dir(tmp_path).exists()


def test_lock_released_on_error(tmp_path):
    with pytest.raises(ValueError), record_lock(tmp_path):
        raise ValueError
    assert not lock_dir(tmp_path).exists()


def test_lock_timeout(tmp_path):
    lock_dir(tmp_path).mkdir(parents=True)
    with pytest.raises(RecordError) as excinfo, record_lock(tmp_path, timeout=0.2):
        pass
    assert _code(excinfo) == "RECORD_CONFLICT"
    assert lock_dir(tmp_path).is_dir()


_WORKER = """
import sys
from pathlib import Path
from solouvm import Bug
from solouvm.records import append_record, record_lock
from solouvm.records.write import _load_cases

root, tag = Path(sys.argv[1]), sys.argv[2]
path = root / "case.py"
for i in range(5):
    with record_lock(root):
        case = next(c for c in _load_cases(path) if c.name == "burst")
        new_id = max((b.id for b in case.notes if isinstance(b, Bug)), default=0) + 1
        append_record(path, "burst", Bug(id=new_id, title=f"{tag}-{i}", found="2026-11-05"))
"""


def test_concurrent_writers(tmp_path):
    path = _write(tmp_path, BASIC)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO), env.get("PYTHONPATH", "")])
    procs = [
        subprocess.Popen([sys.executable, "-c", _WORKER, str(tmp_path), tag], env=env)
        for tag in ("p1", "p2")
    ]
    assert [p.wait(timeout=120) for p in procs] == [0, 0]
    notes = _case(path, "burst").notes
    ids = [b.id for b in notes]
    assert sorted(ids) == list(range(1, 12))
    titles = {b.title for b in notes}
    assert {f"p{n}-{i}" for n in (1, 2) for i in range(5)} <= titles
