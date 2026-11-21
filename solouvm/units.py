"""Units used in configuration values."""

import math

_UNITS = {"s": 1, "m": 60, "h": 3600}
_HINT = f"expected a number with an optional unit ({', '.join(_UNITS)}), e.g. '90', '30m', '1.5h'"

# 仿真时间统一换算成纳秒；键的顺序就是报错提示里的顺序
_SIM_UNITS = {"fs": 1e-6, "ps": 1e-3, "ns": 1.0, "us": 1e3, "ms": 1e6, "s": 1e9}
_SIM_HINT = (
    f"expected a number with an optional unit ({', '.join(_SIM_UNITS)}), "
    "e.g. '100', '50us', '1.5ms'"
)


def parse_duration(value: str | float) -> float:
    """Return a duration in seconds; accepts 90, 1.5, "90", "90s", "30m", "1.5h"."""
    # bool 是 int 的子类，必须在数字之前排除
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError(f"invalid duration {value!r}: expected a string or a number")

    seconds = _parse_text(value) if isinstance(value, str) else _to_float(value)

    # nan 与任何数比较都是 False，只写 seconds < 0 拦不住它
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"invalid duration {value!r}: must be a finite, non-negative number")
    # -0.0 < 0 为 False，能走到这里；abs 把它变成 0.0
    return abs(seconds)


def parse_sim_time_ns(value: str | float) -> float:
    """Return a simulation time in nanoseconds; accepts 50us, 1.5ms, 100, 2s."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise TypeError(f"invalid simulation time {value!r}: expected a string or a number")

    nanoseconds = _parse_sim_text(value) if isinstance(value, str) else _to_float(value)

    if not math.isfinite(nanoseconds) or nanoseconds < 0:
        raise ValueError(
            f"invalid simulation time {value!r}: must be a finite, non-negative number"
        )
    return abs(nanoseconds)


def _to_float(value: float) -> float:
    """Convert an int/float; ints too large for a float become inf.

    float(10**400) 抛的是 OverflowError；换成 inf，交给调用方的 isfinite 检查报 ValueError。
    """
    try:
        return float(value)
    except OverflowError:
        return math.inf


def _split_number_unit(text: str) -> tuple[str, str]:
    """Split "1.5ms" into ("1.5", "ms"); no unit gives an empty string.

    单位取末尾连续的一串字母，因此 "1e3s" 拆成 ("1e3", "s") 而不是 ("1", "e3s")。
    """
    text = text.strip().lower()
    end = len(text)
    while end > 0 and text[end - 1].isalpha():
        end -= 1
    return text[:end], text[end:]


def _parse_text(value: str) -> float:
    num_text, unit = _split_number_unit(value)

    factor = _UNITS.get(unit or "s")
    if factor is None:
        raise ValueError(f"invalid duration {value!r}: {_HINT}")

    try:
        number = float(num_text)
    except ValueError:
        raise ValueError(f"invalid duration {value!r}: {_HINT}") from None
    return number * factor


def _parse_sim_text(value: str) -> float:
    num_text, unit = _split_number_unit(value)

    factor = _SIM_UNITS.get(unit or "ns")
    if factor is None:
        raise ValueError(f"invalid simulation time {value!r}: {_SIM_HINT}")

    try:
        number = float(num_text)
    except ValueError:
        raise ValueError(f"invalid simulation time {value!r}: {_SIM_HINT}") from None
    return number * factor
