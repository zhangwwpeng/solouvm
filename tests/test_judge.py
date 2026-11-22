"""Tests for solouvm.judge (Task 18, J-08/09/13/17/19/23, M-11/13/22, AI-20)."""

from __future__ import annotations

import json
import os
import shlex
import textwrap
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from solouvm import Case
from solouvm.collect import collect_cases
from solouvm.config import load_config
from solouvm.errors import RunActiveError
from solouvm.history import record_run_stats, run_dir
from solouvm.judge import (
    build_results_payload,
    compute_run_hints,
    judge_exit_code,
    judge_run,
    write_results,
)
from solouvm.model import Job, Plan, Result
from solouvm.providers import LocalTransport, Provider, SshTransport
from solouvm.providers.launcher_make import MakeLauncher
from solouvm.providers.sync_none import NoneSync
from tests.test_providers import _setup_ssh_stub

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 18)

_UVM_CLEAN = (
    "UVM_INFO a.sv(1) @ 0ns: uvm_test_top [RNTST] start\n"
    "[AXI_SCB] compared=150 passed=150\n"
    "UVM_INFO a.sv(9) @ 50000ns: reporter [TEST_DONE] done\n"
    "--- UVM Report Summary ---\n"
    "** Report counts by severity\n"
    "UVM_INFO :    3\n"
    "UVM_WARNING :    0\n"
    "UVM_ERROR :    0\n"
    "UVM_FATAL :    0\n"
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _facts_text(
    *,
    run_id: str,
    job_id: str,
    try_num: int = 1,
    kind: str = "job",
    state: str = "done",
    rc: int | None = 0,
    duration_s: float = 4.25,
    host: str = "simbox01",
    signal: str = "",
    timed_out: int = 0,
    hung: int = 0,
) -> str:
    lines = [
        "schema=1",
        "generator=solouvm 0.1.0",
        f"kind={kind}",
        f"run_id={run_id}",
        f"job_id={job_id}",
        f"try={try_num}",
        f"host={host}",
        "pid=1234",
        "timeout_impl=timeout",
        "started_at=2026-11-18T12:00:00+00:00",
        f"state={state}",
    ]
    if state != "running":
        lines.extend(
            [
                f"rc={0 if rc is None else rc}",
                f"signal={signal}",
                f"timed_out={timed_out}",
                f"hung={hung}",
                "finished_at=2026-11-18T12:00:05+00:00",
                f"duration_s={duration_s:.3f}",
            ]
        )
    return "\n".join(lines) + "\n"


def _setup_project(
    tmp_path: Path,
    *,
    conf_extra: str = "",
    case_py: str | None = None,
) -> tuple[Path, Plan]:
    root = tmp_path / "proj"
    _write(
        root / "solouvm.conf.py",
        f'CFG = {{"project": "demo_vip", "simulator": "vcs", "top": "tb_top"{conf_extra}}}\n',
    )
    if case_py is not None:
        _write(root / "case.py", case_py)
    else:
        _write(
            root / "case.py",
            textwrap.dedent(
                """
                from solouvm import Case, Expect

                CASES = [
                    Case(
                        name="burst",
                        expect=[Expect(scoreboard="AXI_SCB", min=100)],
                    ),
                    Case(
                        name="smoke",
                        expect=[Expect(scoreboard="AXI_SCB", min=100)],
                    ),
                ]
                """
            ),
        )
    plan = Plan(
        run_id="R1",
        created_at=T0,
        parent_run=None,
        provider="local",
        selection={},
        builds={"default": "b123456789ab"},
        jobs=[],
    )
    return root, plan


def _make_job(run_id: str, case: str, seed: int, build: str = "default") -> Job:
    jid = f"{case}__{seed}"
    return Job(
        job_id=jid,
        case=case,
        seed=seed,
        build=build,
        uvm_test=None,
        params={},
        opts=[],
        env={},
        timeout_s=60.0,
        hang_s=None,
        retry=0,
        expect_fail=False,
        tags=(),
        dir=f".solouvm/runs/{run_id}/{jid}",
    )


def _mark_build_ok(root: Path, mode: str = "default", key: str = "b123456789ab") -> None:
    _write(root / ".solouvm" / "build" / mode / key / ".ok", "")


def _local_provider(root: Path) -> Provider:
    return Provider(
        name="local",
        transport=LocalTransport(root=root),
        launcher=MakeLauncher(),
        sync=NoneSync(),
    )


def test_judge_retries_flaky_wave_and_non_numeric_tries(tmp_path: Path) -> None:
    root, plan = _setup_project(tmp_path)
    _mark_build_ok(root)
    j1 = _make_job("R1", "burst", 1)
    j2 = _make_job("R1", "smoke", 2)
    plan.jobs = [j1, j2]

    # j1: try1 fails, try2 passes -> flaky=True, status="pass"
    _write(
        root / j1.dir / "try1" / "job.facts",
        _facts_text(run_id="R1", job_id=j1.job_id, try_num=1, rc=1),
    )
    _write(root / j1.dir / "try1" / "sim.log", "Error-[NOA] bad\n")
    _write(
        root / j1.dir / "try2" / "job.facts",
        _facts_text(run_id="R1", job_id=j1.job_id, try_num=2, rc=0),
    )
    _write(root / j1.dir / "try2" / "sim.log", _UVM_CLEAN)

    # j2: try1 fails, try2 has .wave marker and passes (must NOT affect final status!),
    # plus manual tryr1 (non-numeric, must be completely ignored!)
    _write(
        root / j2.dir / "try1" / "job.facts", _facts_text(run_id="R1", job_id=j2.job_id, try_num=1)
    )
    _write(
        root / j2.dir / "try1" / "sim.log",
        "UVM_ERROR scb.sv(4) @ 10ns: uvm_test_top.scb [AXI_SCB] mismatch 0x1\n"
        "--- UVM Report Summary ---\n** Report counts by severity\nUVM_ERROR : 1\n",
    )
    _write(
        root / j2.dir / "try2" / "job.facts", _facts_text(run_id="R1", job_id=j2.job_id, try_num=2)
    )
    _write(root / j2.dir / "try2" / "sim.log", _UVM_CLEAN)
    _write(root / j2.dir / "try2" / ".wave", "")
    _write(
        root / j2.dir / "tryr1" / "job.facts", _facts_text(run_id="R1", job_id=j2.job_id, try_num=9)
    )
    _write(root / j2.dir / "tryr1" / "sim.log", _UVM_CLEAN)

    cfg = load_config(root)
    results = judge_run(config=cfg, plan=plan, provider=_local_provider(root), today=TODAY)
    assert [r.job_id for r in results] == ["burst__1", "smoke__2"]

    r1, r2 = results
    assert r1.status == "pass"
    assert r1.flaky is True
    assert [a["try"] for a in r1.attempts] == [1, 2]
    assert "TRY=r1" in r1.repro["make"]
    assert shlex.split(r1.repro["make"])[:3] == ["make", "-f", ".solouvm/runs/R1/run.mk"]
    assert "solouvm run" in r1.repro["solouvm"]

    assert r2.status == "fail"
    assert r2.flaky is False
    assert [a["try"] for a in r2.attempts] == [1, 2]
    assert r2.attempts[0]["wave"] is False
    assert r2.attempts[1]["wave"] is True
    assert judge_exit_code(results) == 1


def test_judge_run_active_partial_and_allow_missing(tmp_path: Path) -> None:
    root, plan = _setup_project(tmp_path)
    _mark_build_ok(root)
    j1 = _make_job("R1", "burst", 1)
    j2 = _make_job("R1", "smoke", 2)
    plan.jobs = [j1, j2]

    _write(
        root / j1.dir / "try1" / "job.facts",
        _facts_text(run_id="R1", job_id=j1.job_id, state="running"),
    )

    cfg = load_config(root)
    prov = _local_provider(root)

    with pytest.raises(RunActiveError) as exc_info:
        judge_run(config=cfg, plan=plan, provider=prov, partial=False, today=TODAY)
    assert exc_info.value.code == "RUN_ACTIVE"

    partial_res = judge_run(
        config=cfg, plan=plan, provider=prov, partial=True, allow_missing=True, today=TODAY
    )
    assert partial_res[0].status == "incomplete"
    assert partial_res[1].status == "skip"
    payload = build_results_payload(
        config=cfg,
        plan=plan,
        results=partial_res,
        cases=[],
        partial=True,
        judged_at=T0,
    )
    assert payload["partial"] is True

    strict_res = judge_run(
        config=cfg, plan=plan, provider=prov, partial=True, allow_missing=False, today=TODAY
    )
    assert strict_res[1].status == "incomplete"
    assert strict_res[1].reason == "not run yet: job.facts missing"


def test_judge_build_failure_and_corrupted_facts_isolation(tmp_path: Path) -> None:
    root, plan = _setup_project(tmp_path)
    j_ok = _make_job("R1", "burst", 1)
    j_bad_facts = _make_job("R1", "burst", 2)
    j_bad_build = _make_job("R1", "smoke", 3, build="gate")

    _write(
        root / "solouvm.conf.py",
        'CFG = {"project": "demo", "simulator": "vcs", "top": "tb", '
        '"builds": {"default": {}, "gate": {"defines": ["G"]}}}\n',
    )
    plan.builds = {"default": "b1", "gate": "b2"}
    plan.jobs = [j_ok, j_bad_facts, j_bad_build]

    _mark_build_ok(root, "default", "b1")
    _write(root / j_ok.dir / "try1" / "job.facts", _facts_text(run_id="R1", job_id=j_ok.job_id))
    _write(root / j_ok.dir / "try1" / "sim.log", _UVM_CLEAN)

    _write(
        root / j_bad_facts.dir / "try1" / "job.facts",
        _facts_text(run_id="R1", job_id=j_bad_facts.job_id).replace("schema=1", "schema=99"),
    )

    # gate build failed at line 2 of build.log
    _write(
        root / ".solouvm" / "build" / "gate" / "b2" / "job.facts",
        _facts_text(run_id="R1", job_id="build__gate__b2", kind="build", rc=2),
    )
    _write(
        root / ".solouvm" / "build" / "gate" / "b2" / "build.log",
        "compiling...\nError-[SV-SYNTAX] syntax error\n",
    )

    cfg = load_config(root)
    results = judge_run(config=cfg, plan=plan, provider=_local_provider(root), today=TODAY)
    assert results[0].status == "pass"
    assert results[1].status == "error"
    assert "schema 99" in results[1].reason
    assert results[2].status == "error"
    assert results[2].reason == "build failed: gate/b2, see build.log line 2"
    assert judge_exit_code(results) == 2

    # Build not run (no build job.facts, no .ok) also returns exit code 2
    r_not_run = Result(
        job_id="x__1",
        case="x",
        seed=1,
        status="error",
        judged_status="error",
        raw_status="error",
        category="environment",
        reason="build not run: gate/b2",
    )
    assert judge_exit_code([r_not_run]) == 2


def test_judge_weak_signals_triage_known_custom_and_hints(tmp_path: Path) -> None:
    case_py = textwrap.dedent(
        """
        from solouvm import Bug, Case, Expect, Triage, Waiver

        CASES = [
            Case(
                name="burst",
                expect=[Expect(scoreboard="AXI_SCB", min=10)],
                checks={"waivers": [Waiver(id="EXPIRED_W", reason="old", until="2020-01-01")]},
                notes=[
                    Bug(
                        id=1,
                        title="open bug",
                        status="open",
                        seed=10,
                        found="2026-11-10",
                        signature="sig_a",
                    ),
                    Bug(
                        id=2,
                        title="fixed bug",
                        status="fixed",
                        seed=10,
                        found="2026-11-01",
                        fixed="2026-11-12",
                    ),
                    Bug(
                        id=3,
                        title="verified bug",
                        status="verified",
                        seed=2,
                        found="2026-11-01",
                        fixed="2026-11-05",
                        verified="2026-11-06",
                    ),
                    Triage(
                        task="R1/1",
                        verdict="false_pass",
                        confidence="high",
                        summary="did not drive stimulus",
                        evidence=["log:1"],
                        judged_status={"burst__1": "pass"},
                        review={
                            "decision": "accepted",
                            "reason": None,
                            "author": "main",
                            "at": "2026-11-18T12:00",
                        },
                    ),
                    Triage(
                        task="R1/2",
                        verdict="env_issue",
                        confidence="high",
                        summary="bad disk",
                        evidence=["log:1"],
                        judged_status={"burst__2": "fail"},
                        review={
                            "decision": "accepted",
                            "reason": None,
                            "author": "main",
                            "at": "2026-11-18T12:00",
                        },
                    ),
                    Triage(
                        task="R1/3",
                        verdict="env_issue",
                        confidence="high",
                        summary="stale one",
                        evidence=["log:1"],
                        judged_status={"burst__3": "pass"},
                        review={
                            "decision": "accepted",
                            "reason": None,
                            "author": "main",
                            "at": "2026-11-18T12:00",
                        },
                    ),
                ],
            ),
            Case(
                name="noexp",
                notes=[
                    Bug(id=1, title="cross-case sig", status="open", found="2026-11-01"),
                ],
            ),
        ]
        """
    )
    conf_extra = ', "checks": {"waivers": [Waiver(id="UNUSED_W", reason="unused")]}'
    root, plan = _setup_project(tmp_path, case_py=case_py)
    _write(
        root / "solouvm.conf.py",
        "from solouvm import Waiver\n"
        f'CFG = {{"project": "demo_vip", "simulator": "vcs", "top": "tb_top"{conf_extra}}}\n',
    )
    _mark_build_ok(root)

    # Prior run R0 for median baseline + cross-run flaky history
    record_run_stats(root, "R0", {"noexp": {"sim_end_ns": [100000.0], "log_lines": [1000]}})
    _write(
        run_dir(root, "R0") / "results.json",
        json.dumps(
            {
                "schema": 1,
                "run_id": "R0",
                "judged_at": "2026-11-17T12:00:00+00:00",
                "partial": False,
                "summary": {
                    "total": 1,
                    "passed": 0,
                    "by_status": {},
                    "by_category": {},
                    "duration_s": 1.0,
                },
                "jobs": [{"job_id": "noexp__4", "case": "noexp", "seed": 4, "status": "fail"}],
                "signatures": [],
                "waivers": {"used": [], "unused": [], "expired": []},
                "hints": [],
            }
        ),
    )

    j1 = _make_job("R1", "burst", 1)  # pass -> triage false_pass accepted
    j1b = _make_job("R1", "burst", 10)  # clean pass -> triggers open/fixed bug hints
    j2 = _make_job("R1", "burst", 2)  # fail -> triage env_issue accepted -> error + regression hint
    j3 = _make_job("R1", "burst", 3)  # fail -> stale triage -> stays fail!
    j4 = _make_job("R1", "noexp", 4)  # pass with weak signals & cross-run flaky
    j5 = _make_job("R1", "noexp", 5)  # pass tightened by custom_judge -> fail
    plan.jobs = [j1, j1b, j2, j3, j4, j5]

    for j in (j1, j1b, j4, j5):
        _write(root / j.dir / "try1" / "job.facts", _facts_text(run_id="R1", job_id=j.job_id))
    _write(root / j1.dir / "try1" / "sim.log", _UVM_CLEAN)
    _write(root / j1b.dir / "try1" / "sim.log", _UVM_CLEAN)
    # j4: UVM log with 0ns, no uvm_test_top, short log lines, and no Case.expect
    _write(
        root / j4.dir / "try1" / "sim.log",
        "UVM_INFO a.sv(1) @ 0ns: reporter [TEST_DONE] done\n"
        "--- UVM Report Summary ---\n** Report counts by severity\n"
        "UVM_INFO : 1\nUVM_WARNING : 0\nUVM_ERROR : 0\nUVM_FATAL : 0\n",
    )
    _write(root / j5.dir / "try1" / "sim.log", _UVM_CLEAN)

    for j in (j2, j3):
        _write(root / j.dir / "try1" / "job.facts", _facts_text(run_id="R1", job_id=j.job_id))
        _write(
            root / j.dir / "try1" / "sim.log",
            "UVM_ERROR a.sv(2) @ 10ns: uvm_test_top [AXI_SCB] bad\n"
            "--- UVM Report Summary ---\n** Report counts by severity\nUVM_ERROR : 1\n",
        )

    def _custom_hook(result: Result, case: Case) -> tuple[str, str] | None:
        if result.job_id == "noexp__5":
            return ("fail", "custom check failed")
        if result.job_id == "burst__3":
            return ("pass", "must NOT loosen failure")
        return None

    cfg = load_config(root)
    cases = collect_cases(cfg)
    results = judge_run(
        config=cfg,
        plan=plan,
        provider=_local_provider(root),
        cases=cases,
        custom_judge=_custom_hook,
        today=TODAY,
    )

    r1, r1b, r2, r3, r4, r5 = results
    assert r1.judged_status == "pass"
    assert r1.status == "false_pass"
    assert r1.category == "design"
    assert r1.triage is not None and r1.triage["stale"] is False
    assert r1b.status == "pass"

    assert r2.judged_status == "fail"
    assert r2.status == "error"
    assert r2.category == "environment"
    assert "burst#1" in r2.known and "burst#2" in r2.known

    assert r3.judged_status == "fail"
    assert r3.status == "fail"  # stale triage ignored + custom hook couldn't loosen
    assert r3.triage is not None and r3.triage["stale"] is True

    assert r4.status == "pass"
    assert r4.flaky is True  # failed in R0, passed in R1
    assert any("0ns" in s for s in r4.weak_signals)
    assert any("uvm_test_top" in s for s in r4.weak_signals)
    assert any("10%" in s for s in r4.weak_signals)
    assert any("no 'expect'" in s for s in r4.weak_signals)

    assert r5.status == "fail"
    assert r5.reason == "custom check failed"

    hints = compute_run_hints(cfg, cases, results)
    assert any("burst#1 may be fixed" in h for h in hints)
    assert any("burst#2 can be marked verified" in h for h in hints)
    assert any("regression? burst#3 was verified" in h for h in hints)

    payload = write_results(
        run_dir(root, "R1"),
        results,
        run_id="R1",
        partial=False,
        judged_at=T0,
        config=cfg,
        cases=cases,
    )
    assert payload["waivers"]["unused"] == ["UNUSED_W"]
    assert payload["waivers"]["expired"] == ["EXPIRED_W"]
    assert payload["hints"] == hints


def test_remote_prefiltered_judge_parity_with_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir, calls_log, remote_root = _setup_ssh_stub(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    local_root, plan = _setup_project(
        tmp_path,
        case_py=textwrap.dedent(
            """
            from solouvm import Case, Expect, Waiver

            CASES = [
                Case(
                    name="burst",
                    expect=[
                        Expect(scoreboard="AXI_SCB", min=100),
                        Expect(sim_time_min="40us"),
                    ],
                    checks={"waivers": [Waiver(id="NOISE_W", reason="known")]},
                ),
                Case(name="smoke"),
            ]
            """
        ),
    )
    j1 = _make_job("R1", "burst", 1)
    j2 = _make_job("R1", "smoke", 2)
    plan.jobs = [j1, j2]

    noise = "\n".join(f"noise padding line {i}" for i in range(1, 51))
    log1 = (
        f"{noise}\n"
        "UVM_WARNING scb.sv(5) @ 5ns: uvm_test_top [NOISE_W] ignore me\n"
        "UVM_ERROR scb.sv(12) @ 100ns: uvm_test_top.scb [AXI_SCB] mismatch 0x2a\n"
        "[AXI_SCB] compared=120 passed=119\n"
        "UVM_INFO a.sv(9) @ 50000ns: reporter [TEST_DONE] done\n"
        "--- UVM Report Summary ---\n** Report counts by severity\n"
        "UVM_INFO : 2\nUVM_WARNING : 1\nUVM_ERROR : 1\nUVM_FATAL : 0\n"
    )
    log2 = f"{noise}\n{_UVM_CLEAN}"

    for target_root in (local_root, remote_root):
        _mark_build_ok(target_root)
        _write(
            target_root / j1.dir / "try1" / "job.facts", _facts_text(run_id="R1", job_id=j1.job_id)
        )
        _write(target_root / j1.dir / "try1" / "sim.log", log1)
        _write(
            target_root / j2.dir / "try1" / "job.facts", _facts_text(run_id="R1", job_id=j2.job_id)
        )
        _write(target_root / j2.dir / "try1" / "sim.log", log2)

    cfg = load_config(local_root)
    local_results = judge_run(
        config=cfg,
        plan=plan,
        provider=_local_provider(local_root),
        today=TODAY,
    )
    ssh_provider = Provider(
        name="ssh_box",
        transport=SshTransport(host="simbox", workdir=str(remote_root), root=local_root),
        launcher=MakeLauncher(),
        sync=NoneSync(),
    )
    remote_results = judge_run(
        config=cfg,
        plan=plan,
        provider=ssh_provider,
        today=TODAY,
    )

    assert remote_results == local_results
    assert local_results[0].first_error == {
        "line": 52,
        "text": "UVM_ERROR scb.sv(12) @ 100ns: uvm_test_top.scb [AXI_SCB] mismatch 0x2a",
    }
    assert "grep -n -F" in calls_log.read_text(encoding="utf-8")


def test_judge_cross_case_signature_known_expect_fail_and_pending_triage(tmp_path: Path) -> None:
    from solouvm.judge.signature import normalize_signature, signature_id

    err_line = "UVM_ERROR scb.sv(12) @ 100ns: uvm_test_top.scb [SHARED_ERR] mismatch 0x99"
    sig_id = signature_id(normalize_signature(err_line))
    case_py = textwrap.dedent(
        f"""
        from solouvm import Bug, Case, Triage

        CASES = [
            Case(
                name="origin",
                notes=[
                    Bug(
                        id=7,
                        title="shared signature bug",
                        status="open",
                        found="2026-11-10",
                        signature="{sig_id}",
                    ),
                ],
            ),
            Case(
                name="victim",
                notes=[
                    Triage(
                        task="R1/1",
                        verdict="env_issue",
                        confidence="medium",
                        summary="first pending",
                        evidence=["log:1"],
                        judged_status={{"victim__1": "fail"}},
                        review=None,
                    ),
                    Triage(
                        task="R1/2",
                        verdict="env_issue",
                        confidence="medium",
                        summary="rejected later",
                        evidence=["log:1"],
                        judged_status={{"victim__1": "fail"}},
                        review={{
                            "decision": "rejected",
                            "reason": "not env",
                            "author": "main",
                            "at": "2026-11-18T12:00",
                        }},
                    ),
                ],
            ),
            Case(name="neg_ok", expect_fail="SHARED_ERR"),
            Case(name="neg_xpass", expect_fail=True),
        ]
        """
    )
    root, plan = _setup_project(tmp_path, case_py=case_py)
    _mark_build_ok(root)
    j1 = _make_job("R1", "victim", 1)
    j2 = _make_job("R1", "neg_ok", 2)
    j3 = _make_job("R1", "neg_xpass", 3)
    plan.jobs = [j1, j2, j3]

    fail_log = (
        f"{err_line}\n--- UVM Report Summary ---\n** Report counts by severity\nUVM_ERROR : 1\n"
    )
    for j in (j1, j2):
        _write(root / j.dir / "try1" / "job.facts", _facts_text(run_id="R1", job_id=j.job_id))
        _write(root / j.dir / "try1" / "sim.log", fail_log)
    _write(root / j3.dir / "try1" / "job.facts", _facts_text(run_id="R1", job_id=j3.job_id))
    _write(root / j3.dir / "try1" / "sim.log", _UVM_CLEAN)

    cfg = load_config(root)
    results = judge_run(config=cfg, plan=plan, provider=_local_provider(root), today=TODAY)
    r_vic, r_xfail, r_xpass = results

    # Cross-case signature linkage + latest Triage (rejected -> status stays fail)
    assert r_vic.known == ["origin#7"]
    assert r_vic.triage is not None
    assert r_vic.triage["task"] == "R1/2"
    assert r_vic.triage["review"] == "rejected"
    assert r_vic.status == "fail"

    # expect_fail statuses
    assert r_xfail.raw_status == "fail"
    assert r_xfail.judged_status == "xfail"
    assert r_xfail.status == "xfail"
    assert r_xfail.category is None

    assert r_xpass.raw_status == "pass"
    assert r_xpass.judged_status == "xpass"
    assert r_xpass.status == "xpass"
    assert r_xpass.category == "design"
    assert judge_exit_code([r_xfail]) == 0
    assert judge_exit_code([r_xpass]) == 1


def test_judge_run_writes_medians_and_respects_weak_signals_disabled(tmp_path: Path) -> None:
    root, plan1 = _setup_project(
        tmp_path,
        conf_extra=', "triage": {"weak_signals": True}',
        case_py="from solouvm import Case\nCASES = [Case(name='smoke')]\n",
    )
    _mark_build_ok(root)
    j1 = _make_job("R1", "smoke", 1)
    plan1.jobs = [j1]
    long_log = (
        "\n".join(f"UVM_INFO a.sv(1) @ {i}ns: uvm_test_top [MSG] step" for i in range(1, 200))
        + "\nUVM_INFO a.sv(200) @ 100000ns: reporter [TEST_DONE] done\n"
        + "--- UVM Report Summary ---\n** Report counts by severity\n"
        + "UVM_INFO : 200\nUVM_WARNING : 0\nUVM_ERROR : 0\nUVM_FATAL : 0\n"
    )
    _write(root / j1.dir / "try1" / "job.facts", _facts_text(run_id="R1", job_id=j1.job_id))
    _write(root / j1.dir / "try1" / "sim.log", long_log)

    cfg = load_config(root)
    res1 = judge_run(config=cfg, plan=plan1, provider=_local_provider(root), today=TODAY)
    write_results(run_dir(root, "R1"), res1, run_id="R1", partial=False, judged_at=T0, config=cfg)

    # Run 2: short log & short sim time -> weak signals triggered against R1 median
    plan2 = Plan(
        run_id="R2",
        created_at=T0,
        parent_run=None,
        provider="local",
        selection={},
        builds={"default": "b123456789ab"},
        jobs=[_make_job("R2", "smoke", 1)],
    )
    _write(
        root / plan2.jobs[0].dir / "try1" / "job.facts",
        _facts_text(run_id="R2", job_id=plan2.jobs[0].job_id),
    )
    _write(
        root / plan2.jobs[0].dir / "try1" / "sim.log",
        "UVM_INFO a.sv(1) @ 1ns: uvm_test_top [RNTST] start\n"
        "UVM_INFO a.sv(2) @ 100ns: reporter [TEST_DONE] done\n"
        "--- UVM Report Summary ---\n** Report counts by severity\n"
        "UVM_INFO : 2\nUVM_WARNING : 0\nUVM_ERROR : 0\nUVM_FATAL : 0\n",
    )
    res2 = judge_run(config=cfg, plan=plan2, provider=_local_provider(root), today=TODAY)
    assert any("sim time" in s for s in res2[0].weak_signals)
    assert any("log lines" in s for s in res2[0].weak_signals)

    # With weak_signals=False, none reported
    cfg_off = load_config(root, cli={"triage": {"weak_signals": False}})
    res2_off = judge_run(config=cfg_off, plan=plan2, provider=_local_provider(root), today=TODAY)
    assert res2_off[0].weak_signals == []


def test_triage_verdict_is_scoped_to_run_id(tmp_path: Path) -> None:
    case_py = textwrap.dedent(
        """
        from solouvm import Case, Triage

        CASES = [
            Case(
                name="smoke",
                notes=[
                    Triage(
                        task="audit-a/1",
                        verdict="false_pass",
                        confidence="high",
                        summary="bad test in audit-a",
                        evidence=["log:1"],
                        judged_status={"smoke__1": "pass"},
                        review={
                            "decision": "accepted",
                            "reason": None,
                            "author": "main",
                            "at": "2026-11-18T12:00",
                        },
                    )
                ],
            )
        ]
        """
    )
    root, plan_a = _setup_project(tmp_path, case_py=case_py)
    plan_a.run_id = "audit-a"
    plan_a.jobs = [_make_job("audit-a", "smoke", 1)]
    _mark_build_ok(root)
    _write(
        root / plan_a.jobs[0].dir / "try1" / "job.facts",
        _facts_text(run_id="audit-a", job_id="smoke__1"),
    )
    _write(root / plan_a.jobs[0].dir / "try1" / "sim.log", _UVM_CLEAN)

    cfg = load_config(root)
    res_a = judge_run(config=cfg, plan=plan_a, provider=_local_provider(root), today=TODAY)
    assert res_a[0].status == "false_pass"
    assert judge_exit_code(res_a) == 1

    # Fresh run with same job_id must NOT inherit audit-a's false_pass conclusion
    plan_b = Plan(
        run_id="audit-b",
        created_at=T0,
        parent_run=None,
        provider="local",
        selection={},
        builds={"default": "b123456789ab"},
        jobs=[_make_job("audit-b", "smoke", 1)],
    )
    _write(
        root / plan_b.jobs[0].dir / "try1" / "job.facts",
        _facts_text(run_id="audit-b", job_id="smoke__1"),
    )
    _write(root / plan_b.jobs[0].dir / "try1" / "sim.log", _UVM_CLEAN)

    res_b = judge_run(config=cfg, plan=plan_b, provider=_local_provider(root), today=TODAY)
    assert res_b[0].status == "pass"
    assert res_b[0].triage is None
    assert judge_exit_code(res_b) == 0
