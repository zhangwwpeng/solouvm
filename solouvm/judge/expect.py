"""`expect`（config.md §5.5、verdict.md §3 规则 13）：通过时一定能观察到的现象。

数据全部来自单遍扫描的结果（J-12），这里不再读日志。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from solouvm.units import parse_sim_time_ns

if TYPE_CHECKING:
    from solouvm.judge.scan import ScanResult
    from solouvm.model import Expect

__all__ = ["ExpectResult", "apply_expect"]


@dataclass
class ExpectResult:
    rule: str  # 人类可读的规则描述，例如 "scoreboard AXI_SCB min 100"
    ok: bool
    value: object  # 实际观察到的值，进 results.json 的 expect[].value
    detail: str = ""  # 不满足时的说明，例如 "AXI_SCB compared=0 < 100"


def apply_expect(expects: list[Expect], scan: ScanResult) -> list[ExpectResult]:
    """Check every expect against the scan result; one result per checked field."""
    results: list[ExpectResult] = []
    for expect in expects:
        if expect.scoreboard is not None:
            results.append(_scoreboard(expect, scan))
        if expect.regex is not None:
            results.append(_regex(expect, scan))
        if expect.sim_time_min is not None:
            results.append(_sim_time(expect, scan))
        if expect.uvm_info_min is not None:
            results.append(_uvm_info(expect, scan))
    return results


def _limits(prefix: str, low: int | None, high: int | None) -> list[str]:
    parts = []
    if low is not None:
        parts.append(f"{prefix}min {low}")
    if high is not None:
        parts.append(f"{prefix}max {high}")
    return parts


def _check_range(label: str, value: float, low: int | None, high: int | None) -> str:
    """返回不满足时的说明；满足返回空串。"""
    if low is not None and value < low:
        return f"{label}={_fmt(value)} < {low}"
    if high is not None and value > high:
        return f"{label}={_fmt(value)} > {high}"
    return ""


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else str(value)


def _scoreboard(expect: Expect, scan: ScanResult) -> ExpectResult:
    sb = expect.scoreboard
    rule = " ".join([f"scoreboard {sb}", *_limits("", expect.min, expect.max)])
    value = scan.scoreboard.get(sb)  # type: ignore[arg-type]
    if value is None:
        return ExpectResult(rule, False, None, f"{sb} compared=<N> not found")
    detail = _check_range(f"{sb} compared", value, expect.min, expect.max)
    return ExpectResult(rule, not detail, value, detail)


def _regex(expect: Expect, scan: ScanResult) -> ExpectResult:
    pattern = expect.regex
    count_min, count_max = expect.count_min, expect.count_max
    has_value_limits = expect.min is not None or expect.max is not None
    if count_min is None and count_max is None and not has_value_limits:
        count_min = 1  # 只写了 regex：至少出现一次
    parts = [f"regex '{pattern}'"]
    parts += _limits("count_", count_min, count_max)
    parts += _limits("", expect.min, expect.max)
    rule = " ".join(parts)

    count, last = scan.expect_hits.get(pattern, (0, None))  # type: ignore[arg-type]
    detail = _check_range("count", count, count_min, count_max)
    if detail:
        return ExpectResult(rule, False, count, f"regex '{pattern}' {detail}")
    if not has_value_limits:
        return ExpectResult(rule, True, count)

    # min / max 检查最后一次匹配的第 1 个捕获分组
    if last is None:
        return ExpectResult(rule, False, None, f"regex '{pattern}' captured no value")
    try:
        number = float(last)
    except ValueError:
        return ExpectResult(rule, False, last, f"regex '{pattern}' captured non-number {last!r}")
    value: float = int(number) if number.is_integer() else number
    detail = _check_range("value", value, expect.min, expect.max)
    return ExpectResult(rule, not detail, value, f"regex '{pattern}' {detail}" if detail else "")


def _sim_time(expect: Expect, scan: ScanResult) -> ExpectResult:
    rule = f"sim_time_min {expect.sim_time_min}"
    if scan.sim_end_ns is None:
        return ExpectResult(rule, False, None, "cannot determine simulation end time")
    limit = (
        expect.sim_time_ns
        if expect.sim_time_ns is not None
        else parse_sim_time_ns(expect.sim_time_min)  # type: ignore[arg-type]
    )
    if scan.sim_end_ns < limit:
        detail = f"sim end {_fmt(scan.sim_end_ns)}ns < {expect.sim_time_min}"
        return ExpectResult(rule, False, scan.sim_end_ns, detail)
    return ExpectResult(rule, True, scan.sim_end_ns)


def _uvm_info(expect: Expect, scan: ScanResult) -> ExpectResult:
    rule = f"uvm_info_min {expect.uvm_info_min} id {expect.id}"
    observed = scan.info_by_id.get(expect.id, 0)  # type: ignore[arg-type]
    detail = _check_range(f"UVM_INFO [{expect.id}]", observed, expect.uvm_info_min, None)
    return ExpectResult(rule, not detail, observed, detail)
