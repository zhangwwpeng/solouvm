import io

import pytest

from solouvm.backends import get_backend
from solouvm.judge.scan import scan
from solouvm.model import Checks

SUMMARY = """\
--- UVM Report Summary ---

** Report counts by severity
UVM_INFO :  340
UVM_WARNING :    2
UVM_ERROR :    5
UVM_FATAL :    0
** Report counts by id
[AXI_SCB]     3
"""


def _scan(text, checks=None, backend="vcs"):
    return scan(
        io.StringIO(text),
        checks=checks if checks is not None else Checks(),
        backend=get_backend(backend),
    )


def _err(n, msg_id="AXI_SCB"):
    return f"UVM_ERROR verif/a.sv({n}) @ {n}0ns: uvm_test_top.env.scb [{msg_id}] mismatch {n}\n"


def test_summary_counts_win():
    log = "UVM_INFO a.sv(1) @ 0: reporter [RNTST] Running test\n" + _err(1) + SUMMARY
    result = _scan(log)
    assert result.has_summary is True
    assert result.uvm_counts == {"INFO": 340, "WARNING": 2, "ERROR": 5, "FATAL": 0}


def test_line_counts_without_summary():
    log = "hello\n" + _err(1) + "UVM_WARNING @ 5: r [W] careful\n" + _err(2)
    result = _scan(log)
    assert result.has_summary is False
    assert result.uvm_counts["ERROR"] == 2
    assert result.uvm_counts["WARNING"] == 1
    assert result.uvm_counts["FATAL"] == 0


def test_summary_and_three_error_lines():
    log = _err(1) + _err(2) + _err(3) + SUMMARY
    result = _scan(log)
    assert result.uvm_counts["ERROR"] == 5
    assert len(result.uvm_lines["ERROR"]) == 3


def test_uvm_lines_are_real_line_numbers():
    log = "a\nb\n" + _err(1) + "c\n" + _err(2)
    result = _scan(log)
    assert result.uvm_lines["ERROR"] == [3, 5]


def test_summary_lines_before_header_are_not_counts():
    # 只认 "Report counts by severity" 之后的计数行
    log = "UVM_ERROR :    7\n" + _err(1)
    result = _scan(log)
    assert result.has_summary is False
    assert result.uvm_counts["ERROR"] == 1


def test_vcs_sim_errors():
    log = (
        "Info: ok\n"
        "Error-[NOA] Null object access\n"
        '"tb/top.sv", 42: top.u_chk: started at 100ns failed at 120ns\n'
        "\tOffending 'axi_wrap_chk'\n"
    )
    result = _scan(log)
    assert result.sim_errors == [
        (2, "Error-[NOA] Null object access"),
        (4, "\tOffending 'axi_wrap_chk'"),
    ]
    assert result.first_error == (2, "Error-[NOA] Null object access")
    assert result.is_uvm is False


def test_xrun_sim_errors():
    log = "xmsim: *W,FOO: warn\nxmsim: *E,ASRTST (./tb/top.sv,88): assertion failed\n"
    result = _scan(log, backend="xrun")
    assert [n for n, _ in result.sim_errors] == [2]


def test_sim_errors_disabled_not_first_error():
    log = "Error-[NOA] Null object access\n"
    result = _scan(log, checks=Checks(sim_errors=False))
    assert result.sim_errors == [(1, "Error-[NOA] Null object access")]
    assert result.first_error is None


def test_fail_regex_hits():
    log = "line\nTEST FAILED: bad\nok\n"
    result = _scan(log, checks=Checks(fail_regex=("TEST FAILED",)))
    assert result.fail_hits == [(2, "TEST FAILED: bad")]
    assert result.first_error == (2, "TEST FAILED: bad")


def test_pass_regex_hits():
    log = "line\n*** TEST PASSED ***\n"
    result = _scan(log, checks=Checks(pass_regex=(r"TEST PASSED",)))
    assert result.pass_hits == [(2, "*** TEST PASSED ***")]
    assert result.first_error is None


def test_first_error_smallest_line():
    lines = ["filler\n"] * 811
    lines[299] = "TEST FAILED here\n"
    lines.append(_err(812))  # 第 812 行
    result = _scan("".join(lines), checks=Checks(fail_regex=("TEST FAILED",)))
    assert result.uvm_lines["ERROR"] == [812]
    assert result.first_error == (300, "TEST FAILED here")


def test_first_error_uvm_fatal():
    log = "a\nUVM_FATAL @ 9: r [CFG] no vif\n" + _err(3)
    result = _scan(log)
    assert result.first_error == (2, "UVM_FATAL @ 9: r [CFG] no vif")


def test_uvm_info_and_warning_not_first_error():
    log = "UVM_INFO @ 0: r [A] hi\nUVM_WARNING @ 0: r [B] hmm\n"
    assert _scan(log).first_error is None


def test_env_fail_first_hit():
    log = "a\nFailed to obtain license X\nb\nFailed to obtain license Y\n"
    result = _scan(log)
    assert result.env_fail == (2, "Failed to obtain license X")


def test_env_fail_regex_none_uses_backend_license():
    log = "Failed to obtain license\n"
    checks = Checks(env_fail_regex=None)
    assert checks.env_fail_regex is None
    assert _scan(log, checks=checks).env_fail == (1, "Failed to obtain license")


def test_env_fail_regex_override_replaces_backend():
    log = "Failed to obtain license\nLICENSE DOWN\n"
    result = _scan(log, checks=Checks(env_fail_regex=("LICENSE DOWN",)))
    assert result.env_fail == (2, "LICENSE DOWN")


def test_sim_end_from_test_done():
    log = (
        "UVM_INFO a.sv(1) @ 0: reporter [RNTST] Running test\n"
        "UVM_INFO verif/uvm.sv(1) @ 52340ns: reporter [TEST_DONE] 'run' phase is ready to proceed\n"
        "Simulation complete via $finish(1) at time 99999 NS + 0\n"
    )
    assert _scan(log, backend="xrun").sim_end_ns == 52340.0


def test_sim_end_from_time_regex():
    log = "Simulation complete via $finish(1) at time 52340 NS + 0\n"
    assert _scan(log, backend="xrun").sim_end_ns == 52340.0


def test_sim_end_missing():
    assert _scan("nothing here\nUVM_INFO @ 5: r [X] hi\n").sim_end_ns is None


def test_sim_end_bad_test_done_time_falls_back():
    log = (
        "UVM_INFO @ soon: reporter [TEST_DONE] done\n"
        "Simulation complete via $finish(1) at time 7 NS + 0\n"
    )
    assert _scan(log, backend="xrun").sim_end_ns == 7.0


def test_non_uvm_log():
    result = _scan("hello\nworld\n")
    assert result.is_uvm is False
    assert result.line_count == 2


def test_is_uvm_with_info():
    assert _scan("UVM_INFO @ 0: r [A] hi\n").is_uvm is True


def test_done_marker():
    assert _scan("x\nSOLOUVM_DONE status=pass\n").done_marker == "pass"
    assert _scan("x\n").done_marker is None


def test_test_top_seen():
    assert _scan("UVM_INFO @ 0: uvm_test_top [A] hi\n").test_top_seen is True
    assert _scan("UVM_INFO @ 0: reporter [A] hi\n").test_top_seen is False


def test_info_by_id():
    log = (
        "UVM_INFO tb/scb.sv(10) @ 100ns: uvm_test_top.env.scb [AXI_SCB] compared=412\n"
        "UVM_INFO @ 200ns: uvm_test_top.env.agent[0].drv [AXI_DRV] sent\n"
    )
    result = _scan(log)
    assert result.info_by_id == {"AXI_SCB": 1, "AXI_DRV": 1}
    log += "UVM_INFO tb/scb.sv(10) @ 300ns: uvm_test_top.env.scb [AXI_SCB] compared=413\n"
    log += _err(4, msg_id="AXI_SCB")
    assert _scan(log).info_by_id["AXI_SCB"] == 2


def test_last_uvm_id():
    log = "UVM_INFO @ 0: r [FIRST] a\n" + _err(2, msg_id="LAST") + "plain line\n"
    assert _scan(log).last_uvm_id == "LAST"
    assert _scan("plain\n").last_uvm_id is None


def test_crlf_lines_stripped():
    result = _scan("a\r\nTEST FAILED\r\n", checks=Checks(fail_regex=("TEST FAILED",)))
    assert result.fail_hits == [(2, "TEST FAILED")]


def test_accepts_any_iterable():
    # 生成器：只能遍历一次，扫描器不能先 list() 再查
    gen = (line for line in ["UVM_ERROR @ 1: r [E] x\n", "y\n"])
    out = scan(gen, checks=Checks(), backend=get_backend("vcs"))
    assert out.line_count == 2
    assert out.uvm_counts["ERROR"] == 1


@pytest.mark.parametrize("source", [iter(()), io.StringIO(""), []])
def test_empty_log(source):
    result = scan(source, checks=Checks(), backend=get_backend("vcs"))
    assert result.line_count == 0
    assert result.uvm_counts == {"INFO": 0, "WARNING": 0, "ERROR": 0, "FATAL": 0}
    assert result.uvm_lines == {}
    assert result.sim_errors == []
    assert result.fail_hits == []
    assert result.pass_hits == []
    assert result.env_fail is None
    assert result.first_error is None
    assert result.last_uvm_id is None
    assert result.info_by_id == {}
    assert result.sim_end_ns is None
    assert result.is_uvm is False
    assert result.has_summary is False
    assert result.test_top_seen is False
    assert result.done_marker is None


def test_partial_summary_falls_back_to_line_counts():
    log = (
        "UVM_INFO a.sv(1) @ 0: uvm_test_top [RNTST] start\n"
        + _err(2)
        + "** Report counts by severity\nUVM_INFO : 1\n"
    )
    result = _scan(log)
    assert result.has_summary is False
    assert result.uvm_counts == {"INFO": 1, "WARNING": 0, "ERROR": 1, "FATAL": 0}
