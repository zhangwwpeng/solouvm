"""单遍日志扫描（verdict.md §4）：一次遍历、恒定内存，取出判定需要的全部信息。"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from solouvm.units import parse_sim_time_ns

if TYPE_CHECKING:
    from solouvm.backends import Backend
    from solouvm.model import Checks, Expect

__all__ = ["SEVERITIES", "ScanResult", "scan"]

SEVERITIES = ("INFO", "WARNING", "ERROR", "FATAL")

_SUMMARY_HEADER = "** Report counts by severity"
_SUMMARY_COUNT = re.compile(r"^\s*UVM_(INFO|WARNING|ERROR|FATAL)\s*:\s*(\d+)\s*$")
# UVM 消息行：UVM_ERROR <文件>(<行>) @ <时间>: <reporter> [<ID>] <消息>
# 文件与行号可能被 +UVM_REPORT_DISABLE_FILE_LINE 关掉，匹配不依赖它们。
_UVM_MSG = re.compile(r"^\s*UVM_(INFO|WARNING|ERROR|FATAL)\b")
_UVM_TIME_ID = re.compile(r"@\s*([^:]*?)\s*:\s*(?:\S+\s+)?\[([^\]\s]+)\]")
_DONE_MARKER = re.compile(r"SOLOUVM_DONE\s+status=(\S+)")
# scoreboard 汇总打印（uvm-style.md §5.2）：[<ID>] compared=<N> ...
_COMPARED = re.compile(r"\[([^\]\s]+)\]\s*compared=(\d+)")


@dataclass
class ScanResult:
    uvm_counts: dict[str, int]
    uvm_lines: dict[str, list[int]]
    sim_errors: list[tuple[int, str]]
    fail_hits: list[tuple[int, str]]
    pass_hits: list[tuple[int, str]]
    env_fail: tuple[int, str] | None
    first_error: tuple[int, str] | None
    last_uvm_id: str | None
    info_by_id: dict[str, int]
    sim_end_ns: float | None
    line_count: int
    is_uvm: bool
    has_summary: bool
    test_top_seen: bool
    done_marker: str | None
    # 以下字段供 waiver / expect 使用（任务 9），带默认值以兼容直接构造
    # WARNING / ERROR / FATAL 消息：(行号, 级别, ID 或 None, 行文本)
    uvm_msgs: list[tuple[int, str, str | None, str]] = field(default_factory=list)
    # 每个 scoreboard ID 最后一次 compared=<N> 的 N
    scoreboard: dict[str, int] = field(default_factory=dict)
    # expect 正则 -> (匹配行数, 最后一次匹配的第 1 个捕获分组)
    expect_hits: dict[str, tuple[int, str | None]] = field(default_factory=dict)


def scan(
    lines: Iterable[str],
    *,
    checks: Checks,
    backend: Backend,
    expects: Iterable[Expect] = (),
) -> ScanResult:
    """Scan a log once, line by line, collecting everything the verdict rules need."""
    error_res = _compile(backend.error_regex)
    fail_res = _compile(checks.fail_regex)
    pass_res = _compile(checks.pass_regex)
    env_patterns = checks.env_fail_regex
    if env_patterns is None:
        env_patterns = backend.license_regex
    env_res = _compile(env_patterns)
    time_re = re.compile(backend.time_regex) if backend.time_regex else None
    sim_errors_count = getattr(checks, "sim_errors", True)
    expect_res = {
        e.regex: re.compile(e.regex) for e in expects if getattr(e, "regex", None) is not None
    }
    expect_counts: Counter[str] = Counter()
    expect_last: dict[str, str | None] = {}
    uvm_msgs: list[tuple[int, str, str | None, str]] = []
    scoreboard: dict[str, int] = {}

    summary_counts: dict[str, int] = {}
    in_summary = False
    line_counts: Counter[str] = Counter()
    uvm_lines: dict[str, list[int]] = {}
    sim_errors: list[tuple[int, str]] = []
    fail_hits: list[tuple[int, str]] = []
    pass_hits: list[tuple[int, str]] = []
    env_fail: tuple[int, str] | None = None
    first_error: tuple[int, str] | None = None
    last_uvm_id: str | None = None
    info_by_id: Counter[str] = Counter()
    test_done_time: str | None = None
    time_value: str | None = None
    is_uvm = False
    test_top_seen = False
    done_marker: str | None = None
    line_count = 0

    for lineno, raw in enumerate(lines, 1):
        line_count = lineno
        line = raw.rstrip("\r\n")
        failing = False  # 本行是否是"会导致失败"的行

        if "UVM_" in line:
            summary = _SUMMARY_COUNT.match(line)
            if summary:
                is_uvm = True
                if in_summary:
                    summary_counts[summary.group(1)] = int(summary.group(2))
            else:
                msg = _UVM_MSG.match(line)
                if msg:
                    is_uvm = True
                    severity = msg.group(1)
                    line_counts[severity] += 1
                    uvm_lines.setdefault(severity, []).append(lineno)
                    if severity in ("ERROR", "FATAL"):
                        failing = True
                    time_id = _UVM_TIME_ID.search(line, msg.end())
                    msg_id = time_id.group(2) if time_id else None
                    if severity != "INFO":
                        uvm_msgs.append((lineno, severity, msg_id, line))
                    if time_id:
                        last_uvm_id = msg_id
                        if severity == "INFO":
                            info_by_id[msg_id] += 1
                        if msg_id == "TEST_DONE":
                            test_done_time = time_id.group(1)
        if _SUMMARY_HEADER in line:
            in_summary = True

        if "compared=" in line:
            compared = _COMPARED.search(line)
            if compared:
                scoreboard[compared.group(1)] = int(compared.group(2))
        for pattern, regex in expect_res.items():
            hit = regex.search(line)
            if hit:
                expect_counts[pattern] += 1
                expect_last[pattern] = hit.group(1) if regex.groups else None
        if "uvm_test_top" in line:
            test_top_seen = True
        if "SOLOUVM_DONE" in line:
            marker = _DONE_MARKER.search(line)
            if marker:
                done_marker = marker.group(1)

        if _any(error_res, line):
            sim_errors.append((lineno, line))
            if sim_errors_count:
                failing = True
        if _any(fail_res, line):
            fail_hits.append((lineno, line))
            failing = True
        if pass_res and _any(pass_res, line):
            pass_hits.append((lineno, line))
        if env_fail is None and _any(env_res, line):
            env_fail = (lineno, line)
        if time_re is not None:
            found = time_re.search(line)
            if found and found.groups():
                time_value = found.group(1)

        # 逐行扫描，行号单调递增，第一条即行号最小者
        if failing and first_error is None:
            first_error = (lineno, line)

    has_summary = all(sev in summary_counts for sev in SEVERITIES)
    if has_summary:
        uvm_counts = {sev: summary_counts[sev] for sev in SEVERITIES}
    else:
        uvm_counts = {
            sev: max(summary_counts.get(sev, 0), line_counts.get(sev, 0))
            for sev in SEVERITIES
        }

    sim_end_ns = _sim_time(test_done_time)
    if sim_end_ns is None:
        sim_end_ns = _sim_time(time_value)

    return ScanResult(
        uvm_counts=uvm_counts,
        uvm_lines=uvm_lines,
        sim_errors=sim_errors,
        fail_hits=fail_hits,
        pass_hits=pass_hits,
        env_fail=env_fail,
        first_error=first_error,
        last_uvm_id=last_uvm_id,
        info_by_id=dict(info_by_id),
        sim_end_ns=sim_end_ns,
        line_count=line_count,
        is_uvm=is_uvm,
        has_summary=has_summary,
        test_top_seen=test_top_seen,
        done_marker=done_marker,
        uvm_msgs=uvm_msgs,
        scoreboard=scoreboard,
        expect_hits={p: (expect_counts[p], expect_last.get(p)) for p in expect_res},
    )


def _compile(patterns: Iterable[str] | None) -> list[re.Pattern[str]]:
    return [re.compile(p) for p in patterns or ()]


def _any(patterns: list[re.Pattern[str]], line: str) -> bool:
    return any(p.search(line) for p in patterns)


def _sim_time(text: str | None) -> float | None:
    """日志格式不可控：取不到或不合法都返回 None，不抛。"""
    if text is None:
        return None
    try:
        return parse_sim_time_ns("".join(text.split()))
    except (TypeError, ValueError):
        return None
