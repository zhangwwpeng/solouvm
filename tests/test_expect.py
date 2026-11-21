"""任务 9：expect（config.md §5.5）。"""

import io

from solouvm.backends import get_backend
from solouvm.judge.expect import apply_expect
from solouvm.judge.scan import scan
from solouvm.model import Checks, Expect

SB = "UVM_INFO a.sv(9) @ 900ns: uvm_test_top.env.scb [AXI_SCB] compared={n} mismatched=0\n"
DONE = "UVM_INFO a.sv(1) @ 52340ns: reporter [TEST_DONE] 'run' phase is ready to proceed\n"


def _run(expects, text):
    result = scan(io.StringIO(text), checks=Checks(), backend=get_backend("vcs"), expects=expects)
    return apply_expect(expects, result)


def test_scoreboard_pass_uses_last_value():
    (r,) = _run([Expect(scoreboard="AXI_SCB", min=100)], SB.format(n=5) + SB.format(n=412))
    assert r.ok is True
    assert r.value == 412
    assert r.rule == "scoreboard AXI_SCB min 100"


def test_scoreboard_fail():
    (r,) = _run([Expect(scoreboard="AXI_SCB", min=100)], SB.format(n=0))
    assert r.ok is False
    assert r.value == 0
    assert "AXI_SCB compared=0 < 100" in r.detail


def test_scoreboard_missing_fails():
    (r,) = _run([Expect(scoreboard="AXI_SCB", min=1)], "nothing\n")
    assert r.ok is False
    assert r.value is None


def test_scoreboard_max():
    (r,) = _run([Expect(scoreboard="AXI_SCB", max=10)], SB.format(n=11))
    assert r.ok is False


def test_regex_count_min_pass_and_fail():
    log = "[AXI_BURST_VSEQ] start\nfoo\n[AXI_BURST_VSEQ] start\n"
    (ok,) = _run([Expect(regex=r"\[AXI_BURST_VSEQ\] start", count_min=1)], log)
    assert ok.ok is True
    assert ok.value == 2
    (bad,) = _run([Expect(regex=r"\[AXI_BURST_VSEQ\] start", count_min=3)], log)
    assert bad.ok is False
    assert bad.value == 2


def test_regex_count_max_zero():
    exp = Expect(regex=r"\[AXI_SCB\] .*mismatch", count_max=0)
    (ok,) = _run([exp], "clean\n")
    assert ok.ok is True
    assert ok.value == 0
    assert ok.rule == "regex '\\[AXI_SCB\\] .*mismatch' count_max 0"
    (bad,) = _run([exp], "[AXI_SCB] data mismatch\n")
    assert bad.ok is False
    assert bad.value == 1


def test_regex_only_requires_one_hit():
    (r,) = _run([Expect(regex="hello")], "nothing\n")
    assert r.ok is False


def test_regex_capture_group_min_max():
    log = "bytes=10\nbytes=200\n"
    (ok,) = _run([Expect(regex=r"bytes=(\d+)", min=100)], log)
    assert ok.ok is True
    assert ok.value == 200
    (bad,) = _run([Expect(regex=r"bytes=(\d+)", max=100)], log)
    assert bad.ok is False
    assert bad.value == 200


def test_regex_capture_no_match_fails():
    (r,) = _run([Expect(regex=r"bytes=(\d+)", min=1)], "nothing\n")
    assert r.ok is False


def test_sim_time_min_pass_and_fail():
    (ok,) = _run([Expect(sim_time_min="50us")], DONE)
    assert ok.ok is True
    assert ok.value == 52340.0
    (bad,) = _run([Expect(sim_time_min="60us")], DONE)
    assert bad.ok is False
    assert bad.value == 52340.0


def test_sim_time_unknown_fails():
    (r,) = _run([Expect(sim_time_min="50us")], "no time here\n")
    assert r.ok is False
    assert r.value is None
    assert "cannot determine simulation end time" in r.detail


def test_uvm_info_min_pass_and_fail():
    log = SB.format(n=1)
    (ok,) = _run([Expect(uvm_info_min=1, id="AXI_SCB")], log)
    assert ok.ok is True
    assert ok.value == 1
    (bad,) = _run([Expect(uvm_info_min=1, id="AXI_DRV")], log)
    assert bad.ok is False
    assert bad.value == 0


def test_multiple_expects_in_order():
    results = _run(
        [Expect(scoreboard="AXI_SCB", min=1), Expect(sim_time_min="1ns")], SB.format(n=3) + DONE
    )
    assert [r.ok for r in results] == [True, True]
    assert [r.value for r in results] == [3, 52340.0]
