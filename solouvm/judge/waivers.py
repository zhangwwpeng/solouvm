"""Waiver（verdict.md §5）：从计数中扣掉已知、已评估过的告警或错误行。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from solouvm.judge.scan import ScanResult
    from solouvm.model import Waiver

__all__ = ["WaiverResult", "apply_waivers", "waiver_label"]
SEVERITIES = ("INFO", "WARNING", "ERROR", "FATAL")


@dataclass
class WaiverResult:
    counts: dict[str, int]  # 扣除后的计数（INFO/WARNING/ERROR/FATAL/SIM）
    used: list[dict[str, object]]  # [{"id": ..., "lines": n}]，每条生效的 waiver 一项
    expired: list[str]  # 已过期的 waiver 标识
    waived_lines: set[int]  # 被豁免的行号
    waived: dict[str, int]  # 每个级别被豁免的行数


def waiver_label(waiver: Waiver) -> str:
    """报告中用来指代一条 waiver 的标识：优先用 id，否则用正则。"""
    return waiver.id if waiver.id is not None else f"regex '{waiver.regex}'"


class WaiverAccumulator:
    def __init__(self, waivers: tuple[Waiver, ...], today: date) -> None:
        self.active: list[tuple[Waiver, re.Pattern[str] | None]] = []
        self.expired: list[str] = []
        self.waived = dict.fromkeys((*SEVERITIES, "SIM"), 0)
        for waiver in waivers:
            if waiver.until is not None and date.fromisoformat(waiver.until) < today:
                self.expired.append(waiver_label(waiver))
                continue
            regex = re.compile(waiver.regex) if waiver.regex is not None else None
            self.active.append((waiver, regex))
        self.used_count = [0] * len(self.active)

    def match(self, severity: str, msg_id: str | None, text: str) -> bool:
        for index, (waiver, regex) in enumerate(self.active):
            if severity not in waiver.severity:
                continue
            if waiver.id is not None and waiver.id != msg_id:
                continue
            if regex is not None and not regex.search(text):
                continue
            if waiver.max is not None and self.used_count[index] >= waiver.max:
                continue
            self.used_count[index] += 1
            self.waived[severity] += 1
            return True
        return False

    def finish(self, uvm_counts: dict[str, int], sim_count: int) -> WaiverResult:
        counts = {sev: max(0, uvm_counts.get(sev, 0) - self.waived[sev]) for sev in SEVERITIES}
        counts["SIM"] = max(0, sim_count - self.waived["SIM"])
        return WaiverResult(
            counts=counts,
            used=[
                {"id": waiver_label(waiver), "lines": self.used_count[index]}
                for index, (waiver, _) in enumerate(self.active)
            ],
            expired=self.expired,
            waived_lines=set(),
            waived=self.waived,
        )


def apply_waivers(waivers: tuple[Waiver, ...], scan: ScanResult, *, today: date) -> WaiverResult:
    """Apply waivers to the scanned lines; return the reduced counts and usage."""
    if scan.waiver_result is not None:
        return scan.waiver_result
    accumulator = WaiverAccumulator(waivers, today)

    # 候选行：UVM WARNING/ERROR/FATAL 消息与仿真器错误（级别 SIM、没有 ID）
    candidates: list[tuple[int, str, str | None, str]] = list(scan.uvm_msgs)
    candidates.extend((lineno, "SIM", None, text) for lineno, text in scan.sim_errors)
    candidates.sort(key=lambda c: c[0])

    waived_lines: set[int] = set()
    for lineno, severity, msg_id, text in candidates:
        if accumulator.match(severity, msg_id, text):
            waived_lines.add(lineno)
    result = accumulator.finish(scan.uvm_counts, len(scan.sim_errors))
    result.waived_lines = waived_lines
    return result
