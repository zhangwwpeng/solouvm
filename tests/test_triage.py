"""Tests for solouvm.triage (Task 22, AI-05..AI-11, AI-16, AI-19, AI-20)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import textwrap
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from solouvm.collect import collect_cases
from solouvm.config import load_config
from solouvm.errors import ConfigError, RecordError, RoleError
from solouvm.history import run_dir, update_latest_link
from solouvm.judge import judge_run, write_results
from solouvm.model import Bug, Job, Plan, Result, Triage
from solouvm.notes import bug_add
from solouvm.providers import LocalTransport, Provider
from solouvm.providers.launcher_make import MakeLauncher
from solouvm.providers.sync_none import NoneSync
from solouvm.report.json import read_results_json
from solouvm.triage import (
    plan_tasks,
    primary_case_for_task,
    read_tasks_json,
    render_packet,
    review_task,
    triage_status,
    write_result,
)
from solouvm.triage.packet import PACKET_SECTIONS

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 11, 18)

_VALID_DOC = textwrap.dedent(
    """\
    ## 现象
    Scoreboard observed address wrap mismatch on beat 2.

    ## 排查过程
    Inspected sim.log around line 50 and axi_scb.sv line 120.

    ## 证据
    log:50 and verif/envs/axi/axi_scb.sv:120 show addr_q=0.

    ## 结论
    RTL wrap logic fails to mask 4K boundary correctly.

    ## 建议
    Fix wrap mask calculation in axi_slv.sv and re-run seed 4417.
    """
)

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
    state: str = "done",
    rc: int = 0,
    duration_s: float = 2.5,
) -> str:
    lines = [
        "schema=1",
        "generator=solouvm 0.1.0",
        "kind=job",
        f"run_id={run_id}",
        f"job_id={job_id}",
        f"try={try_num}",
        "host=simbox01",
        "pid=1234",
        "timeout_impl=timeout",
        "started_at=2026-11-18T12:00:00+00:00",
        f"state={state}",
    ]
    if state != "running":
        lines.extend(
            [
                f"rc={rc}",
                "signal=",
                "timed_out=0",
                "hung=0",
                "finished_at=2026-11-18T12:00:03+00:00",
                f"duration_s={duration_s:.3f}",
            ]
        )
    return "\n".join(lines) + "\n"


def _make_job(run_id: str, case: str, seed: int) -> Job:
    jid = f"{case}__{seed}"
    return Job(
        job_id=jid,
        case=case,
        seed=seed,
        build="default",
        uvm_test=f"axi_{case}_test",
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


def _local_provider(root: Path) -> Provider:
    return Provider(
        name="local",
        transport=LocalTransport(root=root),
        launcher=MakeLauncher(),
        sync=NoneSync(),
    )


def _write_plan_and_results(
    root: Path,
    run_id: str,
    jobs: list[Job],
    results: list[Result],
    *,
    created_at: str = "2026-11-18T12:00:00+08:00",
    commit: str | None = None,
    dirty: bool = False,
) -> Path:
    rdir = run_dir(root, run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    plan_payload = {
        "schema": 1,
        "run_id": run_id,
        "created_at": created_at,
        "parent_run": None,
        "provider": "local",
        "selection": {"case": [], "tag": [], "exclude_tag": [], "build": None, "stage": ["wip"]},
        "builds": {"default": "b123"},
        "jobs": [
            {
                "job_id": j.job_id,
                "case": j.case,
                "seed": j.seed,
                "build": j.build,
                "uvm_test": j.uvm_test,
                "params": {},
                "opts": [],
                "env": {},
                "timeout_s": j.timeout_s,
                "hang_s": j.hang_s,
                "retry": j.retry,
                "expect_fail": j.expect_fail,
                "tags": list(j.tags),
                "dir": j.dir,
            }
            for j in jobs
        ],
    }
    _write(rdir / "plan.json", json.dumps(plan_payload, indent=2) + "\n")
    if commit is not None:
        _write(
            rdir / "env.json",
            json.dumps(
                {
                    "schema": 1,
                    "run_id": run_id,
                    "git": {"commit": commit, "branch": "main", "dirty": dirty, "diff": None},
                },
                indent=2,
            )
            + "\n",
        )
    write_results(rdir, results, run_id=run_id, partial=False, judged_at=T0)
    return rdir


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _res(
    *,
    job_id: str,
    case: str,
    seed: int = 1,
    status: str = "fail",
    judged_status: str | None = None,
    reason: str = "UVM_ERROR=1 > 0",
    sig_id: str | None = None,
    sig_text: str | None = None,
    weak_signals: list[str] | None = None,
) -> Result:
    jstatus = status if judged_status is None else judged_status
    sig = {"id": sig_id, "text": sig_text or sig_id} if sig_id else None
    return Result(
        job_id=job_id,
        case=case,
        seed=seed,
        status=status,
        judged_status=jstatus,
        raw_status=jstatus,
        category=None if jstatus in ("pass", "xfail", "skip") else "design",
        reason=reason,
        signature=sig,
        weak_signals=weak_signals or [],
    )


def _setup_basic_project(
    tmp_path: Path, *, case_py: str | None = None, conf_extra: str = ""
) -> Path:
    root = tmp_path / "proj"
    _write(
        root / "solouvm.conf.py",
        f'CFG = {{"project": "demo", "simulator": "vcs", "top": "tb_top"{conf_extra}}}\n',
    )
    _write(
        root / "case.py",
        case_py
        or textwrap.dedent(
            """\
            from solouvm import Case, Expect

            CASES = [
                Case(
                    name="burst",
                    stage="ready",
                    intent="Verify burst transfers across 4K boundaries.",
                    method="Run random WRAP bursts and check AXI_SCB.",
                    spec_refs=["AXI4 A3.4.1"],
                    expect=[Expect(scoreboard="AXI_SCB", min=10)],
                ),
                Case(
                    name="burst_long",
                    stage="ready",
                    intent="Verify long burst sequences.",
                    method="Run long INCR/WRAP sequences.",
                    spec_refs=["AXI4 A3.4.2"],
                    expect=[Expect(scoreboard="AXI_SCB", min=10)],
                ),
                Case(name="wip_case", stage="wip", intent="WIP check"),
                Case(
                    name="ready_clean",
                    stage="ready",
                    intent="Ready test",
                    method="Check SCB",
                    spec_refs=["S1"],
                    expect=[Expect(scoreboard="AXI_SCB", min=1)],
                ),
            ]
            """
        ),
    )
    return root


# ------------------------------------------------------------------ plan_tasks tests


def test_plan_grouping_filtering_weak_signals_and_idempotency(tmp_path: Path) -> None:
    root = _setup_basic_project(tmp_path)
    cfg = load_config(root / "solouvm.conf.py")

    results = [
        # Two cases share signature sig_shared -> combined into 1 fail task, job count DESC
        _res(job_id="burst__1", case="burst", seed=1, status="fail", sig_id="sig_shared"),
        _res(job_id="burst__2", case="burst", seed=2, status="fail", sig_id="sig_shared"),
        _res(job_id="burst_long__3", case="burst_long", seed=3, status="fail", sig_id="sig_shared"),
        # Distinct signature -> separate fail task
        _res(
            job_id="burst_long__4",
            case="burst_long",
            seed=4,
            status="timeout",
            reason="wall-clock timeout",
            sig_id="sig_timeout",
        ),
        # error / incomplete / xfail / skip -> NOT triaged
        _res(job_id="burst__5", case="burst", seed=5, status="error", reason="license error"),
        _res(job_id="burst__6", case="burst", seed=6, status="incomplete", reason="did not finish"),
        _res(job_id="burst__7", case="burst", seed=7, status="xfail", reason="expected error"),
        _res(job_id="burst__8", case="burst", seed=8, status="skip", reason="missing"),
        # Pass with weak signal on ready case -> MUST be triaged as kind="pass"
        _res(
            job_id="ready_clean__9",
            case="ready_clean",
            seed=9,
            status="pass",
            reason="clean",
            weak_signals=["sim time 0ns"],
        ),
        # Clean pass on wip case -> triaged under default pass_policy="changed"
        _res(job_id="wip_case__10", case="wip_case", seed=10, status="pass", reason="clean"),
    ]
    run_id = "20261118-120000-r1"
    _write_plan_and_results(root, run_id, [], results)

    tasks = plan_tasks(config=cfg, run_id=run_id)
    assert len(tasks) == 4
    assert [t["id"] for t in tasks] == [
        f"{run_id}/1",
        f"{run_id}/2",
        f"{run_id}/3",
        f"{run_id}/4",
    ]

    t_shared = tasks[0]
    assert t_shared["kind"] == "fail"
    assert t_shared["signature"] == "sig_shared"
    assert t_shared["cases"] == ["burst", "burst_long"]
    assert primary_case_for_task(t_shared) == "burst"
    assert len(t_shared["jobs"]) == 3

    t_timeout = tasks[1]
    assert t_timeout["kind"] == "fail"
    assert t_timeout["signature"] == "sig_timeout"
    assert t_timeout["cases"] == ["burst_long"]

    t_weak = tasks[2]
    assert t_weak["kind"] == "pass"
    assert t_weak["cases"] == ["ready_clean"]
    assert "weak signal" in str(t_weak["reason"])

    t_wip = tasks[3]
    assert t_wip["kind"] == "pass"
    assert t_wip["cases"] == ["wip_case"]
    assert t_wip["reason"] == "stage=wip"

    # Write a result on task 1, then re-run plan_tasks -> no duplicate tasks, result preserved!
    write_result(
        config=cfg,
        run_id=run_id,
        task_id=f"{run_id}/1",
        verdict="design_bug",
        confidence="high",
        summary="wrap bug",
        evidence=["log:10"],
        doc_text=_VALID_DOC,
        author="triage:a1",
    )
    tasks_again = plan_tasks(config=cfg, run_id=run_id)
    assert len(tasks_again) == 4
    assert tasks_again[0]["id"] == f"{run_id}/1"
    assert tasks_again[0]["result"]["verdict"] == "design_bug"

    # Verify tasks.json format (formats.md §10)
    raw_tasks = read_tasks_json(run_dir(root, run_id) / "triage" / "tasks.json")
    assert raw_tasks["schema"] == 1
    assert raw_tasks["run_id"] == run_id
    assert raw_tasks["policy"] == {"pass_policy": "changed", "group_by": "signature"}


def test_plan_pass_policies_and_group_by_modes(tmp_path: Path) -> None:
    root = _setup_basic_project(tmp_path)
    cfg = load_config(root / "solouvm.conf.py")
    results = [
        _res(job_id="burst__1", case="burst", seed=1, status="fail", sig_id="sig_shared"),
        _res(job_id="burst_long__2", case="burst_long", seed=2, status="fail", sig_id="sig_shared"),
        _res(job_id="wip_case__3", case="wip_case", seed=3, status="pass", reason="clean"),
        _res(job_id="ready_clean__4", case="ready_clean", seed=4, status="pass", reason="clean"),
        _res(
            job_id="ready_clean__5",
            case="ready_clean",
            seed=5,
            status="pass",
            reason="clean",
            weak_signals=["case has no 'expect'"],
        ),
    ]

    # pass_policy="none": clean passes (even wip) not triaged; weak signal pass still triaged
    r_none = "R_NONE"
    tasks_none = plan_tasks(config=cfg, run_id=r_none, results=results, pass_policy="none")
    assert [(t["kind"], t["cases"]) for t in tasks_none] == [
        ("fail", ["burst", "burst_long"]),
        ("pass", ["ready_clean"]),
    ]

    # pass_policy="all": all passes triaged
    r_all = "R_ALL"
    tasks_all = plan_tasks(config=cfg, run_id=r_all, results=results, pass_policy="all")
    assert len(tasks_all) == 3
    assert sum(1 for t in tasks_all if t["kind"] == "pass") == 2

    # group_by="case": same signature on different cases stays split
    r_case = "R_CASE"
    tasks_case = plan_tasks(
        config=cfg, run_id=r_case, results=results[:2], pass_policy="none", group_by="case"
    )
    assert len(tasks_case) == 2
    assert [t["cases"] for t in tasks_case] == [["burst"], ["burst_long"]]

    # group_by="job": every job gets its own task
    r_job = "R_JOB"
    tasks_job = plan_tasks(
        config=cfg,
        run_id=r_job,
        results=[
            _res(job_id="burst__1", case="burst", seed=1, status="fail", sig_id="sig_shared"),
            _res(job_id="burst__2", case="burst", seed=2, status="fail", sig_id="sig_shared"),
        ],
        pass_policy="none",
        group_by="job",
    )
    assert len(tasks_job) == 2
    assert [t["jobs"][0]["job_id"] for t in tasks_job] == ["burst__1", "burst__2"]

    with pytest.raises(ConfigError) as exc_pp:
        plan_tasks(config=cfg, run_id=r_none, results=results, pass_policy="bogus")
    assert exc_pp.value.code == "CFG_INVALID"

    with pytest.raises(ConfigError) as exc_gb:
        plan_tasks(config=cfg, run_id=r_none, results=results, group_by="bogus")
    assert exc_gb.value.code == "CFG_INVALID"


def test_plan_changed_policy_detects_case_and_verif_tests_edits(tmp_path: Path) -> None:
    root = _setup_basic_project(tmp_path)
    test_sv = _write(root / "verif" / "tests" / "axi" / "axi_ready_clean_test.sv", "// v1\n")
    other_sv = _write(root / "verif" / "tests" / "axi" / "axi_burst_test.sv", "// v1\n")

    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    c0 = _git(root, "rev-parse", "HEAD")

    cfg = load_config(root / "solouvm.conf.py")
    prev_results = [
        _res(job_id="ready_clean__1", case="ready_clean", status="pass", reason="clean"),
        _res(job_id="burst__1", case="burst", status="pass", reason="clean"),
    ]
    _write_plan_and_results(
        root,
        "R0",
        [_make_job("R0", "ready_clean", 1), _make_job("R0", "burst", 1)],
        prev_results,
        created_at="2026-11-17T10:00:00+08:00",
        commit=c0,
    )

    # Without edits, ready cases are NOT triaged in R1 under pass_policy="changed"
    _write_plan_and_results(
        root,
        "R1",
        [_make_job("R1", "ready_clean", 1), _make_job("R1", "burst", 1)],
        prev_results,
        created_at="2026-11-18T10:00:00+08:00",
        commit=c0,
    )
    assert plan_tasks(config=cfg, run_id="R1", pass_policy="changed") == []

    # Edit verif/tests/axi/axi_ready_clean_test.sv -> only ready_clean becomes changed!
    _write(test_sv, "// v2 changed test\n")
    assert other_sv.is_file()
    tasks_after_sv = plan_tasks(config=cfg, run_id="R1", pass_policy="changed")
    assert len(tasks_after_sv) == 1
    assert tasks_after_sv[0]["cases"] == ["ready_clean"]

    # Non-git mtime test: reset to a non-git temp dir and touch case.py after R0_ng/plan.json
    ng_root = _setup_basic_project(tmp_path / "nongit")
    ng_cfg = load_config(ng_root / "solouvm.conf.py")
    _write_plan_and_results(
        ng_root,
        "R0_ng",
        [_make_job("R0_ng", "ready_clean", 1)],
        [_res(job_id="ready_clean__1", case="ready_clean", status="pass", reason="clean")],
    )
    _write_plan_and_results(
        ng_root,
        "R1_ng",
        [_make_job("R1_ng", "ready_clean", 1)],
        [_res(job_id="ready_clean__1", case="ready_clean", status="pass", reason="clean")],
    )
    # Force case.py mtime older than R0_ng/plan.json -> unchanged
    r0_plan_ns = (run_dir(ng_root, "R0_ng") / "plan.json").stat().st_mtime_ns
    os.utime(ng_root / "case.py", ns=(r0_plan_ns - 5_000_000_000, r0_plan_ns - 5_000_000_000))
    assert plan_tasks(config=ng_cfg, run_id="R1_ng", pass_policy="changed") == []

    # Force case.py mtime newer than R0_ng/plan.json -> changed!
    os.utime(ng_root / "case.py", ns=(r0_plan_ns + 5_000_000_000, r0_plan_ns + 5_000_000_000))
    ng_tasks = plan_tasks(config=ng_cfg, run_id="R1_ng", pass_policy="changed")
    assert [t["cases"] for t in ng_tasks] == [["ready_clean"]]


# ------------------------------------------------------------------ render_packet tests


def test_packet_seven_sections_multi_case_order_and_truncation(tmp_path: Path) -> None:
    case_py = textwrap.dedent(
        """\
        from solouvm import Bug, Case, Expect

        CASES = [
            Case(
                name="minor_case",
                stage="ready",
                intent="MINOR_INTENT_MUST_SURVIVE",
                method="MINOR_METHOD_MUST_SURVIVE",
                spec_refs=["SPEC-2"],
                expect=[Expect(scoreboard="AXI_SCB", min=10)],
                doc="Minor case documentation",
            ),
            Case(
                name="major_case",
                stage="ready",
                intent="MAJOR_INTENT_MUST_SURVIVE",
                method="MAJOR_METHOD_MUST_SURVIVE",
                spec_refs=["SPEC-1"],
                expect=[Expect(scoreboard="AXI_SCB", min=100)],
                doc="Major case documentation",
                notes=[Bug(id=1, title="prior bug", status="open", found="2026-11-10")],
            ),
        ]
        """
    )
    root = _setup_basic_project(
        tmp_path,
        case_py=case_py,
        conf_extra=', "system_doc": ["doc/verif/system.md"]',
    )
    sys_doc_body = "# System Guide\n\n## Overview\n" + ("SYS_DOC_PADDING_LINE\n" * 400)
    _write(root / "doc" / "verif" / "system.md", sys_doc_body)

    sv_lines = [f"// line {i}\n" for i in range(1, 30)]
    sv_lines[11] = '  `uvm_error("AXI_SCB", "wrap mismatch")\n'
    _write(root / "verif" / "envs" / "axi" / "axi_scb.sv", "".join(sv_lines))

    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    c_pass = _git(root, "rev-parse", "HEAD")

    _write(root / "rtl_note.txt", "changed rtl\n")
    _git(root, "add", "rtl_note.txt")
    _git(root, "commit", "-m", "fix rtl address mask")

    # Previous passing run for major_case
    _write_plan_and_results(
        root,
        "R_PASS",
        [_make_job("R_PASS", "major_case", 1)],
        [_res(job_id="major_case__1", case="major_case", status="pass", reason="clean")],
        created_at="2026-11-17T10:00:00+08:00",
        commit=c_pass,
    )

    run_id = "R_CUR"
    jobs = [
        _make_job(run_id, "minor_case", 1),
        _make_job(run_id, "major_case", 2),
        _make_job(run_id, "major_case", 3),
    ]
    log_text = "\n".join(
        f"UVM_INFO a.sv(1) @ {i}ns: uvm_test_top [MSG] step {i}" for i in range(1, 50)
    )
    err_text = (
        "UVM_ERROR verif/envs/axi/axi_scb.sv(12) @ 100ns: uvm_test_top.scb [AXI_SCB] mismatch"
    )
    full_log = f"{log_text}\n{err_text}\nUVM_INFO a.sv(2) @ 120ns: reporter [DONE] end\n"
    for j in jobs:
        _write(root / j.dir / "try1" / "sim.log", full_log)
    # Mark wave attempt on major_case__2 try2
    _write(root / jobs[1].dir / "try2" / "sim.log", full_log)
    _write(root / jobs[1].dir / "try2" / ".wave", "")

    results = [
        Result(
            job_id="minor_case__1",
            case="minor_case",
            seed=1,
            status="fail",
            judged_status="fail",
            raw_status="fail",
            category="design",
            reason="UVM_ERROR=1 > 0",
            first_error={"line": 50, "text": err_text},
            signature={"id": "sig_wrap", "text": "UVM_ERROR [AXI_SCB] mismatch"},
            expect=[{"rule": "scoreboard AXI_SCB min 10", "ok": False, "value": 0}],
            attempts=[
                {
                    "try": 1,
                    "status": "fail",
                    "log": f".solouvm/runs/{run_id}/minor_case__1/try1/sim.log",
                    "wave": False,
                }
            ],
            repro={
                "make": "make run/minor_case__1",
                "solouvm": "solouvm run --case minor_case --seed 1 --wave",
            },
        ),
        Result(
            job_id="major_case__2",
            case="major_case",
            seed=2,
            status="fail",
            judged_status="fail",
            raw_status="fail",
            category="design",
            reason="UVM_ERROR=1 > 0",
            first_error={"line": 50, "text": err_text},
            signature={"id": "sig_wrap", "text": "UVM_ERROR [AXI_SCB] mismatch"},
            expect=[{"rule": "scoreboard AXI_SCB min 100", "ok": False, "value": 0}],
            attempts=[
                {
                    "try": 1,
                    "status": "fail",
                    "log": f".solouvm/runs/{run_id}/major_case__2/try1/sim.log",
                    "wave": False,
                },
                {
                    "try": 2,
                    "status": "fail",
                    "log": f".solouvm/runs/{run_id}/major_case__2/try2/sim.log",
                    "wave": True,
                },
            ],
            repro={
                "make": "make run/major_case__2",
                "solouvm": "solouvm run --case major_case --seed 2 --wave",
            },
        ),
        Result(
            job_id="major_case__3",
            case="major_case",
            seed=3,
            status="fail",
            judged_status="fail",
            raw_status="fail",
            category="design",
            reason="UVM_ERROR=1 > 0",
            first_error={"line": 50, "text": err_text},
            signature={"id": "sig_wrap", "text": "UVM_ERROR [AXI_SCB] mismatch"},
            attempts=[
                {
                    "try": 1,
                    "status": "fail",
                    "log": f".solouvm/runs/{run_id}/major_case__3/try1/sim.log",
                    "wave": False,
                }
            ],
            repro={
                "make": "make run/major_case__3",
                "solouvm": "solouvm run --case major_case --seed 3 --wave",
            },
        ),
    ]
    _write_plan_and_results(root, run_id, jobs, results, created_at="2026-11-18T12:00:00+08:00")
    cfg = load_config(root / "solouvm.conf.py")
    plan_tasks(config=cfg, run_id=run_id)

    # Full packet (limit=0)
    pkt_full = render_packet(
        config=cfg,
        run_id=run_id,
        task_id=f"{run_id}/1",
        limit=0,
        provider=_local_provider(root),
    )
    for sec in PACKET_SECTIONS:
        assert sec in pkt_full
    # major_case (2 failures) ordered BEFORE minor_case (1 failure)
    assert pkt_full.index("### Case `major_case`") < pkt_full.index("### Case `minor_case`")
    assert "SYS_DOC_PADDING_LINE" in pkt_full
    assert '`uvm_error("AXI_SCB", "wrap mismatch")' in pkt_full
    assert "fix rtl address mask" in pkt_full
    assert "Wave attempt: available" in pkt_full
    assert "solouvm wave major_case__2" in pkt_full

    # Tight limit truncates system_doc FIRST while preserving Section 1 & Section 3 & source snippet
    pkt_limited = render_packet(
        config=cfg,
        run_id=run_id,
        task_id=f"{run_id}/1",
        limit=6500,
        provider=_local_provider(root),
    )
    for sec in PACKET_SECTIONS:
        assert sec in pkt_limited
    assert "truncated" in pkt_limited
    assert "doc/verif/system.md" in pkt_limited
    assert "MAJOR_INTENT_MUST_SURVIVE" in pkt_limited
    assert "MAJOR_METHOD_MUST_SURVIVE" in pkt_limited
    assert "MINOR_INTENT_MUST_SURVIVE" in pkt_limited
    assert "MINOR_METHOD_MUST_SURVIVE" in pkt_limited
    assert "scoreboard `AXI_SCB` min=100" in pkt_limited
    assert '`uvm_error("AXI_SCB", "wrap mismatch")' in pkt_limited

    # Very tight limit also truncates log context with solouvm log ref, never truncating Section 1/3
    pkt_tiny = render_packet(config=cfg, run_id=run_id, task_id=f"{run_id}/1", limit=1000)
    assert "solouvm log" in pkt_tiny
    assert "MAJOR_INTENT_MUST_SURVIVE" in pkt_tiny
    assert "### Questions to Answer" in pkt_tiny

    with pytest.raises(ConfigError) as exc_task:
        render_packet(config=cfg, run_id=run_id, task_id=f"{run_id}/999")
    assert exc_task.value.code == "TASK_NOT_FOUND"

    with pytest.raises(ConfigError) as exc_lim:
        render_packet(config=cfg, run_id=run_id, task_id=f"{run_id}/1", limit=-1)
    assert exc_lim.value.code == "CFG_INVALID"


# ------------------------------------------------------------------ write_result tests


def test_write_result_validation_overwrite_multi_case_and_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _setup_basic_project(tmp_path)
    cfg = load_config(root / "solouvm.conf.py")
    run_id = "20261118-130000-w1"
    results = [
        _res(job_id="burst__1", case="burst", seed=1, status="fail", sig_id="sig_wrap"),
        _res(job_id="burst_long__2", case="burst_long", seed=2, status="fail", sig_id="sig_wrap"),
        _res(job_id="wip_case__3", case="wip_case", seed=3, status="pass", reason="clean"),
    ]
    _write_plan_and_results(root, run_id, [], results)
    plan_tasks(config=cfg, run_id=run_id)

    # Register a real bug first so --bug validation succeeds
    bug = bug_add(
        config=cfg,
        case_name="burst",
        title="4K wrap bug",
        seed=1,
        run_id=run_id,
        role="triage",
        author="triage:agent-1",
    )
    bug_ref = f"burst#{bug.id}"

    # Invalid doc (missing ## 结论) -> TRIAGE_INVALID
    bad_doc = _VALID_DOC.replace(
        "## 结论\nRTL wrap logic fails to mask 4K boundary correctly.\n", ""
    )
    with pytest.raises(ConfigError) as exc_doc:
        write_result(
            config=cfg,
            run_id=run_id,
            task_id=f"{run_id}/1",
            verdict="design_bug",
            confidence="high",
            summary="wrap bug",
            evidence=["log:50"],
            bugs=[bug_ref],
            doc_text=bad_doc,
        )
    assert exc_doc.value.code == "TRIAGE_INVALID"

    # Empty section body -> TRIAGE_INVALID
    empty_sec_doc = _VALID_DOC.replace("RTL wrap logic fails to mask 4K boundary correctly.", "   ")
    with pytest.raises(ConfigError) as exc_empty_sec:
        write_result(
            config=cfg,
            run_id=run_id,
            task_id=f"{run_id}/1",
            verdict="design_bug",
            confidence="high",
            summary="wrap bug",
            evidence=["log:50"],
            bugs=[bug_ref],
            doc_text=empty_sec_doc,
        )
    assert exc_empty_sec.value.code == "TRIAGE_INVALID"

    # Empty or invalid evidence -> TRIAGE_INVALID
    for bad_ev in ([], ["812"]):
        with pytest.raises(ConfigError) as exc_ev:
            write_result(
                config=cfg,
                run_id=run_id,
                task_id=f"{run_id}/1",
                verdict="design_bug",
                confidence="high",
                summary="wrap bug",
                evidence=bad_ev,
                doc_text=_VALID_DOC,
            )
        assert exc_ev.value.code == "TRIAGE_INVALID"

    # Summary > 200 chars -> TRIAGE_INVALID
    with pytest.raises(ConfigError) as exc_sum:
        write_result(
            config=cfg,
            run_id=run_id,
            task_id=f"{run_id}/1",
            verdict="design_bug",
            confidence="high",
            summary="x" * 201,
            evidence=["log:50"],
            doc_text=_VALID_DOC,
        )
    assert exc_sum.value.code == "TRIAGE_INVALID"

    # Incompatible verdicts: fail+real_pass -> TRIAGE_INVALID; pass+design_bug -> TRIAGE_INVALID
    with pytest.raises(ConfigError) as exc_compat_fail:
        write_result(
            config=cfg,
            run_id=run_id,
            task_id=f"{run_id}/1",
            verdict="real_pass",
            confidence="high",
            summary="invalid",
            evidence=["log:50"],
            doc_text=_VALID_DOC,
        )
    assert exc_compat_fail.value.code == "TRIAGE_INVALID"

    with pytest.raises(ConfigError) as exc_compat_pass:
        write_result(
            config=cfg,
            run_id=run_id,
            task_id=f"{run_id}/2",
            verdict="design_bug",
            confidence="high",
            summary="invalid",
            evidence=["log:50"],
            doc_text=_VALID_DOC,
        )
    assert exc_compat_pass.value.code == "TRIAGE_INVALID"

    # Missing bug ref -> RECORD_INVALID
    with pytest.raises(RecordError) as exc_bug:
        write_result(
            config=cfg,
            run_id=run_id,
            task_id=f"{run_id}/1",
            verdict="design_bug",
            confidence="high",
            summary="missing bug",
            evidence=["log:50"],
            bugs=["burst#999"],
            doc_text=_VALID_DOC,
        )
    assert exc_bug.value.code == "RECORD_INVALID"

    # Unknown task -> TASK_NOT_FOUND
    with pytest.raises(ConfigError) as exc_unk:
        write_result(
            config=cfg,
            run_id=run_id,
            task_id=f"{run_id}/999",
            verdict="design_bug",
            confidence="high",
            summary="unknown",
            evidence=["log:50"],
            doc_text=_VALID_DOC,
        )
    assert exc_unk.value.code == "TASK_NOT_FOUND"

    # Valid write from stdin ("-")
    monkeypatch.setattr("sys.stdin", io.StringIO(_VALID_DOC))
    written = write_result(
        config=cfg,
        run_id=run_id,
        task_id=f"{run_id}/1",
        verdict="tb_bug",
        confidence="medium",
        summary="initial guess",
        evidence=["log:50"],
        bugs=[bug_ref],
        doc_text="-",
        author="triage:agent-1",
    )
    assert written["result"]["verdict"] == "tb_bug"
    doc_file = root / "docs" / "debug" / run_id / "1.md"
    assert doc_file.is_file()
    doc_content = doc_file.read_text(encoding="utf-8")
    assert f"# Triage: {run_id}/1" in doc_content
    assert "triage:agent-1" in doc_content
    assert "## 现象" in doc_content

    # Overwrite unreviewed result -> replaces existing Triage records without duplicating
    overwritten = write_result(
        config=cfg,
        run_id=run_id,
        task_id=f"{run_id}/1",
        verdict="design_bug",
        confidence="high",
        summary="confirmed wrap bug",
        evidence=["log:50", "rtl/axi_slv.sv:120", "wave:1234ns tb_top.dut.addr_q"],
        bugs=[bug_ref],
        doc_text=_VALID_DOC,
        author="triage:agent-1",
    )
    assert overwritten["result"]["verdict"] == "design_bug"
    assert overwritten["result"]["doc"] == f"docs/debug/{run_id}/1.md"

    # Both cases in multi-case task have exactly one updated Triage record
    cases_map = {c.name: c for c in collect_cases(cfg)}
    for cname in ("burst", "burst_long"):
        triages = [r for r in cases_map[cname].notes if isinstance(r, Triage)]
        assert len(triages) == 1
        assert triages[0].verdict == "design_bug"
        assert triages[0].doc == f"docs/debug/{run_id}/1.md"
        assert triages[0].bugs == [bug_ref]
        assert triages[0].judged_status == {"burst__1": "fail", "burst_long__2": "fail"}

    # Review task -> rewriting afterwards MUST fail with TRIAGE_INVALID
    review_task(config=cfg, run_id=run_id, task_id=f"{run_id}/1", accept=True)
    with pytest.raises(ConfigError) as exc_rev:
        write_result(
            config=cfg,
            run_id=run_id,
            task_id=f"{run_id}/1",
            verdict="tb_bug",
            confidence="low",
            summary="after review",
            evidence=["log:50"],
            doc_text=_VALID_DOC,
        )
    assert exc_rev.value.code == "TRIAGE_INVALID"


# ------------------------------------------------------------------ review, status & stale tests


def test_review_accept_effects_reject_bug_invalidation_status_and_stale(tmp_path: Path) -> None:
    case_py = textwrap.dedent(
        """\
        from solouvm import Case, Expect

        CASES = [
            Case(
                name="pass_suspicious",
                stage="wip",
                intent="Verify scoreboard comparisons",
                expect=[Expect(scoreboard="AXI_SCB", min=1)],
            ),
            Case(
                name="env_victim",
                stage="ready",
                intent="Verify AXI slave",
                method="Drive burst",
                spec_refs=["S1"],
                expect=[Expect(scoreboard="AXI_SCB", min=1)],
            ),
            Case(
                name="rtl_victim",
                stage="ready",
                intent="Verify wrap",
                method="Drive wrap",
                spec_refs=["S2"],
                expect=[Expect(scoreboard="AXI_SCB", min=1)],
            ),
        ]
        """
    )
    root = _setup_basic_project(tmp_path, case_py=case_py)
    run_id = "20261118-140000-rv"
    _write(root / ".solouvm" / "build" / "default" / "b123" / ".ok", "")

    j_pass = _make_job(run_id, "pass_suspicious", 1)
    j_env = _make_job(run_id, "env_victim", 2)
    j_rtl = _make_job(run_id, "rtl_victim", 3)
    jobs = [j_pass, j_env, j_rtl]

    for j in jobs:
        _write(root / j.dir / "try1" / "job.facts", _facts_text(run_id=run_id, job_id=j.job_id))
    _write(root / j_pass.dir / "try1" / "sim.log", _UVM_CLEAN)
    _write(
        root / j_env.dir / "try1" / "sim.log",
        "UVM_ERROR env.sv(10) @ 5ns: uvm_test_top [ENV_DISK] NFS read error\n"
        "[AXI_SCB] compared=10 passed=10\n"
        "UVM_INFO env.sv(12) @ 100ns: reporter [TEST_DONE] done\n"
        "--- UVM Report Summary ---\n** Report counts by severity\n"
        "UVM_INFO : 1\nUVM_WARNING : 0\nUVM_ERROR : 1\nUVM_FATAL : 0\n",
    )
    _write(
        root / j_rtl.dir / "try1" / "sim.log",
        "UVM_ERROR rtl.sv(20) @ 8ns: uvm_test_top [AXI_SCB] wrap mismatch\n"
        "--- UVM Report Summary ---\n** Report counts by severity\n"
        "UVM_INFO : 0\nUVM_WARNING : 0\nUVM_ERROR : 1\nUVM_FATAL : 0\n",
    )

    cfg = load_config(root / "solouvm.conf.py")
    plan = Plan(
        run_id=run_id,
        created_at=T0,
        parent_run=None,
        provider="local",
        selection={},
        builds={"default": "b123"},
        jobs=jobs,
    )
    initial_results = judge_run(config=cfg, plan=plan, provider=_local_provider(root), today=TODAY)
    _write_plan_and_results(root, run_id, jobs, initial_results)
    update_latest_link(root, run_id)

    tasks = plan_tasks(config=cfg, run_id="latest")
    assert [t["cases"][0] for t in tasks] == ["env_victim", "rtl_victim", "pass_suspicious"]

    # Initially all 3 are unreviewed -> status exit code 1
    status_list, code = triage_status(config=cfg, run_id=run_id)
    assert code == 1
    assert [t["id"] for t in status_list] == [f"{run_id}/1", f"{run_id}/2", f"{run_id}/3"]

    # Register bug on rtl_victim for task 2
    rtl_bug = bug_add(
        config=cfg,
        case_name="rtl_victim",
        title="suspected rtl wrap bug",
        job_id="rtl_victim__3",
        role="triage",
        author="triage:a2",
        today=TODAY,
    )

    # Reject without reason / review before write_result / unknown task errors
    with pytest.raises(ConfigError) as exc_no_res:
        review_task(config=cfg, run_id=run_id, task_id=f"{run_id}/1", accept=True)
    assert exc_no_res.value.code == "TRIAGE_INVALID"

    with pytest.raises(ConfigError) as exc_unk:
        review_task(config=cfg, run_id=run_id, task_id=f"{run_id}/999", accept=True)
    assert exc_unk.value.code == "TASK_NOT_FOUND"

    # Write results for all 3 tasks
    write_result(
        config=cfg,
        run_id=run_id,
        task_id=f"{run_id}/1",
        verdict="env_issue",
        confidence="high",
        summary="NFS transient failure",
        evidence=["log:1"],
        doc_text=_VALID_DOC,
        author="triage:a1",
    )
    write_result(
        config=cfg,
        run_id=run_id,
        task_id=f"{run_id}/2",
        verdict="design_bug",
        confidence="medium",
        summary="suspect RTL wrap",
        evidence=["log:1"],
        bugs=[f"rtl_victim#{rtl_bug.id}"],
        doc_text=_VALID_DOC,
        author="triage:a2",
    )
    write_result(
        config=cfg,
        run_id=run_id,
        task_id=f"{run_id}/3",
        verdict="false_pass",
        confidence="high",
        summary="fake scb count",
        evidence=["log:2"],
        doc_text=_VALID_DOC,
        author="triage:a3",
    )

    with pytest.raises(ConfigError) as exc_no_reason:
        review_task(config=cfg, run_id=run_id, task_id=f"{run_id}/2", accept=False, reason="   ")
    assert exc_no_reason.value.code == "TRIAGE_INVALID"

    # Accept task 1 (env_issue) -> env_victim status becomes "error" in results.json
    rev1 = review_task(config=cfg, run_id=run_id, task_id=f"{run_id}/1", accept=True)
    assert rev1["review"]["decision"] == "accepted"
    # Accept task 3 (false_pass) -> pass_suspicious status becomes "false_pass" in results.json
    rev3 = review_task(config=cfg, run_id=run_id, task_id=f"{run_id}/3", accept=True)
    assert rev3["review"]["decision"] == "accepted"

    res_after_accept = read_results_json(run_dir(root, run_id) / "results.json")
    by_jid = {j["job_id"]: j for j in res_after_accept["jobs"]}
    assert by_jid["env_victim__2"]["judged_status"] == "fail"
    assert by_jid["env_victim__2"]["status"] == "error"
    assert by_jid["pass_suspicious__1"]["judged_status"] == "pass"
    assert by_jid["pass_suspicious__1"]["status"] == "false_pass"

    # Task 2 is still unreviewed -> status puts unreviewed task 2 FIRST and returns exit code 1!
    status_mid, code_mid = triage_status(config=cfg, run_id=run_id)
    assert code_mid == 1
    assert status_mid[0]["id"] == f"{run_id}/2"
    assert [t["id"] for t in status_mid] == [f"{run_id}/2", f"{run_id}/1", f"{run_id}/3"]

    # Reject task 2 with reason -> bug becomes invalid with reason in body; debug doc preserved
    rej_reason = "Monitor sampling race, not an RTL bug"
    rev2 = review_task(
        config=cfg,
        run_id=run_id,
        task_id=f"{run_id}/2",
        accept=False,
        reason=rej_reason,
    )
    assert rev2["review"]["decision"] == "rejected"
    assert (root / "docs" / "debug" / run_id / "2.md").is_file()

    cases_after_rej = {c.name: c for c in collect_cases(cfg)}
    rej_bug = next(r for r in cases_after_rej["rtl_victim"].notes if isinstance(r, Bug))
    assert rej_bug.status == "invalid"
    assert rej_reason in rej_bug.body
    rtl_triage = next(r for r in cases_after_rej["rtl_victim"].notes if isinstance(r, Triage))
    assert rtl_triage.review["decision"] == "rejected"

    # All tasks reviewed -> exit code 0!
    status_done, code_done = triage_status(config=cfg, run_id=run_id)
    assert code_done == 0
    assert [t["id"] for t in status_done] == [f"{run_id}/1", f"{run_id}/2", f"{run_id}/3"]

    # Now add a Waiver to env_victim so its mechanical judged_status flips from fail -> pass!
    with open(root / "case.py", encoding="utf-8") as fh:
        src = fh.read()
    src = src.replace(
        "from solouvm import Bug, Case, Expect, Triage",
        "from solouvm import Bug, Case, Expect, Triage, Waiver",
    ).replace(
        'name="env_victim",',
        'name="env_victim",\n        checks={"waivers": [Waiver(id="ENV_DISK", reason="waived")]},',
    )
    _write(root / "case.py", src)

    fresh_cfg = load_config(root / "solouvm.conf.py")
    rejudged = judge_run(config=fresh_cfg, plan=plan, provider=_local_provider(root), today=TODAY)
    write_results(run_dir(root, run_id), rejudged, run_id=run_id, partial=False, judged_at=T0)

    # Mechanical result changed -> stale=True, so accepted env_issue no longer overrides status!
    rejudged_by_id = {r.job_id: r for r in rejudged}
    assert rejudged_by_id["env_victim__2"].judged_status == "pass"
    assert rejudged_by_id["env_victim__2"].status == "pass"
    assert rejudged_by_id["env_victim__2"].triage["stale"] is True

    # Manually mark task 3 unreviewed to verify full priority ordering:
    # 0 = unreviewed (task 3), 1 = stale (task 1), 2 = reviewed & fresh (task 2)
    tpath = run_dir(root, run_id) / "triage" / "tasks.json"
    tdata = read_tasks_json(tpath)
    tdata["tasks"][2]["review"] = None
    _write(tpath, json.dumps(tdata, indent=2) + "\n")

    ordered_final, final_code = triage_status(config=fresh_cfg, run_id=run_id)
    assert final_code == 1
    assert [t["id"] for t in ordered_final] == [f"{run_id}/3", f"{run_id}/1", f"{run_id}/2"]
    assert ordered_final[1]["stale"] is True
    assert ordered_final[2]["stale"] is False


def test_role_enforcement_on_write_and_review(tmp_path: Path) -> None:
    root = _setup_basic_project(tmp_path)
    cfg = load_config(root / "solouvm.conf.py")
    run_id = "R_ROLE"
    _write_plan_and_results(
        root,
        run_id,
        [],
        [_res(job_id="burst__1", case="burst", status="fail", sig_id="sig1")],
    )
    plan_tasks(config=cfg, run_id=run_id)

    with pytest.raises(RoleError) as exc_w:
        write_result(
            config=cfg,
            run_id=run_id,
            task_id=f"{run_id}/1",
            verdict="design_bug",
            confidence="high",
            summary="x",
            evidence=["log:1"],
            doc_text=_VALID_DOC,
            role="bogus",
        )
    assert exc_w.value.code == "ROLE_DENIED"

    write_result(
        config=cfg,
        run_id=run_id,
        task_id=f"{run_id}/1",
        verdict="design_bug",
        confidence="high",
        summary="x",
        evidence=["log:1"],
        doc_text=_VALID_DOC,
        role="triage",
    )

    with pytest.raises(RoleError) as exc_r:
        review_task(
            config=cfg,
            run_id=run_id,
            task_id=f"{run_id}/1",
            accept=True,
            role="triage",
        )
    assert exc_r.value.code == "ROLE_DENIED"


def test_plan_killed_xpass_weak_signals_off_case_dirs_and_stale_refresh(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write(
        root / "solouvm.conf.py",
        'CFG = {"project": "demo", "simulator": "vcs", "top": "tb_top", "case_dirs": ["cases"]}\n',
    )
    _write(
        root / "cases" / "alpha.py",
        textwrap.dedent(
            """\
            from solouvm import Case, Expect

            CASE = Case(
                name="alpha",
                stage="ready",
                intent="Alpha",
                method="M",
                spec_refs=["S1"],
                expect=[Expect(scoreboard="SCB", min=1)],
            )
            """
        ),
    )
    _write(
        root / "cases" / "beta.py",
        textwrap.dedent(
            """\
            from solouvm import Case, Expect

            CASE = Case(
                name="beta",
                stage="ready",
                intent="Beta",
                method="M",
                spec_refs=["S2"],
                expect=[Expect(scoreboard="SCB", min=1)],
            )
            """
        ),
    )
    cfg = load_config(root / "solouvm.conf.py")

    # R0 baseline with both passing
    r0_res = [
        _res(job_id="alpha__1", case="alpha", status="pass", reason="clean"),
        _res(job_id="beta__1", case="beta", status="pass", reason="clean"),
    ]
    _write_plan_and_results(
        root, "R0", [_make_job("R0", "alpha", 1), _make_job("R0", "beta", 1)], r0_res
    )

    # Touch only cases/alpha.py after R0/plan.json, ensure cases/beta.py is older
    r0_plan_ns = (run_dir(root, "R0") / "plan.json").stat().st_mtime_ns
    os.utime(
        root / "cases" / "alpha.py", ns=(r0_plan_ns + 5_000_000_000, r0_plan_ns + 5_000_000_000)
    )
    os.utime(
        root / "cases" / "beta.py", ns=(r0_plan_ns - 5_000_000_000, r0_plan_ns - 5_000_000_000)
    )

    r1_res = [
        _res(job_id="alpha__1", case="alpha", status="pass", reason="clean"),
        _res(
            job_id="beta__1",
            case="beta",
            status="pass",
            reason="clean",
            weak_signals=["sim time 0ns"],
        ),
        _res(
            job_id="alpha__2",
            case="alpha",
            seed=2,
            status="killed",
            reason="killed by SIGSEGV",
            sig_id="sig_kill",
        ),
        _res(
            job_id="beta__2",
            case="beta",
            seed=2,
            status="xpass",
            reason="expected to fail but passed",
            sig_id="xpass",
        ),
    ]
    _write_plan_and_results(root, "R1", [], r1_res)
    tasks = plan_tasks(config=cfg, run_id="R1")
    # alpha__2 (killed), beta__2 (xpass), alpha__1 (changed alpha.py), beta__1 (weak signal)
    assert [(t["kind"], t["cases"], t["jobs"][0]["judged_status"]) for t in tasks] == [
        ("fail", ["alpha"], "killed"),
        ("fail", ["beta"], "xpass"),
        ("pass", ["alpha"], "pass"),
        ("pass", ["beta"], "pass"),
    ]

    # When weak_signals=False, beta__1 (unchanged & weak signal ignored) is not added to a fresh run
    cfg_no_weak = load_config(root / "solouvm.conf.py", cli={"triage": {"weak_signals": False}})
    _write_plan_and_results(
        root,
        "R2",
        [],
        [
            _res(
                job_id="beta__1",
                case="beta",
                status="pass",
                reason="clean",
                weak_signals=["sim time 0ns"],
            )
        ],
    )
    assert plan_tasks(config=cfg_no_weak, run_id="R2", pass_policy="none") == []

    # Re-calling plan_tasks after alpha__2 flips from killed -> pass marks R1/1 stale=True!
    updated_r1 = [
        _res(job_id="alpha__1", case="alpha", status="pass", reason="clean"),
        _res(
            job_id="beta__1",
            case="beta",
            status="pass",
            reason="clean",
            weak_signals=["sim time 0ns"],
        ),
        _res(job_id="alpha__2", case="alpha", seed=2, status="pass", reason="clean"),
        _res(
            job_id="beta__2",
            case="beta",
            seed=2,
            status="xpass",
            reason="expected to fail but passed",
            sig_id="xpass",
        ),
    ]
    refreshed = plan_tasks(config=cfg, run_id="R1", results=updated_r1)
    assert refreshed[0]["id"] == "R1/1"
    assert refreshed[0]["stale"] is True
    assert refreshed[1]["stale"] is False


def test_write_and_review_additional_validation_and_custom_debug_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _TtyStdin(io.StringIO):
        def isatty(self) -> bool:
            return True

    root = _setup_basic_project(
        tmp_path,
        conf_extra=', "triage": {"debug_dir": "custom/debug"}',
    )
    cfg = load_config(root / "solouvm.conf.py")
    run_id = "R_EXTRA"
    # No plan.json created -> review_task must still update tasks.json & case notes cleanly
    rdir = run_dir(root, run_id)
    rdir.mkdir(parents=True)
    write_results(
        rdir,
        [
            _res(job_id="burst__1", case="burst", status="fail", sig_id="sig1"),
            _res(job_id="wip_case__2", case="wip_case", seed=2, status="pass", reason="clean"),
        ],
        run_id=run_id,
        partial=False,
        judged_at=T0,
    )
    tasks = plan_tasks(config=cfg, run_id=run_id)
    assert len(tasks) == 2

    # TTY stdin rejected with CFG_INVALID
    monkeypatch.setattr("sys.stdin", _TtyStdin())
    with pytest.raises(ConfigError) as exc_tty:
        write_result(
            config=cfg,
            run_id=run_id,
            task_id=f"{run_id}/1",
            verdict="design_bug",
            confidence="high",
            summary="x",
            evidence=["log:1"],
            doc_text="-",
        )
    assert exc_tty.value.code == "CFG_INVALID"

    # Invalid confidence / empty summary / bad bug ref syntax
    with pytest.raises(ConfigError) as exc_conf:
        write_result(
            config=cfg,
            run_id=run_id,
            task_id=f"{run_id}/1",
            verdict="design_bug",
            confidence="ultra",
            summary="x",
            evidence=["log:1"],
            doc_text=_VALID_DOC,
        )
    assert exc_conf.value.code == "TRIAGE_INVALID"

    with pytest.raises(ConfigError) as exc_empty_sum:
        write_result(
            config=cfg,
            run_id=run_id,
            task_id=f"{run_id}/1",
            verdict="design_bug",
            confidence="high",
            summary="   ",
            evidence=["log:1"],
            doc_text=_VALID_DOC,
        )
    assert exc_empty_sum.value.code == "TRIAGE_INVALID"

    with pytest.raises(RecordError) as exc_bad_ref:
        write_result(
            config=cfg,
            run_id=run_id,
            task_id=f"{run_id}/1",
            verdict="design_bug",
            confidence="high",
            summary="ok",
            evidence=["log:1"],
            bugs=["badref"],
            doc_text=_VALID_DOC,
        )
    assert exc_bad_ref.value.code == "RECORD_INVALID"

    # Exact 200-char summary allowed; custom debug_dir used
    res1 = write_result(
        config=cfg,
        run_id=run_id,
        task_id=f"{run_id}/1",
        verdict="spec_unclear",
        confidence="low",
        summary="s" * 200,
        evidence=["rtl/axi.sv:10"],
        doc_text=_VALID_DOC,
    )
    assert res1["result"]["doc"] == f"custom/debug/{run_id}/1.md"
    assert (root / "custom" / "debug" / run_id / "1.md").is_file()

    # Pass task accepts real_pass
    res2 = write_result(
        config=cfg,
        run_id=run_id,
        task_id=f"{run_id}/2",
        verdict="real_pass",
        confidence="high",
        summary="verified stimulus",
        evidence=["log:12"],
        doc_text=_VALID_DOC,
    )
    assert res2["result"]["verdict"] == "real_pass"

    review_task(config=cfg, run_id=run_id, task_id=f"{run_id}/1", accept=True)
    review_task(config=cfg, run_id=run_id, task_id=f"{run_id}/2", accept=True)
    st, code = triage_status(config=cfg, run_id=run_id)
    assert code == 0
    assert len(st) == 2

    # Empty tasks.json status returns ([], 0); missing tasks.json raises TASK_NOT_FOUND
    _write_plan_and_results(root, "R_EMPTY", [], [])
    assert plan_tasks(config=cfg, run_id="R_EMPTY") == []
    assert triage_status(config=cfg, run_id="R_EMPTY") == ([], 0)

    _write_plan_and_results(root, "R_NO_TASKS", [], [])
    with pytest.raises(ConfigError) as exc_nt:
        triage_status(config=cfg, run_id="R_NO_TASKS")
    assert exc_nt.value.code == "TASK_NOT_FOUND"
