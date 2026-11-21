import dataclasses
import math

import pytest

from solouvm import Case, Checks, Expect, Stage, Waiver
from solouvm.errors import ConfigError
from solouvm.units import parse_sim_time_ns


def _raises_case_invalid(func, *args, **kwargs) -> ConfigError:
    with pytest.raises(ConfigError) as excinfo:
        func(*args, **kwargs)
    assert excinfo.value.code == "CASE_INVALID"
    return excinfo.value


# ---------- Case ----------


def test_minimal_case():
    case = Case(name="smoke")
    assert case.stage == "wip"
    assert case.params == {}
    assert Case(name="a").params is not Case(name="b").params


def test_valid_names():
    for name in ("smoke", "a_b", "a-b.c", "A1"):
        Case(name=name)


@pytest.mark.parametrize("name", ["a b", "a__b", "", "a/b", 1])
def test_invalid_name(name):
    err = _raises_case_invalid(Case, name=name)
    assert "name" in str(err)
    assert repr(name) in str(err)


def test_stage():
    err = _raises_case_invalid(Case, name="c", stage="done")
    assert "stage" in str(err) and "'done'" in str(err)
    assert Case(name="c", stage=Stage.READY).stage == "ready"
    assert Case(name="c", stage="ready").stage == Stage.READY


def test_expect_fail():
    Case(name="c", expect_fail=True)
    Case(name="c", expect_fail="TEST FAILED")
    err = _raises_case_invalid(Case, name="c", expect_fail="[unclosed")
    assert "expect_fail" in str(err) and "'[unclosed'" in str(err)
    _raises_case_invalid(Case, name="c", expect_fail="")
    _raises_case_invalid(Case, name="c", expect_fail=1)


def test_timeout_and_hang():
    Case(name="c", timeout="30m", hang="5m")
    err = _raises_case_invalid(Case, name="c", timeout="5d")
    assert "timeout" in str(err) and "'5d'" in str(err)
    _raises_case_invalid(Case, name="c", hang="x")


def test_retry():
    Case(name="c", retry=0)
    Case(name="c", retry=2)
    for bad in (-1, True, "1"):
        err = _raises_case_invalid(Case, name="c", retry=bad)
        assert "retry" in str(err)


def test_params():
    Case(name="c", params={"NUM": 32, "FAST": True, "X": 1.5, "S": "a"})
    err = _raises_case_invalid(Case, name="c", params={"1BAD": 1})
    assert "params" in str(err) and "'1BAD'" in str(err)
    err = _raises_case_invalid(Case, name="c", params={"N": [1]})
    assert "params" in str(err) and "[1]" in str(err)


def test_checks_keys():
    Case(name="c", checks={"max_uvm_error": 1})
    err = _raises_case_invalid(Case, name="c", checks={"max_uvm_err": 1})
    assert "unknown checks key 'max_uvm_err'" in str(err)
    _raises_case_invalid(Case, name="c", checks={"fail_regex": ["[bad"]})


def test_notes_duplicate_id():
    from solouvm import Bug

    with pytest.raises(ConfigError) as excinfo:
        Case(
            name="c",
            notes=[
                Bug(id=1, title="a", found="2026-11-02"),
                Bug(id=1, title="b", found="2026-11-02"),
            ],
        )
    assert "duplicate" in str(excinfo.value)
    Case(
        name="c",
        notes=[Bug(id=1, title="a", found="2026-11-02"), Bug(id=2, title="b", found="2026-11-02")],
    )


def test_doc_dedented():
    case = Case(
        name="c",
        doc="""
        line one
          indented
        """,
    )
    assert case.doc == "line one\n  indented"


# ---------- Waiver ----------


def test_waiver_reason():
    with pytest.raises(TypeError):
        Waiver(id="X")  # 缺 reason：dataclass 必填参数
    err = _raises_case_invalid(Waiver, id="X", reason="")
    assert "reason" in str(err)


def test_waiver_id_or_regex():
    _raises_case_invalid(Waiver, reason="r")
    Waiver(id="AXI_SCB.LATE", reason="r")
    Waiver(regex=r"\[AXI_SCB\] late", reason="r", max=3)
    _raises_case_invalid(Waiver, regex="[bad", reason="r")


def test_waiver_severity():
    assert Waiver(regex="x", reason="r", severity=("SIM",)).severity == ("SIM",)
    err = _raises_case_invalid(Waiver, regex="x", reason="r", severity=("BOGUS",))
    assert "severity" in str(err) and "BOGUS" in str(err)
    _raises_case_invalid(Waiver, regex="x", reason="r", severity="ERROR")


def test_waiver_until():
    Waiver(regex="x", reason="r", until="2026-12-31")
    for bad in ("2026-13-01", "2026/12/31", "2026-02-30", "20261231"):
        err = _raises_case_invalid(Waiver, regex="x", reason="r", until=bad)
        assert "until" in str(err) and repr(bad) in str(err)


# ---------- Expect ----------


def test_expect_empty():
    _raises_case_invalid(Expect)


def test_expect_scoreboard():
    err = _raises_case_invalid(Expect, scoreboard="AXI_SCB")
    assert "scoreboard" in str(err) and "'AXI_SCB'" in str(err)
    Expect(scoreboard="AXI_SCB", min=100)
    Expect(scoreboard="AXI_SCB", max=0)


def test_expect_uvm_info():
    _raises_case_invalid(Expect, uvm_info_min=1)
    Expect(uvm_info_min=1, id="AXI_SCB")


def test_expect_regex():
    Expect(regex=r"\[AXI_BURST_VSEQ\] start", count_min=1)
    _raises_case_invalid(Expect, regex="[bad")


def test_expect_sim_time():
    assert Expect(sim_time_min="50us").sim_time_ns == 50000.0
    err = _raises_case_invalid(Expect, sim_time_min="5d")
    assert "sim_time_min" in str(err) and "'5d'" in str(err)


# ---------- Checks ----------


def test_checks_frozen_and_normalized():
    checks = Checks(fail_regex=["a", "b"])
    assert checks.fail_regex == ("a", "b")
    with pytest.raises(dataclasses.FrozenInstanceError):
        checks.max_uvm_error = 3  # type: ignore[misc]


def test_checks_from_dict_override():
    base = Checks(max_uvm_error=0, fail_regex=("x",))
    merged = Checks.from_dict({"max_uvm_error": 2}, base)
    assert merged.max_uvm_error == 2
    assert merged.fail_regex == ("x",)


def test_checks_from_dict_waivers_append():
    w1 = Waiver(id="A", reason="r")
    w2 = Waiver(id="B", reason="r")
    base = Checks(waivers=(w1,))
    merged = Checks.from_dict({"waivers": [w2]}, base)
    assert merged.waivers == (*base.waivers, w2)


def test_checks_from_dict_unknown():
    err = _raises_case_invalid(Checks.from_dict, {"bogus": 1}, Checks())
    assert "unknown checks key 'bogus'" in str(err)


def test_checks_bad_values():
    _raises_case_invalid(Checks, max_uvm_error=-1)
    _raises_case_invalid(Checks, max_uvm_error=True)
    _raises_case_invalid(Checks, pass_regex=("[bad",))
    _raises_case_invalid(Checks, waivers=("not a waiver",))


# ---------- parse_sim_time_ns ----------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("50us", 50000.0),
        ("1.5ms", 1500000.0),
        ("100", 100.0),
        ("2s", 2000000000.0),
        ("1e3ns", 1000.0),
        ("1e3s", 1e12),
        ("10ps", 0.01),
        (100, 100.0),
        (1.5, 1.5),
    ],
)
def test_parse_sim_time_valid(value, expected):
    result = parse_sim_time_ns(value)
    assert isinstance(result, float)
    assert math.isclose(result, expected)


def test_parse_sim_time_type():
    with pytest.raises(TypeError):
        parse_sim_time_ns(True)


@pytest.mark.parametrize("value", [-1, "-1", "", "5d", "infs", float("nan"), float("inf")])
def test_parse_sim_time_invalid(value):
    with pytest.raises(ValueError) as excinfo:
        parse_sim_time_ns(value)
    assert repr(value) in str(excinfo.value)
