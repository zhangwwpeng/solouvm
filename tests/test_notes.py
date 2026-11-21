"""Tests for solouvm.notes.{query,commands} (Task 21, M-05a, M-06, M-07, M-14, M-16, M-25)."""

from __future__ import annotations

import io
import json
import textwrap
from datetime import date
from pathlib import Path

import pytest

from solouvm.collect import collect_cases
from solouvm.config import load_config
from solouvm.errors import ConfigError, RecordError, RoleError
from solouvm.history import update_latest_link
from solouvm.model import Log, Note, Todo
from solouvm.notes import (
    bug_add,
    bug_update,
    format_notes_table,
    next_bug_id,
    note_add,
    query_notes,
    todo_add,
    todo_done,
)
from solouvm.output import make_json_envelope


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _setup_project(tmp_path: Path, case_py: str) -> Path:
    root = tmp_path / "proj"
    _write(
        root / "solouvm.conf.py", 'CFG = {"project": "demo", "simulator": "fake", "top": "tb"}\n'
    )
    _write(root / "case.py", case_py)
    return root


class _TtyStdin(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_bug_add_id_allocation_and_max_plus_one(tmp_path: Path):
    root = _setup_project(
        tmp_path,
        textwrap.dedent(
            """\
            from solouvm import Bug, Case

            CASES = [
                Case(name="empty_case"),
                Case(
                    name="burst",
                    notes=[
                        Bug(id=1, title="bug1", found="2026-11-01"),
                        Bug(id=3, title="bug3", found="2026-11-02"),
                    ],
                ),
            ]
            """
        ),
    )
    config = load_config(root / "solouvm.conf.py")
    today = date(2026, 11, 18)

    b1 = bug_add(config=config, case_name="empty_case", title="first bug", today=today)
    assert b1.id == 1
    assert f"empty_case#{b1.id}" == "empty_case#1"
    assert b1.status == "open"
    assert b1.found == "2026-11-18"
    assert b1.author == "main"
    assert b1.at is not None

    b4 = bug_add(config=config, case_name="burst", title="fourth bug", today=today)
    assert b4.id == 4
    assert f"burst#{b4.id}" == "burst#4"


def test_bug_add_job_autofill_and_stdin_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _setup_project(
        tmp_path,
        textwrap.dedent(
            """\
            from solouvm import Case

            CASES = [Case(name="burst")]
            """
        ),
    )
    actual_run = "20261110-100000-ab12"
    rdir = root / ".solouvm" / "runs" / actual_run
    rdir.mkdir(parents=True)
    _write(
        rdir / "results.json",
        json.dumps(
            {
                "schema": 1,
                "run_id": actual_run,
                "judged_at": "2026-11-10T10:05:00+08:00",
                "partial": False,
                "summary": {"total": 1, "passed": 0},
                "jobs": [
                    {
                        "job_id": "burst__4417",
                        "case": "burst",
                        "seed": 4417,
                        "status": "fail",
                        "signature": {"id": "3fa9c2e1", "text": "UVM_ERROR mismatch"},
                    }
                ],
            }
        ),
    )
    update_latest_link(root, actual_run)

    config = load_config(root / "solouvm.conf.py")
    monkeypatch.setattr("sys.stdin", io.StringIO("body from stdin\nline 2"))
    bug = bug_add(
        config=config,
        case_name="burst",
        title="wrap bug",
        job_id="burst__4417",
        body="-",
        author="triage:agent-1",
        role="triage",
        today=date(2026, 11, 18),
    )
    assert bug.seed == 4417
    assert bug.signature == "3fa9c2e1"
    assert bug.run == actual_run
    assert bug.job == "burst__4417"
    assert bug.body == "body from stdin\nline 2"
    assert bug.author == "triage:agent-1"
    assert bug.at is not None


def test_stdin_tty_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = _setup_project(tmp_path, 'from solouvm import Case\nCASES = [Case(name="burst")]\n')
    config = load_config(root / "solouvm.conf.py")
    monkeypatch.setattr("sys.stdin", _TtyStdin())
    with pytest.raises(ConfigError) as exc:
        bug_add(config=config, case_name="burst", title="x", body="-")
    assert exc.value.code == "CFG_INVALID"


def test_bug_update_dates_duplicate_validation_and_role_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = _setup_project(
        tmp_path,
        textwrap.dedent(
            """\
            from solouvm import Bug, Case, Triage

            CASES = [
                Case(
                    name="burst",
                    notes=[
                        Bug(id=1, title="by triage", found="2026-11-01", author="triage"),
                        Bug(id=2, title="by main", found="2026-11-01", author="main"),
                        Bug(id=3, title="reviewed", found="2026-11-01", author="triage"),
                        Triage(
                            task="run1/1",
                            verdict="design_bug",
                            confidence="high",
                            summary="reviewed bug 3",
                            evidence=["log:10"],
                            judged_status={"burst__1": "fail"},
                            bugs=["burst#3"],
                            review={
                                "decision": "accepted",
                                "reason": None,
                                "author": "main",
                                "at": "2026-11-02T10:00:00+08:00",
                            },
                        ),
                    ],
                ),
            ]
            """
        ),
    )
    config = load_config(root / "solouvm.conf.py")
    today = date(2026, 11, 18)

    # Triage updates own unreviewed bug -> ok
    monkeypatch.setattr("sys.stdin", io.StringIO("extra detail"))
    updated = bug_update(
        config=config,
        ref="burst#1",
        status="fixed",
        append_body="-",
        role="triage",
        today=today,
    )
    assert updated.status == "fixed"
    assert updated.fixed == "2026-11-18"
    assert updated.body == "extra detail"

    # Verified auto-fills fixed & verified
    ver = bug_update(config=config, ref="burst#2", status="verified", role="main", today=today)
    assert ver.fixed == "2026-11-18"
    assert ver.verified == "2026-11-18"

    # Duplicate without link -> RECORD_INVALID
    with pytest.raises(RecordError) as exc_dup:
        bug_update(config=config, ref="burst#1", status="duplicate", role="main")
    assert exc_dup.value.code == "RECORD_INVALID"

    # Triage cannot update someone else's bug
    with pytest.raises(RoleError) as exc_other:
        bug_update(config=config, ref="burst#2", owner="alice", role="triage")
    assert exc_other.value.code == "ROLE_DENIED"

    # Triage cannot update reviewed bug
    with pytest.raises(RoleError) as exc_rev:
        bug_update(config=config, ref="burst#3", owner="alice", role="triage")
    assert exc_rev.value.code == "ROLE_DENIED"

    # Bad ref format
    with pytest.raises(RecordError) as exc_fmt:
        bug_update(config=config, ref="badref", status="fixed")
    assert exc_fmt.value.code == "RECORD_INVALID"


def test_note_add_todo_add_and_todo_done(tmp_path: Path):
    root = _setup_project(
        tmp_path,
        textwrap.dedent(
            """\
            from solouvm import Bug, Case, Todo

            CASES = [
                Case(
                    name="burst",
                    notes=[
                        Bug(id=1, title="existing bug", found="2026-11-01"),
                    ],
                ),
                Case(
                    name="legacy",
                    notes=[
                        Todo("legacy task without id"),
                    ],
                ),
            ]
            """
        ),
    )
    config = load_config(root / "solouvm.conf.py")

    log_rec = note_add(config=config, case_name="burst", text="checked waveform", role="triage")
    assert isinstance(log_rec, Log)
    assert log_rec.author == "triage"
    assert log_rec.at is not None

    note_rec = note_add(
        config=config,
        case_name="burst",
        text="general note",
        kind="note",
        author="wp",
    )
    assert isinstance(note_rec, Note)
    assert note_rec.author == "wp"
    assert note_rec.at is not None

    # Adding todo on a case that already has Bug(id=1) avoids ID collision in model.validate_records
    todo_rec = todo_add(
        config=config,
        case_name="burst",
        title="add narrow transfer",
        due="2026-11-20",
        owner="wp",
    )
    assert isinstance(todo_rec, Todo)
    assert todo_rec.id == 2
    assert todo_rec.done is False
    assert todo_rec.author == "main"
    assert todo_rec.at is not None

    # Triage cannot mark todo done
    with pytest.raises(RoleError) as exc_role:
        todo_done(config=config, ref="burst#2", role="triage")
    assert exc_role.value.code == "ROLE_DENIED"

    done_rec = todo_done(config=config, ref="burst#2", role="main")
    assert done_rec.done is True

    # Legacy Todo without explicit id works via 1-based todo index fallback
    legacy_done = todo_done(config=config, ref="legacy#1", role="main")
    assert legacy_done.done is True

    cases = {c.name: c for c in collect_cases(config)}
    assert any(isinstance(r, Todo) and r.id == 2 and r.done for r in cases["burst"].notes)
    assert any(isinstance(r, Todo) and r.id is None and r.done for r in cases["legacy"].notes)


def test_query_notes_filters_table_json_and_next_id(tmp_path: Path):
    root = _setup_project(
        tmp_path,
        textwrap.dedent(
            """\
            from solouvm import Bug, Case, Log, Note, Todo

            CASES = [
                Case(
                    name="burst",
                    owner="wp",
                    tags=("axi", "nightly"),
                    notes=[
                        Bug(
                            id=1,
                            title="open bug",
                            status="open",
                            seed=4417,
                            found="2026-11-02",
                            owner="hw-zhao",
                        ),
                        Bug(
                            id=2,
                            title="fixed bug",
                            status="fixed",
                            found="2026-11-03",
                            fixed="2026-11-04",
                        ),
                        Bug(
                            id=3,
                            title="verified bug",
                            status="verified",
                            found="2026-11-01",
                            fixed="2026-11-02",
                            verified="2026-11-04",
                        ),
                        Todo("overdue todo", due="2026-11-10"),
                        Todo("done todo", due="2026-11-05", done=True),
                        Log("2026-11-02", "debug log"),
                        Note("general note", date="2026-11-03"),
                    ],
                ),
                Case(
                    name="smoke",
                    owner="alice",
                    tags=("smoke",),
                    notes=[
                        Todo("future todo", due="2026-11-25"),
                    ],
                ),
            ]
            """
        ),
    )
    config = load_config(root / "solouvm.conf.py")
    cases = collect_cases(config)
    today = date(2026, 11, 18)

    # Default: open + fixed bugs and incomplete todos
    default_res = query_notes(config=config, cases=cases, today=today)
    assert default_res["open_bugs"] == 1
    assert default_res["fixed_bugs"] == 1
    assert default_res["open_todos"] == 2
    ids_types = [(r["id"], r["type"], r["status"]) for r in default_res["rows"]]
    assert ids_types == [
        ("burst#1", "bug", "open"),
        ("burst#2", "bug", "fixed"),
        ("burst", "todo", "-"),
        ("smoke", "todo", "-"),
    ]

    table = format_notes_table(default_res)
    for col in ("ID", "TYPE", "STATUS", "OWNER", "SEED", "TITLE"):
        assert col in table
    assert "1 open bug, 1 fixed awaiting verification, 2 todos" in table

    # JSON envelope shape
    env = make_json_envelope("notes", default_res)
    assert env["ok"] is True
    assert len(env["data"]["rows"]) == 4

    # --type bug --status all
    all_bugs = query_notes(
        config=config,
        cases=cases,
        filters={"types": ["bug"], "statuses": ["all"]},
        today=today,
    )
    assert [r["id"] for r in all_bugs["rows"]] == ["burst#1", "burst#2", "burst#3"]

    # --overdue
    overdue = query_notes(config=config, cases=cases, filters={"overdue": True}, today=today)
    assert [r["title"] for r in overdue["rows"]] == ["overdue todo"]

    # --owner wp (inherits case.owner for bug#2 and overdue todo)
    by_wp = query_notes(config=config, cases=cases, filters={"owner": "wp"}, today=today)
    assert [(r["id"], r["title"]) for r in by_wp["rows"]] == [
        ("burst#2", "fixed bug"),
        ("burst", "overdue todo"),
    ]

    # --since / --until and --tag
    dated = query_notes(
        config=config,
        cases=cases,
        filters={"types": ["log", "note"], "since": "2026-11-03", "until": "2026-11-03"},
        today=today,
    )
    assert [r["title"] for r in dated["rows"]] == ["general note"]

    tagged = query_notes(
        config=config,
        cases=cases,
        filters={"tag": ["smoke"]},
        today=today,
    )
    assert [r["case"] for r in tagged["rows"]] == ["smoke"]

    # --next-id
    assert next_bug_id(cases, "burst") == 4
    assert next_bug_id(cases, "smoke") == 1

    # --case glob, --exclude-tag, --status open vs --type todo --status done
    by_case = query_notes(config=config, cases=cases, filters={"case": ["sm*"]}, today=today)
    assert [r["case"] for r in by_case["rows"]] == ["smoke"]

    excl = query_notes(config=config, cases=cases, filters={"exclude_tag": ["axi"]}, today=today)
    assert [r["case"] for r in excl["rows"]] == ["smoke"]

    only_open_bugs = query_notes(
        config=config, cases=cases, filters={"statuses": ["open"]}, today=today
    )
    assert [r["id"] for r in only_open_bugs["rows"]] == ["burst#1"]

    done_todos = query_notes(
        config=config,
        cases=cases,
        filters={"types": ["todo"], "statuses": ["done"]},
        today=today,
    )
    assert [r["title"] for r in done_todos["rows"]] == ["done todo"]
