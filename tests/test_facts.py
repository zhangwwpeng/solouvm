from dataclasses import fields
from datetime import datetime, timedelta, timezone

import pytest

from solouvm.judge.facts import FactsError, parse_facts, read_facts
from solouvm.model import Facts

HEAD = """\
schema=1
generator=solouvm 0.1.0
kind=job
run_id=20261103-101500-a1b2
job_id=burst__4417
try=1
host=simbox01
pid=48213
timeout_impl=gtimeout
started_at=2026-11-03T10:15:02+08:00
state=running
"""

TAIL = """\
state=done
rc=0
signal=
timed_out=0
hung=0
finished_at=2026-11-03T10:21:40+08:00
duration_s=398.214
"""

CST = timezone(timedelta(hours=8))


def _replace(text: str, key: str, value: str | None) -> str:
    """把 text 里所有 key= 行改成 key=value；value 为 None 时删掉这些行。"""
    out = []
    for line in text.splitlines():
        if line.partition("=")[0] == key:
            if value is not None:
                out.append(f"{key}={value}")
        else:
            out.append(line)
    return "\n".join(out) + "\n"


def test_full_facts():
    facts = parse_facts(HEAD + TAIL)
    assert facts == Facts(
        schema=1,
        generator="solouvm 0.1.0",
        kind="job",
        run_id="20261103-101500-a1b2",
        job_id="burst__4417",
        try_=1,
        host="simbox01",
        pid=48213,
        timeout_impl="gtimeout",
        started_at=datetime(2026, 11, 3, 10, 15, 2, tzinfo=CST),
        state="done",
        rc=0,
        signal=None,
        timed_out=False,
        hung=False,
        finished_at=datetime(2026, 11, 3, 10, 21, 40, tzinfo=CST),
        duration_s=398.214,
    )
    assert facts.rc == 0
    assert facts.timed_out is False
    assert facts.hung is False
    assert facts.duration_s == 398.214


def test_facts_field_names():
    names = [f.name for f in fields(Facts)]
    assert "try_" in names
    assert "try" not in names


def test_head_only_is_running():
    facts = parse_facts(HEAD)
    assert facts.state == "running"
    assert facts.rc is None
    assert facts.finished_at is None
    assert facts.duration_s is None
    assert facts.signal is None
    assert facts.timed_out is False


def test_interrupted_with_tail():
    tail = _replace(TAIL, "state", "interrupted")
    tail = _replace(tail, "rc", "130")
    facts = parse_facts(HEAD + tail)
    assert facts.state == "interrupted"
    assert facts.rc == 130


def test_duplicate_key_last_wins():
    assert parse_facts(HEAD + TAIL).state == "done"


def test_unknown_key_ignored():
    facts = parse_facts(HEAD + "future_field=x\n" + TAIL)
    assert facts.state == "done"
    assert not hasattr(facts, "future_field")


def test_schema_2_rejected():
    with pytest.raises(FactsError, match="regenerate") as excinfo:
        parse_facts(_replace(HEAD, "schema", "2"))
    assert str(excinfo.value) == "facts schema 2 not supported; regenerate"


def test_facts_error_is_value_error():
    with pytest.raises(ValueError):
        parse_facts("")


def test_generator_prefix():
    assert parse_facts(_replace(HEAD, "generator", "solouvm 0.1.0")).generator == "solouvm 0.1.0"
    with pytest.raises(FactsError, match="generator"):
        parse_facts(_replace(HEAD, "generator", "other 1.0"))
    with pytest.raises(FactsError):
        parse_facts(_replace(HEAD, "generator", "solouvm"))


def test_bad_timestamp():
    with pytest.raises(FactsError, match="yesterday"):
        parse_facts(_replace(HEAD, "started_at", "yesterday"))


def test_timestamp_without_timezone_rejected():
    with pytest.raises(FactsError, match="timezone"):
        parse_facts(_replace(HEAD, "started_at", "2026-11-03T10:15:02"))


def test_bad_finished_at():
    with pytest.raises(FactsError, match="soon"):
        parse_facts(HEAD + _replace(TAIL, "finished_at", "soon"))


def test_missing_run_id():
    with pytest.raises(FactsError, match="run_id"):
        parse_facts(_replace(HEAD, "run_id", None))


def test_empty_file_lists_all_missing():
    with pytest.raises(FactsError) as excinfo:
        parse_facts("")
    message = str(excinfo.value)
    for key in (
        "schema",
        "generator",
        "kind",
        "run_id",
        "job_id",
        "try",
        "host",
        "pid",
        "timeout_impl",
        "started_at",
        "state",
    ):
        assert key in message


def test_crlf():
    facts = parse_facts((HEAD + TAIL).replace("\n", "\r\n"))
    assert facts.state == "done"
    assert facts.rc == 0
    assert facts.host == "simbox01"
    assert facts.duration_s == 398.214


def test_value_with_equals():
    facts = parse_facts(HEAD + "setup=a=b\n" + TAIL)
    assert facts.state == "done"
    facts = parse_facts(_replace(HEAD, "host", "a=b"))
    assert facts.host == "a=b"


def test_blank_and_garbage_lines_ignored():
    facts = parse_facts("\n\n" + HEAD + "\nno equals sign here\n\n" + TAIL)
    assert facts.state == "done"


def test_try_must_be_number():
    with pytest.raises(FactsError, match="try"):
        parse_facts(_replace(HEAD, "try", "abc"))
    with pytest.raises(FactsError, match="try"):
        parse_facts(_replace(HEAD, "try", "0"))
    assert parse_facts(_replace(HEAD, "try", "1")).try_ == 1


def test_pid_must_be_number():
    with pytest.raises(FactsError, match="pid"):
        parse_facts(_replace(HEAD, "pid", "x"))


@pytest.mark.parametrize(("key", "value"), [("kind", "other"), ("state", "weird")])
def test_bad_enum_values(key, value):
    with pytest.raises(FactsError, match=key):
        parse_facts(_replace(HEAD, key, value))


@pytest.mark.parametrize("key", ["rc", "finished_at", "duration_s"])
def test_done_missing_tail_field(key):
    with pytest.raises(FactsError, match=key):
        parse_facts(HEAD + _replace(TAIL, key, None))


def test_bad_rc():
    with pytest.raises(FactsError, match="rc"):
        parse_facts(HEAD + _replace(TAIL, "rc", "x"))


def test_signal():
    assert parse_facts(HEAD + TAIL).signal is None
    facts = parse_facts(HEAD + _replace(TAIL, "signal", "SIGSEGV"))
    assert facts.signal == "SIGSEGV"


def test_timed_out_and_hung_flags():
    tail = _replace(TAIL, "timed_out", "1")
    tail = _replace(tail, "hung", "1")
    tail = _replace(tail, "rc", "124")
    facts = parse_facts(HEAD + tail)
    assert facts.timed_out is True
    assert facts.hung is True
    assert facts.rc == 124
    with pytest.raises(FactsError, match="timed_out"):
        parse_facts(HEAD + _replace(TAIL, "timed_out", "2"))


def test_read_facts_missing(tmp_path):
    assert read_facts(tmp_path / "nope" / "job.facts") is None


def test_read_facts_matches_parse(tmp_path):
    path = tmp_path / "job.facts"
    path.write_text(HEAD + TAIL, encoding="utf-8")
    assert read_facts(path) == parse_facts(HEAD + TAIL)


def test_read_facts_broken_file_raises(tmp_path):
    path = tmp_path / "job.facts"
    path.write_text("schema=1\n", encoding="utf-8")
    with pytest.raises(FactsError):
        read_facts(path)
