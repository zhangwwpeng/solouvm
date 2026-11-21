import math

import pytest

from solouvm.units import parse_duration, parse_sim_time_ns


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (90, 90.0),
        (0, 0.0),
        (1.5, 1.5),
        ("90", 90.0),
        ("90s", 90.0),
        ("30m", 1800.0),
        ("2h", 7200.0),
        ("1.5h", 5400.0),
        (" 30M ", 1800.0),
        ("0s", 0.0),
        ("1.5", 1.5),
        ("1e3s", 1000.0),
    ],
)
def test_parse_duration_valid(value, expected):
    result = parse_duration(value)
    assert result == expected
    assert isinstance(result, float)


@pytest.mark.parametrize(
    "value",
    ["", "   ", "5d", "abc", "m", "-5m", -1, -0.5, "²", "infs", float("nan"), float("inf")],
)
def test_parse_duration_invalid_value(value):
    with pytest.raises(ValueError) as excinfo:
        parse_duration(value)
    # 错误信息里要能看到用户写的原始值
    assert repr(value) in str(excinfo.value)


@pytest.mark.parametrize("value", [True, False, None, [30], {"t": 1}])
def test_parse_duration_invalid_type(value):
    # 类型不对用 TypeError，值不对用 ValueError（Python 惯例）
    with pytest.raises(TypeError):
        parse_duration(value)


# ---------- 回归：评审发现的边界 ----------


@pytest.mark.parametrize("func", [parse_duration, parse_sim_time_ns])
def test_huge_int_is_value_error(func):
    # float(10**400) 本身抛 OverflowError，这里要统一成 ValueError
    with pytest.raises(ValueError, match="finite"):
        func(10**400)


@pytest.mark.parametrize("func", [parse_duration, parse_sim_time_ns])
@pytest.mark.parametrize("value", ["-0", "-0.0", -0.0])
def test_negative_zero_becomes_positive_zero(func, value):
    result = func(value)
    assert result == 0.0
    assert math.copysign(1.0, result) == 1.0  # 不能是 -0.0
