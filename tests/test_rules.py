"""任务 9：判定规则、优先级与 expect_fail（verdict.md §3、§6、§8）。"""

import io
from datetime import date, datetime, timezone

import pytest

from solouvm.backends import get_backend
from solouvm.judge.rules import Verdict, apply_expect_fail, decide
from solouvm.judge.scan import scan
from solouvm.judge.signature import normalize_signature, signature_id
from solouvm.model import Case, Checks, Expect, Facts, Waiver

T0 = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 17)

SUMMARY = (
    "--- UVM Report Summary ---\n"
    "** Report counts by severity\n"
    "UVM_INFO : {i}\nUVM_WARNING : {w}\nUVM_ERROR : {e}\nUVM_FATAL : {f}\n"
)
DONE = "UVM_INFO a.sv(1) @ 50000ns: reporter [TEST_DONE] 'run' phase is ready to proceed\n"


def _summary(i=1, w=0, e=0, f=0):
    return SUMMARY.format(i=i, w=w, e=e, f=f)


def _msg(n, sev="ERROR", msg_id="AXI_SCB", text="mismatch"):
    return f"UVM_{sev} verif/a.sv({n}) @ {n}0ns: uvm_test_top.env.scb [{msg_id}] {text} {n}\n"


CLEAN = "UVM_INFO a.sv(1) @ 0: uvm_test_top [RNTST] Running test\n" + DONE + _summary()


def _facts(**kw):
    values = dict(
        schema=1,
        generator="solouvm 0.1.0",
        kind="job",
        run_id="R1",
        job_id="smoke.1",
        try_=1,
        host="simbox01",
        pid=42,
        timeout_impl="setsid",
        started_at=T0,
        state="done",
        rc=0,
        finished_at=T0,
        duration_s=12.0,
    )
    values.update(kw)
    return Facts(**values)


def _decide(
    log=CLEAN,
    *,
    facts="default",
    checks=None,
    case=None,
    build_ok=True,
    build_facts=None,
    allow_missing=False,
    backend="vcs",
    **kw,
):
    checks = checks if checks is not None else Checks()
    case = case if case is not None else Case(name="smoke")
    result = scan(
        io.StringIO(log), checks=checks, backend=get_backend(backend), expects=case.expect
    )
    return decide(
        facts=_facts() if facts == "default" else facts,
        scan=result,
        build_ok=build_ok,
        build_facts=build_facts,
        build_key="default/3c9e1f2a",
        checks=checks,
        case=case,
        allow_missing=allow_missing,
        today=TODAY,
        **kw,
    )


# ---------------------------------------------------------------- 各条规则


def test_0a_build_failed():
    v = _decide(build_ok=False, build_facts=_facts(kind="build", rc=1), build_error_line=120)
    assert v.status == "error"
    assert v.reason == "build failed: default/3c9e1f2a, see build.log line 120"
    assert v.category == "environment"
    assert v.signature == "build"


def test_0a_build_not_run():
    v = _decide(build_ok=False, build_facts=None)
    assert v.status == "error"
    assert v.reason == "build not run: default/3c9e1f2a"


def test_0a_build_facts_without_end_is_not_run():
    v = _decide(build_ok=False, build_facts=_facts(kind="build", state="running", rc=None))
    assert "build not run" in v.reason


def test_0b_missing_facts():
    v = _decide(facts=None)
    assert v.status == "error"
    assert v.reason == "not run: job.facts missing"
    skip = _decide(facts=None, allow_missing=True)
    assert skip.status == "skip"
    assert skip.category is None
    assert skip.signature is None


def test_0c_stale_run_id():
    v = _decide(facts=_facts(run_id="OLD"), run_id="R1")
    assert v.status == "error"
    assert "stale facts from run OLD" in v.reason
    assert _decide(run_id="R1").status == "pass"


def test_0d_schema():
    v = _decide(facts=_facts(schema=2))
    assert v.status == "error"
    assert v.reason == "facts schema 2 not supported; regenerate"
    assert _decide(facts=_facts(generator="other 1")).status == "error"


def test_0d_facts_parse_error():
    v = _decide(facts=None, facts_error="facts schema 3 not supported; regenerate")
    assert v.status == "error"
    assert "schema 3" in v.reason


def test_1_incomplete():
    running = _decide(facts=_facts(state="running", rc=None, finished_at=None, duration_s=None))
    assert running.status == "incomplete"
    assert running.reason == "job did not finish (host simbox01)"
    assert running.category == "execution"
    interrupted = _decide(facts=_facts(state="interrupted", rc=130))
    assert interrupted.status == "incomplete"


def test_1_done_without_rc_is_incomplete():
    assert _decide(facts=_facts(rc=None)).status == "incomplete"


def test_2_license_failure():
    log = "Failed to obtain license for VCS\n"
    v = _decide(log, facts=_facts(rc=1))
    assert v.status == "error"
    assert v.category == "environment"
    assert v.reason.startswith("environment failure at line 1: ")


def test_3_killed():
    v = _decide(facts=_facts(rc=139, signal="SIGSEGV"))
    assert v.status == "killed"
    assert v.reason == "killed by SIGSEGV"
    assert v.category == "execution"


def test_4_timeout_and_hung():
    t = _decide(facts=_facts(rc=124, signal="SIGTERM", timed_out=True, duration_s=1800.0))
    assert t.status == "timeout"
    assert t.reason == "wall-clock timeout after 30m"
    case = Case(name="smoke", timeout="20m", hang="10m")
    t2 = _decide(facts=_facts(rc=124, timed_out=True), case=case)
    assert t2.reason == "wall-clock timeout after 20m"
    h = _decide(facts=_facts(rc=124, signal="SIGKILL", hung=True), case=case)
    assert h.status == "timeout"
    assert h.reason == "no log output for 10m"


def test_5_exit_code():
    v = _decide(facts=_facts(rc=3))
    assert v.status == "fail"
    assert "exit code 3" in v.reason
    assert v.category == "design"


def test_6_simulator_error():
    log = CLEAN + "Error-[NOA] Null object access\n"
    v = _decide(log)
    assert v.status == "fail"
    assert v.reason.startswith("simulator error at line ")
    assert "Error-[NOA]" in v.reason
    off = _decide(log, checks=Checks(sim_errors=False))
    assert off.status == "pass"


def test_6_simulator_error_waived_with_sim_severity():
    log = CLEAN + "Error-[NOA] Null object access\n"
    checks = Checks(waivers=(Waiver(regex="NOA", severity=("SIM",), reason="r"),))
    assert _decide(log, checks=checks).status == "pass"


def test_7_fail_regex():
    log = CLEAN + "TEST FAILED\n"
    v = _decide(log, checks=Checks(fail_regex=("TEST FAILED",)))
    assert v.status == "fail"
    assert v.reason == f"fail_regex 'TEST FAILED' at line {CLEAN.count(chr(10)) + 1}"


def test_7_fail_regex_cannot_be_waived():
    log = CLEAN + "TEST FAILED\n"
    checks = Checks(
        fail_regex=("TEST FAILED",),
        waivers=(Waiver(regex="TEST FAILED", severity=("ERROR", "SIM"), reason="r"),),
    )
    assert _decide(log, checks=checks).status == "fail"


def test_8_fatal():
    v = _decide(_msg(5, "FATAL") + _summary(f=1))
    assert v.status == "fail"
    assert v.reason == "UVM_FATAL=1 > 0"


def test_9_error():
    v = _decide(_msg(5) + _summary(e=1))
    assert v.status == "fail"
    assert v.reason == "UVM_ERROR=1 > 0"


def test_9_error_with_waived_count():
    log = _msg(1) + _msg(2) + _msg(3, msg_id="OTHER") + _summary(e=3)
    checks = Checks(waivers=(Waiver(id="AXI_SCB", reason="known"),))
    v = _decide(log, checks=checks)
    assert v.reason == "UVM_ERROR=1 > 0 (2 waived)"
    assert v.first_error[0] == 3


def test_10_warning_limit():
    log = DONE + _summary(w=31)
    v = _decide(log, checks=Checks(max_uvm_warning=30))
    assert v.status == "fail"
    assert v.reason == "UVM_WARNING=31 > 30"
    assert _decide(log, checks=Checks(max_uvm_warning=None)).status == "pass"


def test_11_missing_summary():
    log = DONE
    v = _decide(log)
    assert v.status == "fail"
    assert v.reason == "UVM log without Report Summary"
    rescued = _decide(log + "ALL TESTS PASSED\n", checks=Checks(pass_regex=("ALL TESTS PASSED",)))
    assert rescued.status == "pass"


def test_truncated_summary_does_not_pass():
    # 摘要被截断且仅含 UVM_INFO 时，正文里的 UVM_ERROR 仍须触发 fail
    err_log = (
        "UVM_ERROR @ 10ns: uvm_test_top [SCB] mismatch\n"
        "** Report counts by severity\n"
        "UVM_INFO : 1\n"
    )
    v_err = _decide(err_log)
    assert v_err.status == "fail"
    assert v_err.reason == "UVM_ERROR=1 > 0"

    # 无 ERROR 时，残缺摘要同样不能算通过，命中规则 11
    info_only = (
        "UVM_INFO @ 10ns: uvm_test_top [RNTST] start\n"
        "** Report counts by severity\n"
        "UVM_INFO : 1\n"
    )
    v_info = _decide(info_only)
    assert v_info.status == "fail"
    assert v_info.reason == "UVM log without Report Summary"


def test_12_non_uvm_marker():
    v = _decide("hello world\n")
    assert v.status == "fail"
    assert v.reason == "missing SOLOUVM_DONE marker"
    assert v.signature == "marker"
    ok = _decide("hello\nSOLOUVM_DONE status=pass\n")
    assert ok.status == "pass"


def test_13_expect_not_met():
    case = Case(name="smoke", expect=[Expect(scoreboard="AXI_SCB", min=100)])
    log = (
        "UVM_INFO a.sv(9) @ 9ns: uvm_test_top.env.scb [AXI_SCB] compared=0 mismatched=0\n"
        + DONE
        + _summary()
    )
    v = _decide(log, case=case)
    assert v.status == "fail"
    assert "expect not met" in v.reason
    assert "AXI_SCB compared=0 < 100" in v.reason
    assert v.first_error is None
    assert v.signature == "expect|TEST_DONE"


def test_13_expect_reason_included():
    case = Case(name="smoke", expect=[Expect(sim_time_min="1ms", reason="must run long")])
    v = _decide(CLEAN, case=case)
    assert v.status == "fail"
    assert v.reason.endswith("(must run long)")


def test_14_clean():
    v = _decide(CLEAN)
    assert v == Verdict(
        status="pass", reason="clean", category=None, first_error=None, signature=None
    )


# ---------------------------------------------------------------- 优先级


def test_env_failure_before_exit_code():
    v = _decide("Failed to obtain license\n", facts=_facts(rc=1))
    assert v.status == "error"


def test_fatal_before_error():
    log = _msg(1) + _msg(2, "FATAL") + _summary(e=5, f=1)
    v = _decide(log)
    assert v.reason == "UVM_FATAL=1 > 0"


def test_error_before_expect():
    case = Case(name="smoke", expect=[Expect(scoreboard="AXI_SCB", min=100)])
    v = _decide(_msg(1) + _summary(e=1), case=case)
    assert v.reason == "UVM_ERROR=1 > 0"


# ---------------------------------------------------------------- 首条错误与签名


def test_first_error_signature():
    log = _msg(7) + _msg(8) + _summary(e=2)
    v = _decide(log)
    assert v.first_error == (1, _msg(7).rstrip("\n"))
    assert v.signature == signature_id(normalize_signature(_msg(7).rstrip("\n")))


def test_waived_first_error_moves_to_next():
    log = _msg(1, msg_id="KNOWN") + _msg(2) + _summary(e=2)
    checks = Checks(waivers=(Waiver(id="KNOWN", reason="r"),))
    v = _decide(log, checks=checks)
    assert v.first_error == (2, _msg(2).rstrip("\n"))


def test_timeout_signature_uses_last_uvm_id():
    log = "UVM_INFO a.sv(1) @ 5ns: uvm_test_top.env.drv [AXI_MST_DRV] waiting\n"
    v = _decide(log, facts=_facts(rc=124, timed_out=True))
    assert v.first_error is None
    assert v.signature == "timeout|AXI_MST_DRV"
    bare = _decide("", facts=_facts(rc=124, timed_out=True))
    assert bare.signature == "timeout"


def test_internal_error_does_not_raise():
    v = decide(
        facts=_facts(),
        scan=None,
        build_ok=True,
        build_facts=None,
        build_key="k",
        checks=Checks(),
        case=Case(name="smoke"),
        allow_missing=False,
    )
    assert v.status == "error"
    assert "internal judge error" in v.reason


# ---------------------------------------------------------------- expect_fail


def _fail_verdict():
    return _decide(CLEAN + "TEST FAILED\n", checks=Checks(fail_regex=("TEST FAILED",)))


def test_expect_fail_pass_becomes_xpass():
    v = apply_expect_fail(_decide(CLEAN), Case(name="smoke", expect_fail=True))
    assert v.status == "xpass"
    assert v.category == "design"
    v2 = apply_expect_fail(_decide(CLEAN), Case(name="smoke", expect_fail="TEST FAILED"))
    assert v2.status == "xpass"


def test_expect_fail_fail_becomes_xfail():
    v = apply_expect_fail(_fail_verdict(), Case(name="smoke", expect_fail=True))
    assert v.status == "xfail"
    assert v.category is None


def test_expect_fail_regex_matches_first_error():
    v = apply_expect_fail(_fail_verdict(), Case(name="smoke", expect_fail="TEST FAILED"))
    assert v.status == "xfail"


def test_expect_fail_regex_mismatch():
    v = apply_expect_fail(_fail_verdict(), Case(name="smoke", expect_fail="OTHER"))
    assert v.status == "fail"
    assert "unexpected reason" in v.reason


def test_expect_fail_regex_without_first_error():
    case = Case(name="smoke", expect=[Expect(sim_time_min="1ms")], expect_fail="X")
    raw = _decide(CLEAN, case=case)
    assert raw.status == "fail"
    v = apply_expect_fail(raw, case)
    assert v.status == "fail"
    assert "unexpected reason" in v.reason


@pytest.mark.parametrize("expect_fail", [True, "TIME"])
def test_expect_fail_timeout_unchanged(expect_fail):
    raw = _decide(facts=_facts(rc=124, timed_out=True))
    v = apply_expect_fail(raw, Case(name="smoke", expect_fail=expect_fail))
    assert v.status == "timeout"


def test_expect_fail_false_keeps_result():
    raw = _fail_verdict()
    assert apply_expect_fail(raw, Case(name="smoke")) is raw


def test_0a_build_interrupted_and_partial_missing_are_incomplete():
    v_int = _decide(
        build_ok=False,
        build_facts=_facts(kind="build", state="interrupted", rc=130),
    )
    assert v_int.status == "incomplete"
    assert v_int.category == "execution"
    assert "build interrupted" in v_int.reason

    empty_scan = scan(io.StringIO(""), checks=Checks(), backend=get_backend("vcs"))
    v_partial_run = decide(
        facts=None,
        scan=empty_scan,
        build_ok=False,
        build_facts=_facts(kind="build", state="running", rc=None),
        build_key="default/3c9e1f2a",
        checks=Checks(),
        case=Case(name="smoke"),
        allow_missing=True,
        run_id="R1",
        partial=True,
    )
    assert v_partial_run.status == "incomplete"
    assert "build interrupted" in v_partial_run.reason

    v_partial_none = decide(
        facts=None,
        scan=empty_scan,
        build_ok=False,
        build_facts=None,
        build_key="default/3c9e1f2a",
        checks=Checks(),
        case=Case(name="smoke"),
        allow_missing=True,
        run_id="R1",
        partial=True,
    )
    assert v_partial_none.status == "incomplete"
    assert "build did not finish" in v_partial_none.reason

    v_partial_fail = decide(
        facts=None,
        scan=empty_scan,
        build_ok=False,
        build_facts=_facts(kind="build", state="done", rc=2),
        build_key="default/3c9e1f2a",
        checks=Checks(),
        case=Case(name="smoke"),
        allow_missing=True,
        run_id="R1",
        partial=True,
    )
    assert v_partial_fail.status == "error"
    assert "build failed" in v_partial_fail.reason
