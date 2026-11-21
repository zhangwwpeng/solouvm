"""Tests for solouvm.explain (Task 19, AI-04, AI-15, K-11)."""

from __future__ import annotations

import json
import shlex
import subprocess
import textwrap
from pathlib import Path

import pytest

from solouvm.config import load_config
from solouvm.errors import ConfigError
from solouvm.explain import (
    EXPLAIN_SECTIONS,
    explain,
    explain_data,
    extract_source_location,
    find_previous_pass,
    find_signature_history,
    git_commits_since_pass,
    read_log_context,
    read_source_snippet,
)
from solouvm.history import update_latest_link
from solouvm.plan import read_plan
from solouvm.providers import LocalTransport, Provider
from solouvm.providers.launcher_make import MakeLauncher
from solouvm.providers.sync_none import NoneSync


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _setup_project(tmp_path: Path, *, case_py: str | None = None) -> Path:
    root = tmp_path / "proj"
    _write(root / "solouvm.conf.py", 'CFG = {"project": "demo", "simulator": "vcs", "top": "tb"}\n')
    _write(
        root / "case.py",
        case_py
        or textwrap.dedent(
            """\
            from solouvm import Bug, Case, Expect, Triage

            CASES = [
                Case(
                    name="burst",
                    owner="wp",
                    expect=[Expect(scoreboard="AXI_SCB", min=100)],
                    notes=[
                        Bug(
                            id=1,
                            title="WRAP 4K boundary wrap bug",
                            status="open",
                            seed=4417,
                            found="2026-11-02",
                            signature="sig11111",
                        ),
                        Triage(
                            task="20261101-100000-r1/1",
                            verdict="design_bug",
                            confidence="high",
                            summary="addr_q wraps to 0 on beat 2",
                            evidence=["log:50", "verif/envs/axi/axi_scb.sv:120"],
                            judged_status={"burst__4417": "fail"},
                            doc="docs/debug/20261101-100000-r1/1.md",
                            bugs=["burst#1"],
                        ),
                    ],
                ),
            ]
            """
        ),
    )
    return root


def _write_run(
    root: Path,
    run_id: str,
    *,
    created_at: str,
    jobs_plan: list[dict[str, object]],
    jobs_results: list[dict[str, object]],
    commit: str | None = None,
) -> Path:
    rdir = root / ".solouvm" / "runs" / run_id
    rdir.mkdir(parents=True, exist_ok=True)
    plan_payload = {
        "schema": 1,
        "run_id": run_id,
        "created_at": created_at,
        "parent_run": None,
        "provider": "local",
        "selection": {"case": [], "tag": [], "exclude_tag": [], "build": None, "stage": ["wip"]},
        "builds": {"default": "deadbeef"},
        "jobs": jobs_plan,
    }
    _write(rdir / "plan.json", json.dumps(plan_payload, indent=2) + "\n")
    if commit is not None:
        env_payload = {
            "schema": 1,
            "run_id": run_id,
            "git": {"commit": commit, "branch": "main", "dirty": False, "diff": None},
        }
        _write(rdir / "env.json", json.dumps(env_payload, indent=2) + "\n")
    passed = sum(1 for j in jobs_results if j["status"] in ("pass", "xfail", "skip"))
    res_payload = {
        "schema": 1,
        "run_id": run_id,
        "judged_at": created_at,
        "partial": False,
        "summary": {
            "total": len(jobs_results),
            "passed": passed,
            "by_status": {},
            "by_category": {},
            "duration_s": 1.0,
        },
        "jobs": jobs_results,
        "signatures": [],
        "waivers": {"used": [], "unused": [], "expired": []},
        "hints": [],
    }
    _write(rdir / "results.json", json.dumps(res_payload, indent=2) + "\n")
    return rdir


def _plan_job(
    run_id: str, job_id: str = "burst__4417", case: str = "burst", seed: int = 4417
) -> dict[str, object]:
    return {
        "job_id": job_id,
        "case": case,
        "seed": seed,
        "build": "default",
        "uvm_test": "axi_burst_test",
        "params": {},
        "opts": [],
        "env": {},
        "timeout_s": 600.0,
        "hang_s": None,
        "retry": 0,
        "expect_fail": False,
        "tags": [],
        "dir": f".solouvm/runs/{run_id}/{job_id}",
    }


def _make_sv_file(path: Path, total_lines: int = 150) -> None:
    lines = [f"// sv line {i}\n" for i in range(1, total_lines + 1)]
    lines[119] = '  if (addr_q == 0) `uvm_error("AXI_SCB", "mismatch")\n'
    _write(path, "".join(lines))


def _make_sim_log(path: Path, total_lines: int = 100, err_line: int = 50) -> None:
    lines = [
        f"UVM_INFO info.sv(10) @ {i}ns: uvm_test_top [INFO] line {i}\n"
        for i in range(1, total_lines + 1)
    ]
    lines[err_line - 1] = (
        "UVM_ERROR verif/envs/axi/axi_scb.sv(120) @ 1234ns: "
        "uvm_test_top.env.scb [AXI_SCB] mismatch: expect 0x00 got 0x1f\n"
    )
    lines[err_line + 1] = 'Error-[NOA] Null object access "rtl/axi_slv.sv", 12\n'
    _write(path, "".join(lines))


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def test_extract_source_location_three_formats():
    assert extract_source_location(
        "UVM_ERROR verif/envs/axi/axi_scb.sv(120) @ 1234ns: [ID] msg"
    ) == ("verif/envs/axi/axi_scb.sv", 120)
    assert extract_source_location('Error-[NOA] "rtl/axi_slv.sv", 120') == ("rtl/axi_slv.sv", 120)
    assert extract_source_location("xmsim: *E,ERR: rtl/axi_slv.sv:120: bad") == (
        "rtl/axi_slv.sv",
        120,
    )
    assert extract_source_location("plain message without file") is None


def test_read_source_snippet_coverage_and_missing(tmp_path: Path):
    sv_file = tmp_path / "verif" / "envs" / "axi" / "axi_scb.sv"
    _make_sv_file(sv_file, total_lines=140)

    snippet = read_source_snippet(tmp_path, "verif/envs/axi/axi_scb.sv", 120, context=5)
    snippet_lines = snippet.splitlines()
    assert len(snippet_lines) == 11
    assert "  115  // sv line 115" in snippet_lines[0]
    assert snippet_lines[5].startswith(">   120  ")
    assert "  125  // sv line 125" in snippet_lines[-1]

    missing = read_source_snippet(tmp_path, "rtl/missing.sv", 10)
    assert missing == "source not available: rtl/missing.sv"

    out_of_range = read_source_snippet(tmp_path, "verif/envs/axi/axi_scb.sv", 9999)
    assert "out of range" in out_of_range


def test_read_log_context_local_and_transport(tmp_path: Path):
    log_path = tmp_path / "sim.log"
    _make_sim_log(log_path, total_lines=100, err_line=50)

    ctx = read_log_context(log_path, 50, context=30)
    ctx_lines = ctx.splitlines()
    assert len(ctx_lines) == 61
    assert "    20  " in ctx_lines[0]
    assert ">     50  UVM_ERROR verif/envs/axi/axi_scb.sv(120)" in ctx
    assert "    80  " in ctx_lines[-1]

    transport = LocalTransport(root=tmp_path)
    t_ctx = read_log_context(Path("sim.log"), 50, context=5, transport=transport)
    assert ">     50  UVM_ERROR" in t_ctx
    assert len(t_ctx.splitlines()) == 11

    missing_ctx = read_log_context(tmp_path / "deleted.log", 50, context=30)
    assert "not available" in missing_ctx


def test_explain_unknown_run_missing_results_and_unknown_job(tmp_path: Path):
    root = _setup_project(tmp_path)
    config = load_config(root / "solouvm.conf.py")

    rdir = _write_run(
        root,
        "20261110-100000-r1",
        created_at="2026-11-10T10:00:00+08:00",
        jobs_plan=[_plan_job("20261110-100000-r1")],
        jobs_results=[],
    )
    plan = read_plan(rdir / "plan.json")

    with pytest.raises(ConfigError) as exc_run:
        explain(config=config, plan=plan, run_id="no_such_run", job_id="burst__4417")
    assert exc_run.value.code == "RUN_NOT_FOUND"

    (rdir / "results.json").unlink()
    with pytest.raises(ConfigError) as exc_no_res:
        explain(config=config, plan=plan, run_id="20261110-100000-r1", job_id="burst__4417")
    assert exc_no_res.value.code == "RUN_NOT_FOUND"
    assert exc_no_res.value.hint == "run 'solouvm judge' first"

    _write_run(
        root,
        "20261110-100000-r1",
        created_at="2026-11-10T10:00:00+08:00",
        jobs_plan=[_plan_job("20261110-100000-r1")],
        jobs_results=[{"job_id": "burst__4417", "case": "burst", "seed": 4417, "status": "fail"}],
    )
    with pytest.raises(ConfigError) as exc_job:
        explain(config=config, plan=plan, run_id="20261110-100000-r1", job_id="missing__1")
    assert exc_job.value.code == "JOB_NOT_FOUND"


def test_explain_full_eight_sections_history_git_and_repro(tmp_path: Path):
    root = _setup_project(tmp_path)
    _make_sv_file(root / "verif" / "envs" / "axi" / "axi_scb.sv", total_lines=140)
    _write(root / "rtl" / "axi_slv.sv", "".join(f"// rtl {i}\n" for i in range(1, 30)))

    _git(root, "init")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial commit")
    c1 = _git(root, "rev-parse", "HEAD")

    _write(root / "rtl" / "fix.sv", "module fix; endmodule\n")
    _git(root, "add", "rtl/fix.sv")
    _git(root, "commit", "-m", "modify axi_slv logic")

    # Run 1 (earlier pass)
    _write_run(
        root,
        "zzz_pass_run",
        created_at="2026-11-01T09:00:00+08:00",
        jobs_plan=[_plan_job("zzz_pass_run")],
        jobs_results=[
            {
                "job_id": "burst__4417",
                "case": "burst",
                "seed": 4417,
                "status": "pass",
                "judged_status": "pass",
                "raw_status": "pass",
                "category": None,
                "reason": "clean",
            }
        ],
        commit=c1,
    )

    # Run 2 (earlier fail with same signature)
    _write_run(
        root,
        "mmm_old_fail",
        created_at="2026-11-02T09:00:00+08:00",
        jobs_plan=[_plan_job("mmm_old_fail")],
        jobs_results=[
            {
                "job_id": "burst__4417",
                "case": "burst",
                "seed": 4417,
                "status": "fail",
                "judged_status": "fail",
                "raw_status": "fail",
                "category": "design",
                "reason": "UVM_ERROR=1 > 0",
                "signature": {"id": "sig11111", "text": "UVM_ERROR [AXI_SCB] mismatch"},
            }
        ],
        commit=c1,
    )

    # Run 3 (current fail; name starts with 'aaa' to prove created_at sorting, not dir-name sorting)
    run3 = "aaa_cur_fail"
    log_rel = f".solouvm/runs/{run3}/burst__4417/try1/sim.log"
    _make_sim_log(root / log_rel, total_lines=100, err_line=50)
    r3_dir = _write_run(
        root,
        run3,
        created_at="2026-11-03T09:00:00+08:00",
        jobs_plan=[_plan_job(run3)],
        jobs_results=[
            {
                "job_id": "burst__4417",
                "case": "burst",
                "seed": 4417,
                "status": "fail",
                "judged_status": "fail",
                "raw_status": "fail",
                "category": "design",
                "reason": "UVM_ERROR=1 > 0",
                "first_error": {
                    "line": 50,
                    "text": "UVM_ERROR verif/envs/axi/axi_scb.sv(120) @ 1234ns: [AXI_SCB] mismatch",
                },
                "signature": {"id": "sig11111", "text": "UVM_ERROR [AXI_SCB] mismatch"},
                "expect": [
                    {
                        "rule": "scoreboard AXI_SCB min 100",
                        "ok": False,
                        "value": 42,
                        "detail": "AXI_SCB compared=42 < 100",
                    }
                ],
                "weak_signals": ["sim time 0ns", "no UVM messages from uvm_test_top"],
                "known": ["burst#1"],
                "triage": {
                    "task": "20261101-100000-r1/1",
                    "verdict": "design_bug",
                    "confidence": "high",
                    "review": "pending",
                    "doc": "docs/debug/20261101-100000-r1/1.md",
                },
                "attempts": [
                    {
                        "try": 1,
                        "status": "fail",
                        "reason": "UVM_ERROR=1 > 0",
                        "duration_s": 2.5,
                        "host": "local",
                        "log": log_rel,
                        "wave": False,
                    }
                ],
                "repro": {
                    "make": f"make -f .solouvm/runs/{run3}/run.mk TRY=r1 WAVE=1 run/burst__4417",
                    "solouvm": "solouvm run --case burst --seed 4417 --wave",
                },
            }
        ],
    )
    update_latest_link(root, run3)

    config = load_config(root / "solouvm.conf.py")
    plan = read_plan(r3_dir / "plan.json")
    provider = Provider(
        name="local",
        transport=LocalTransport(root=root),
        launcher=MakeLauncher(),
        sync=NoneSync(),
    )

    md = explain(config=config, plan=plan, run_id="latest", job_id="burst__4417", provider=provider)
    for heading in EXPLAIN_SECTIONS:
        assert heading in md

    assert run3 in md
    assert ">     50  UVM_ERROR verif/envs/axi/axi_scb.sv(120)" in md
    assert ">   120    if (addr_q == 0)" in md
    assert "rtl/axi_slv.sv:12" in md
    assert "scoreboard AXI_SCB min 100" in md and "42" in md
    assert "sim time 0ns" in md
    assert "no UVM messages from uvm_test_top" in md
    assert "mmm_old_fail" in md
    assert "burst#1" in md and "WRAP 4K boundary wrap bug" in md
    assert "20261101-100000-r1/1" in md and "addr_q wraps to 0 on beat 2" in md
    assert "zzz_pass_run" in md
    assert "modify axi_slv logic" in md

    data = explain_data(config=config, plan=plan, run_id=run3, job_id="burst__4417")
    make_tokens = shlex.split(data["repro"]["make"])
    assert make_tokens[0] == "make"
    assert "-f" in make_tokens
    assert "TRY=r1" in make_tokens
    assert "WAVE=1" in make_tokens
    assert "run/burst__4417" in make_tokens
    assert "solouvm run --case burst --seed 4417 --wave" in md

    assert find_previous_pass(root, "burst", run3) == "zzz_pass_run"
    assert len(find_signature_history(root, "sig11111", exclude_run_id=run3)) == 1


def test_explain_try_num_and_no_first_error(tmp_path: Path):
    root = _setup_project(tmp_path)
    run_id = "20261110-130000-r2"
    try1_log = f".solouvm/runs/{run_id}/burst__4417/try1/sim.log"
    try2_log = f".solouvm/runs/{run_id}/burst__4417/try2/sim.log"
    _write(root / try1_log, "try1 line\n")
    _write(root / try2_log, "try2 timeout line 1\ntry2 timeout line 2\n")
    rdir = _write_run(
        root,
        run_id,
        created_at="2026-11-10T13:00:00+08:00",
        jobs_plan=[_plan_job(run_id)],
        jobs_results=[
            {
                "job_id": "burst__4417",
                "case": "burst",
                "seed": 4417,
                "status": "timeout",
                "judged_status": "timeout",
                "raw_status": "timeout",
                "category": "execution",
                "reason": "wall-clock timeout after 20m",
                "first_error": None,
                "signature": {"id": "timeout", "text": "timeout"},
                "attempts": [
                    {"try": 1, "status": "timeout", "log": try1_log, "wave": False},
                    {"try": 2, "status": "timeout", "log": try2_log, "wave": True},
                ],
                "repro": {},
            }
        ],
    )
    config = load_config(root / "solouvm.conf.py")
    plan = read_plan(rdir / "plan.json")

    md_try2 = explain(config=config, plan=plan, run_id=run_id, job_id="burst__4417", try_num=2)
    assert "try2 timeout line 2" in md_try2
    assert "First error: none" in md_try2

    with pytest.raises(ConfigError) as exc_try:
        explain(config=config, plan=plan, run_id=run_id, job_id="burst__4417", try_num=99)
    assert exc_try.value.code == "JOB_NOT_FOUND"


def test_explain_missing_log_and_missing_git(tmp_path: Path):
    root = _setup_project(tmp_path)
    run_id = "20261110-120000-r1"
    rdir = _write_run(
        root,
        run_id,
        created_at="2026-11-10T12:00:00+08:00",
        jobs_plan=[_plan_job(run_id)],
        jobs_results=[
            {
                "job_id": "burst__4417",
                "case": "burst",
                "seed": 4417,
                "status": "fail",
                "judged_status": "fail",
                "raw_status": "fail",
                "category": "design",
                "reason": "UVM_ERROR=1 > 0",
                "first_error": {
                    "line": 10,
                    "text": "UVM_ERROR verif/envs/axi/axi_scb.sv(120) @ 5ns: [AXI_SCB] err",
                },
                "signature": {"id": "sig11111", "text": "UVM_ERROR [AXI_SCB] err"},
                "expect": [],
                "weak_signals": [],
                "known": [],
                "attempts": [
                    {
                        "try": 1,
                        "status": "fail",
                        "reason": "UVM_ERROR=1 > 0",
                        "duration_s": 1.0,
                        "host": "local",
                        "log": f".solouvm/runs/{run_id}/burst__4417/try1/sim.log",
                        "wave": False,
                    }
                ],
                "repro": {},
            }
        ],
    )
    config = load_config(root / "solouvm.conf.py")
    plan = read_plan(rdir / "plan.json")
    md = explain(config=config, plan=plan, run_id=run_id, job_id="burst__4417")

    for heading in EXPLAIN_SECTIONS:
        assert heading in md
    assert "log not available:" in md
    assert "source not available: verif/envs/axi/axi_scb.sv" in md
    assert "git unavailable" in md
    assert git_commits_since_pass(root, None) == "git unavailable"
