from __future__ import annotations

import pytest

from solouvm.logcheck import Judge, judge_file

BASE = {"max_uvm_error": 0, "max_uvm_fatal": 0, "pass_regex": [], "fail_regex": []}


def verdict(lines: list[str], rc: int | None = 0, **kwargs) -> dict:
    judge = Judge({**BASE, **kwargs.pop("checks", {})})
    for line in lines:
        judge.feed(line)
    return judge.verdict(rc=rc, **kwargs)


def test_clean_uvm_summary_passes():
    result = verdict(["UVM_INFO @ 0: reporter [RNTST] Running test", "UVM_ERROR : 0", "UVM_FATAL : 0"])
    assert result["status"] == "pass"
    assert result["uvm"]["error"] == 0


def test_uvm_error_line_fails_without_summary():
    result = verdict(["UVM_INFO @ 0: x", "UVM_ERROR @ 10: sb [MISMATCH] boom"])
    assert result["status"] == "fail"
    assert result["uvm"]["error"] == 1


def test_uvm_fatal_fails():
    result = verdict(["UVM_FATAL @ 5: env [X] dead"])
    assert result["status"] == "fail"
    assert "UVM_FATAL" in result["reason"]


def test_summary_wins_over_at_lines():
    lines = ["UVM_ERROR @ 1: sb [X] y", "UVM_ERROR : 0", "UVM_FATAL : 0"]
    result = verdict(lines)
    assert result["status"] == "pass"
    assert result["uvm"]["error"] == 0


def test_uvm_log_without_summary_fails():
    result = verdict(["UVM_INFO @ 0: tb [EARLY] died"])
    assert result["status"] == "fail"
    assert "Report Summary" in result["reason"]


def test_pass_regex_rescues_missing_summary():
    result = verdict(
        ["UVM_INFO @ 0: tb [EARLY] died", "TEST PASSED"],
        checks={"pass_regex": ["TEST PASSED"]},
    )
    assert result["status"] == "pass"


def test_pass_regex_cannot_rescue_hard_failure():
    result = verdict(
        ["UVM_FATAL @ 1: env [X] dead", "ALL GOOD"],
        checks={"pass_regex": ["ALL GOOD"]},
    )
    assert result["status"] == "fail"


def test_fail_regex_beats_clean_summary():
    result = verdict(
        ["UVM_ERROR : 0", "UVM_FATAL : 0", "TEST FAILED"],
        checks={"fail_regex": ["TEST FAILED"]},
    )
    assert result["status"] == "fail"
    assert result["checks_fired"][0]["kind"] == "fail_regex"


def test_fail_regex_beats_pass_regex():
    result = verdict(
        ["UVM_ERROR : 0", "TEST FAILED"],
        checks={"pass_regex": ["TEST"], "fail_regex": ["TEST FAILED"]},
    )
    assert result["status"] == "fail"


def test_max_uvm_error_limit():
    lines = ["UVM_ERROR : 2", "UVM_FATAL : 0"]
    assert verdict(lines)["status"] == "fail"
    assert verdict(lines, checks={"max_uvm_error": 2})["status"] == "pass"


def test_max_uvm_warning_limit():
    lines = ["UVM_WARNING : 3", "UVM_ERROR : 0", "UVM_FATAL : 0"]
    assert verdict(lines)["status"] == "pass"
    assert verdict(lines, checks={"max_uvm_warning": 2})["status"] == "fail"


def test_nonzero_rc_fails():
    assert verdict(["UVM_ERROR : 0"], rc=1)["status"] == "fail"


def test_timeout_wins_over_rc():
    assert verdict(["UVM_ERROR : 0"], rc=-9, timed_out=True)["status"] == "timeout"


def test_signal_wins_over_timeout():
    assert verdict([], rc=-11, signal="SIGSEGV", timed_out=True)["status"] == "killed"


def test_non_uvm_done_markers():
    assert verdict(["SOLOUVM_DONE checks=3 status=pass"])["status"] == "pass"
    assert verdict(["SOLOUVM_DONE checks=3 status=fail"])["status"] == "fail"
    assert verdict(["SOLOUVM_DONE checks=3 status=pass", "SOLOUVM_DONE status=fail"])["status"] == "fail"


def test_non_uvm_without_marker_fails():
    result = verdict(["just some output"])
    assert result["status"] == "fail"
    assert "SOLOUVM_DONE" in result["reason"]


def test_checks_fired_is_capped():
    judge = Judge({**BASE, "fail_regex": ["x"]})
    for _ in range(50):
        judge.feed("x")
    assert len(judge.fired) == 20


def test_judge_file_streams_and_records_line_numbers(tmp_path):
    log = tmp_path / "sim.log"
    log.write_text("UVM_INFO @ 0: x\nboom\nTEST FAILED\n")
    result = judge_file(log, checks={**BASE, "fail_regex": ["TEST FAILED"]}, rc=0)
    assert result["status"] == "fail"
    assert result["checks_fired"][0]["line"] == 3


def test_judge_file_missing_log_is_judged_on_process_data(tmp_path):
    result = judge_file(tmp_path / "absent.log", checks=BASE, rc=0)
    assert result["status"] == "fail"


def test_judge_file_tail(tmp_path):
    log = tmp_path / "sim.log"
    log.write_text("a\nb\nc\n")
    result = judge_file(log, checks=BASE, rc=0, tail=2)
    assert result["log_tail"] == ["b", "c"]


def test_invalid_regex_is_a_config_error():
    from solouvm.errors import ConfigError

    with pytest.raises(ConfigError, match="invalid regex"):
        Judge({**BASE, "fail_regex": ["("]})
