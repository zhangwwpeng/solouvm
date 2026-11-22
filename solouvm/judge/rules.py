"""判定规则（verdict.md §3、§4.3、§6、§8）：把事实、扫描结果与 case 定义压成一个状态加一句原因。

规则顺序固定，先命中者为准；每条规则一个函数，`decide` 依次调用。
判定函数不抛业务异常：无法判定的情况落到 `error` 并写清原因。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date
from typing import TYPE_CHECKING

from solouvm.judge.expect import ExpectResult, apply_expect
from solouvm.judge.signature import normalize_signature, signature_id
from solouvm.judge.waivers import WaiverResult, apply_waivers

if TYPE_CHECKING:
    from collections.abc import Callable

    from solouvm.judge.scan import ScanResult
    from solouvm.model import Case, Checks, Facts

__all__ = ["CATEGORIES", "Verdict", "apply_expect_fail", "decide"]

_SUPPORTED_SCHEMA = 1
_GENERATOR_PREFIX = "solouvm "

# 状态 → 报告类别（verdict.md §2）
CATEGORIES: dict[str, str | None] = {
    "pass": None,
    "skip": None,
    "xfail": None,
    "fail": "design",
    "xpass": "design",
    "false_pass": "design",
    "timeout": "execution",
    "killed": "execution",
    "incomplete": "execution",
    "error": "environment",
}


@dataclass
class Verdict:
    status: str
    reason: str
    category: str | None
    first_error: tuple[int, str] | None
    signature: str | None
    waived: WaiverResult | None = field(default=None, compare=False, repr=False)
    expects: list[ExpectResult] | None = field(default=None, compare=False, repr=False)


@dataclass
class _Ctx:
    facts: Facts | None
    scan: ScanResult
    build_ok: bool
    build_facts: Facts | None
    build_key: str
    checks: Checks
    case: Case
    allow_missing: bool
    run_id: str | None
    facts_error: str | None
    build_error_line: int | None
    waived: WaiverResult
    expects: list[ExpectResult]
    partial: bool = False
    stopped_early: bool = False


@dataclass
class _Hit:
    """规则命中：状态、原因与签名用的原因类别（J-22）。"""

    status: str
    reason: str
    kind: str


def decide(
    *,
    facts: Facts | None,
    scan: ScanResult,
    build_ok: bool,
    build_facts: Facts | None,
    build_key: str,
    checks: Checks,
    case: Case,
    allow_missing: bool,
    run_id: str | None = None,
    facts_error: str | None = None,
    build_error_line: int | None = None,
    today: date | None = None,
    partial: bool = False,
    stopped_early: bool = False,
) -> Verdict:
    """Apply the fixed rule table (verdict.md §3) and return the raw verdict.

    `expect_fail` is not applied here; see `apply_expect_fail`.
    """
    try:
        waived = apply_waivers(checks.waivers, scan, today=today or date.today())
        ctx = _Ctx(
            facts=facts,
            scan=scan,
            build_ok=build_ok,
            build_facts=build_facts,
            build_key=build_key,
            checks=checks,
            case=case,
            allow_missing=allow_missing,
            run_id=run_id,
            facts_error=facts_error,
            build_error_line=build_error_line,
            waived=waived,
            expects=apply_expect(list(case.expect), scan),
            partial=partial,
            stopped_early=stopped_early,
        )
        hit = _first_hit(ctx)
        # 编译失败、没有 facts 等情况下日志并没有被读，首条错误无从谈起
        first = _first_error(ctx) if hit.kind not in _NO_LOG_KINDS else None
        return Verdict(
            status=hit.status,
            reason=hit.reason,
            category=CATEGORIES.get(hit.status),
            first_error=first,
            signature=_signature(hit, first, scan),
            waived=waived,
            expects=ctx.expects,
        )
    except Exception as exc:  # 判定绝不能让整轮回归拿不到结果
        return Verdict(
            status="error",
            reason=f"internal judge error: {type(exc).__name__}: {exc}",
            category="environment",
            first_error=None,
            signature="internal",
        )


def apply_expect_fail(verdict: Verdict, case: Case) -> Verdict:
    """Flip pass/fail according to `case.expect_fail` (verdict.md §6)."""
    expect_fail = case.expect_fail
    if expect_fail is False or verdict.status not in ("pass", "fail"):
        return verdict
    match verdict.status, expect_fail:
        case "pass", _:
            reason = "expected to fail but passed"
            return replace(verdict, status="xpass", reason=reason, category="design")
        case "fail", True:
            return replace(verdict, status="xfail", category=None)
        case "fail", str() as pattern:
            text = verdict.first_error[1] if verdict.first_error else None
            if text is not None and re.search(pattern, text):
                return replace(verdict, status="xfail", category=None)
            reason = f"failed for an unexpected reason: {verdict.reason}"
            return replace(verdict, reason=reason)
    return verdict


# ---------------------------------------------------------------- 规则


def _rule_0a(ctx: _Ctx) -> _Hit | None:
    if ctx.build_ok:
        return None
    bf = ctx.build_facts
    if bf is not None and bf.state == "done" and bf.rc is not None and bf.rc != 0:
        where = "see build.log"
        if ctx.build_error_line is not None:
            where += f" line {ctx.build_error_line}"
        return _Hit("error", f"build failed: {ctx.build_key}, {where}", "build")
    if bf is not None and bf.state == "interrupted":
        return _Hit("incomplete", f"build interrupted: {ctx.build_key}", "incomplete")
    if ctx.partial:
        if bf is not None and bf.state == "running":
            return _Hit("incomplete", f"build still running: {ctx.build_key}", "incomplete")
        return _Hit("incomplete", f"build did not finish: {ctx.build_key}", "incomplete")
    return _Hit("error", f"build not run: {ctx.build_key}", "build")


def _rule_0b(ctx: _Ctx) -> _Hit | None:
    if ctx.facts is not None or ctx.facts_error is not None:
        return None
    if ctx.allow_missing:
        return _Hit("skip", "not run: job.facts missing", "missing")
    if ctx.stopped_early:
        return _Hit("incomplete", "not run: stopped early", "incomplete")
    if ctx.partial:
        return _Hit("incomplete", "not run yet: job.facts missing", "incomplete")
    return _Hit("error", "not run: job.facts missing", "missing")


def _rule_0c(ctx: _Ctx) -> _Hit | None:
    facts = ctx.facts
    if facts is None or ctx.run_id is None or facts.run_id == ctx.run_id:
        return None
    return _Hit("error", f"stale facts from run {facts.run_id}", "stale")


def _rule_0d(ctx: _Ctx) -> _Hit | None:
    if ctx.facts_error is not None:  # 解析 job.facts 时的错误（FactsError）
        return _Hit("error", ctx.facts_error, "schema")
    facts = ctx.facts
    assert facts is not None
    if facts.schema != _SUPPORTED_SCHEMA:
        return _Hit("error", f"facts schema {facts.schema} not supported; regenerate", "schema")
    if not str(facts.generator).startswith(_GENERATOR_PREFIX):
        return _Hit(
            "error", f"facts generator {facts.generator!r} not supported; regenerate", "schema"
        )
    return None


def _rule_1(ctx: _Ctx) -> _Hit | None:
    facts = ctx.facts
    assert facts is not None
    # 只有开始段、被中断，或者结束段不完整（无法解释的组合）都算没跑完
    if facts.state != "done" or facts.rc is None:
        return _Hit("incomplete", f"job did not finish (host {facts.host})", "incomplete")
    return None


def _rule_2(ctx: _Ctx) -> _Hit | None:
    env = ctx.scan.env_fail
    if env is None:
        return None
    lineno, text = env
    return _Hit("error", f"environment failure at line {lineno}: {text.strip()}", "env")


def _rule_3(ctx: _Ctx) -> _Hit | None:
    facts = ctx.facts
    assert facts is not None
    if not facts.signal or facts.timed_out or facts.hung:
        return None
    return _Hit("killed", f"killed by {facts.signal}", "killed")


def _rule_4(ctx: _Ctx) -> _Hit | None:
    facts = ctx.facts
    assert facts is not None
    if facts.timed_out:
        limit = ctx.case.timeout or _fmt_duration(facts.duration_s)
        reason = f"wall-clock timeout after {limit}" if limit else "wall-clock timeout"
        return _Hit("timeout", reason, "timeout")
    if facts.hung:
        reason = f"no log output for {ctx.case.hang}" if ctx.case.hang else "no log output"
        return _Hit("timeout", reason, "timeout")
    return None


def _rule_5(ctx: _Ctx) -> _Hit | None:
    facts = ctx.facts
    assert facts is not None
    if facts.rc == 0:
        return None
    return _Hit("fail", f"exit code {facts.rc}", "exit")


def _rule_6(ctx: _Ctx) -> _Hit | None:
    if not ctx.checks.sim_errors:
        return None
    for lineno, text in ctx.scan.sim_errors:
        if lineno not in ctx.waived.waived_lines:
            return _Hit("fail", f"simulator error at line {lineno}: {text.strip()}", "sim")
    return None


def _rule_7(ctx: _Ctx) -> _Hit | None:
    if not ctx.scan.fail_hits:
        return None
    lineno, text = ctx.scan.fail_hits[0]
    pattern = next((p for p in ctx.checks.fail_regex if re.search(p, text)), "?")
    return _Hit("fail", f"fail_regex '{pattern}' at line {lineno}", "fail_regex")


def _uvm_limit(ctx: _Ctx, severity: str, limit: int | None) -> _Hit | None:
    if limit is None:
        return None
    count = ctx.waived.counts[severity]
    if count <= limit:
        return None
    reason = f"UVM_{severity}={count} > {limit}"
    waived = ctx.waived.waived[severity]
    if waived:
        reason += f" ({waived} waived)"
    return _Hit("fail", reason, severity.lower())


def _rule_8(ctx: _Ctx) -> _Hit | None:
    return _uvm_limit(ctx, "FATAL", ctx.checks.max_uvm_fatal)


def _rule_9(ctx: _Ctx) -> _Hit | None:
    return _uvm_limit(ctx, "ERROR", ctx.checks.max_uvm_error)


def _rule_10(ctx: _Ctx) -> _Hit | None:
    return _uvm_limit(ctx, "WARNING", ctx.checks.max_uvm_warning)


def _rule_11(ctx: _Ctx) -> _Hit | None:
    scan = ctx.scan
    if scan.is_uvm and not scan.has_summary and not scan.pass_hits:
        return _Hit("fail", "UVM log without Report Summary", "summary")
    return None


def _rule_12(ctx: _Ctx) -> _Hit | None:
    scan = ctx.scan
    if not scan.is_uvm and scan.done_marker != "pass":
        return _Hit("fail", "missing SOLOUVM_DONE marker", "marker")
    return None


def _rule_13(ctx: _Ctx) -> _Hit | None:
    for index, result in enumerate(ctx.expects):
        if result.ok:
            continue
        reason = f"expect not met: {result.detail or result.rule}"
        expect_reason = _expect_reason(ctx, index)
        if expect_reason:
            reason += f" ({expect_reason})"
        return _Hit("fail", reason, "expect")
    return None


def _rule_14(ctx: _Ctx) -> _Hit:
    return _Hit("pass", "clean", "pass")


_RULES: tuple[Callable[[_Ctx], _Hit | None], ...] = (
    _rule_0a,
    _rule_0b,
    _rule_0c,
    _rule_0d,
    _rule_1,
    _rule_2,
    _rule_3,
    _rule_4,
    _rule_5,
    _rule_6,
    _rule_7,
    _rule_8,
    _rule_9,
    _rule_10,
    _rule_11,
    _rule_12,
    _rule_13,
)

_NO_LOG_KINDS = frozenset({"build", "missing", "stale", "schema"})


def _first_hit(ctx: _Ctx) -> _Hit:
    for rule in _RULES:
        if (hit := rule(ctx)) is not None:
            return hit
    return _rule_14(ctx)


# ---------------------------------------------------------------- 首条错误与签名


def _first_error(ctx: _Ctx) -> tuple[int, str] | None:
    """行号最小的、未被豁免的错误行（verdict.md §4.3）。"""
    waived = ctx.waived.waived_lines
    candidates: list[tuple[int, str]] = []
    if ctx.checks.sim_errors:
        candidates += [e for e in ctx.scan.sim_errors if e[0] not in waived]
    candidates += [
        (lineno, text)
        for lineno, severity, _, text in ctx.scan.uvm_msgs
        if severity in ("ERROR", "FATAL") and lineno not in waived
    ]
    candidates += ctx.scan.fail_hits
    return min(candidates, key=lambda c: c[0], default=None)


def _signature(hit: _Hit, first: tuple[int, str] | None, scan: ScanResult) -> str | None:
    if hit.status in ("pass", "skip"):
        return None
    if first is not None:
        return signature_id(normalize_signature(first[1]))
    if scan.last_uvm_id and hit.kind not in _NO_LOG_KINDS:
        return f"{hit.kind}|{scan.last_uvm_id}"
    return hit.kind


def _expect_reason(ctx: _Ctx, index: int) -> str | None:
    """第 index 条 ExpectResult 对应的 Expect.reason。"""
    position = 0
    for expect in ctx.case.expect:
        fields = (expect.scoreboard, expect.regex, expect.sim_time_min, expect.uvm_info_min)
        position += sum(f is not None for f in fields)
        if index < position:
            return expect.reason
    return None


def _fmt_duration(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    whole = int(seconds)
    if whole != seconds:
        return f"{seconds:g}s"
    for unit, size in (("h", 3600), ("m", 60)):
        if whole and whole % size == 0:
            return f"{whole // size}{unit}"
    return f"{whole}s"
