"""任务 9：waiver（verdict.md §5）。"""

import io
from datetime import date

from solouvm.backends import get_backend
from solouvm.judge.scan import scan
from solouvm.judge.waivers import apply_waivers
from solouvm.model import Checks, Waiver

TODAY = date(2026, 11, 17)


def _err(n, msg_id="AXI_SCB", sev="ERROR", text="mismatch"):
    return f"UVM_{sev} verif/a.sv({n}) @ {n}0ns: uvm_test_top.env.scb [{msg_id}] {text} {n}\n"


def _scan(text):
    return scan(io.StringIO(text), checks=Checks(), backend=get_backend("vcs"))


def test_waive_by_id():
    result = apply_waivers((Waiver(id="AXI_SCB", reason="known"),), _scan(_err(1)), today=TODAY)
    assert result.counts["ERROR"] == 0
    assert result.used == [{"id": "AXI_SCB", "lines": 1}]
    assert result.waived_lines == {1}
    assert result.expired == []


def test_waive_by_id_counts_from_summary():
    log = (
        _err(1)
        + _err(2, "OTHER")
        + (
            "** Report counts by severity\n"
            "UVM_INFO : 0\nUVM_WARNING : 0\nUVM_ERROR : 2\nUVM_FATAL : 0\n"
        )
    )
    result = apply_waivers((Waiver(id="AXI_SCB", reason="known"),), _scan(log), today=TODAY)
    assert result.counts["ERROR"] == 1
    assert result.waived["ERROR"] == 1


def test_waive_by_regex():
    log = _err(1, text="late response") + _err(2, text="mismatch")
    result = apply_waivers((Waiver(regex="late response", reason="r"),), _scan(log), today=TODAY)
    assert result.counts["ERROR"] == 1
    assert result.waived_lines == {1}


def test_id_and_regex_must_both_match():
    log = _err(1, "AXI_SCB", text="late") + _err(2, "OTHER", text="late") + _err(3, text="bad")
    waiver = Waiver(id="AXI_SCB", regex="late", reason="r")
    result = apply_waivers((waiver,), _scan(log), today=TODAY)
    assert result.waived_lines == {1}
    assert result.counts["ERROR"] == 2


def test_sim_error_needs_explicit_sim_severity():
    log = "Error-[NOA] Null object access\n"
    default = apply_waivers((Waiver(regex="NOA", reason="r"),), _scan(log), today=TODAY)
    assert default.waived_lines == set()
    assert default.counts["SIM"] == 1
    sim = apply_waivers(
        (Waiver(regex="NOA", severity=("SIM",), reason="r"),), _scan(log), today=TODAY
    )
    assert sim.waived_lines == {1}
    assert sim.counts["SIM"] == 0


def test_fatal_needs_explicit_severity():
    log = _err(1, sev="FATAL")
    default = apply_waivers((Waiver(id="AXI_SCB", reason="r"),), _scan(log), today=TODAY)
    assert default.counts["FATAL"] == 1
    explicit = apply_waivers(
        (Waiver(id="AXI_SCB", severity=("FATAL",), reason="r"),), _scan(log), today=TODAY
    )
    assert explicit.counts["FATAL"] == 0


def test_max_limits_waived_lines():
    log = _err(1) + _err(2) + _err(3)
    result = apply_waivers((Waiver(id="AXI_SCB", max=1, reason="r"),), _scan(log), today=TODAY)
    assert result.counts["ERROR"] == 2
    assert result.used == [{"id": "AXI_SCB", "lines": 1}]
    assert result.waived_lines == {1}


def test_expired_waiver_not_applied():
    waiver = Waiver(id="AXI_SCB", until="2026-11-16", reason="r")
    result = apply_waivers((waiver,), _scan(_err(1)), today=TODAY)
    assert result.counts["ERROR"] == 1
    assert result.expired == ["AXI_SCB"]
    assert result.used == []


def test_until_today_still_active():
    waiver = Waiver(id="AXI_SCB", until="2026-11-17", reason="r")
    result = apply_waivers((waiver,), _scan(_err(1)), today=TODAY)
    assert result.counts["ERROR"] == 0
    assert result.expired == []


def test_line_belongs_to_first_matching_waiver():
    first = Waiver(id="AXI_SCB", reason="a")
    second = Waiver(regex="mismatch", reason="b")
    result = apply_waivers((first, second), _scan(_err(1)), today=TODAY)
    assert result.used == [{"id": "AXI_SCB", "lines": 1}, {"id": "regex 'mismatch'", "lines": 0}]
    assert result.counts["ERROR"] == 0


def test_warning_waived_by_default_info_untouched():
    log = _err(1, sev="WARNING") + _err(2, sev="INFO")
    result = apply_waivers((Waiver(id="AXI_SCB", reason="r"),), _scan(log), today=TODAY)
    assert result.counts["WARNING"] == 0
    assert result.counts["INFO"] == 1
