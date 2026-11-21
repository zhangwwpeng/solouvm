import pytest

from solouvm import Bug, Case, Log, Note, Todo, Triage
from solouvm.errors import ConfigError
from solouvm.model import validate_records


def _ok(*records):
    return Case(name="c", notes=list(records))


def _bad(*records) -> str:
    with pytest.raises(ConfigError) as excinfo:
        Case(name="c", notes=list(records))
    assert excinfo.value.code == "RECORD_INVALID"
    return str(excinfo.value)


def _triage(**kw):
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


# ---------- Bug ----------


def test_bug_minimal():
    bug = Bug(id=1, title="x", found="2026-11-02")
    assert bug.status == "open"
    _ok(bug)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"status": "open"},
        {"status": "fixed", "fixed": "2026-11-03"},
        {"status": "verified", "fixed": "2026-11-03", "verified": "2026-11-04"},
        {"status": "wontfix", "body": "reason"},
        {"status": "invalid", "body": "reason"},
        {"status": "duplicate", "link": "burst#1"},
    ],
)
def test_bug_status_valid(kwargs):
    _ok(Bug(id=1, title="x", found="2026-11-02", **kwargs))


def test_bug_status_missing_dates():
    msg = _bad(Bug(id=1, title="x", status="fixed", found="2026-11-02"))
    assert "'fixed'" in msg
    msg = _bad(Bug(id=1, title="x", status="verified", found="2026-11-02", fixed="2026-11-03"))
    assert "'verified'" in msg
    msg = _bad(Bug(id=1, title="x"))
    assert "'found'" in msg


def test_bug_duplicate_link():
    assert "'link'" in _bad(Bug(id=1, title="x", status="duplicate", found="2026-11-02"))
    _ok(Bug(id=1, title="x", status="duplicate", found="2026-11-02", link="burst#1"))


def test_bug_status_bogus():
    assert "'bogus'" in _bad(Bug(id=1, title="x", status="bogus", found="2026-11-02"))


@pytest.mark.parametrize("found", ["2026-13-01", "2026/11/02", "2026-02-30"])
def test_bug_bad_date(found):
    msg = _bad(Bug(id=1, title="x", found=found))
    assert "found" in msg and repr(found) in msg


def test_bug_id_and_seed():
    _bad(Bug(id=0, title="x", found="2026-11-02"))
    _bad(Bug(id=True, title="x", found="2026-11-02"))
    _bad(Bug(id=1, title="x", found="2026-11-02", seed=-1))
    _bad(Bug(id=1, title="x", found="2026-11-02", seed=True))
    _ok(Bug(id=1, title="x", found="2026-11-02", seed=4417))


# ---------- Todo / Log / Note ----------


def test_todo():
    todo = Todo("补 narrow transfer 场景")
    assert todo.title == "补 narrow transfer 场景" and todo.done is False
    _ok(todo, Todo("y", due="2026-11-20"))
    assert "due" in _bad(Todo("y", due="2026-11-20x"))


def test_log():
    log = Log("2026-11-02", "x")
    assert (log.date, log.text) == ("2026-11-02", "x")
    _ok(log)
    assert "date" in _bad(Log("2026-11-32", "x"))


def test_note():
    assert Note("x").text == "x"
    _ok(Note("x"), Note("x", date="2026-11-02"))
    _bad(Note("x", date="11/02"))


# ---------- Triage ----------


def test_triage_valid():
    _ok(_triage())


def test_triage_verdict_confidence():
    assert "verdict" in _bad(_triage(verdict="maybe"))
    assert "confidence" in _bad(_triage(confidence="sure"))


def test_triage_summary_length():
    _ok(_triage(summary="x" * 200))
    assert "summary" in _bad(_triage(summary="x" * 201))
    _bad(_triage(summary=""))


def test_triage_evidence():
    _bad(_triage(evidence=[]))
    _ok(_triage(evidence=["log:812", "tb/axi_scb.sv:120", "wave:1200ns axi_awaddr"]))
    assert "'812'" in _bad(_triage(evidence=["812"]))


def test_triage_review():
    review = {"decision": "accepted", "reason": None, "author": "main", "at": "2026-11-17T10:00"}
    _ok(_triage(review=review))
    assert "decision" in _bad(_triage(review={**review, "decision": "maybe"}))
    no_author = {k: v for k, v in review.items() if k != "author"}
    assert "author" in _bad(_triage(review=no_author))
    _bad(_triage(review={**review, "extra": 1}))


def test_triage_judged_status():
    _ok(_triage(judged_status={"burst__4417": "fail"}))
    assert "'bogus'" in _bad(_triage(judged_status={"burst__4417": "bogus"}))


# ---------- validate_records ----------


def test_validate_records_reports_all():
    case = Case(name="c")
    case.notes = [
        Bug(id=1, title="x", status="bogus", found="2026-11-02"),
        Todo("y", due="bad"),
        Log("2026-13-01", "z"),
    ]
    with pytest.raises(ConfigError) as excinfo:
        validate_records(case)
    assert excinfo.value.code == "RECORD_INVALID"
    assert len(str(excinfo.value).splitlines()) == 3


def test_case_construction_reports_all():
    msg = _bad(
        Bug(id=1, title="x", status="bogus", found="2026-11-02"),
        Todo("y", due="bad"),
        Log("2026-13-01", "z"),
    )
    assert len(msg.splitlines()) == 3


def test_unknown_record_type():
    assert "unknown record type" in _bad(object())
