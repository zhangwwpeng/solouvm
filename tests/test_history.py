"""Tests for solouvm.history (Y-01..Y-06, J-17, J-19)."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from solouvm.backends import get_backend
from solouvm.config import load_config
from solouvm.errors import ConfigError
from solouvm.history import (
    ENV_SCHEMA,
    RUNS_REL,
    capture_env,
    case_historical_medians,
    case_history,
    detect_sim_version,
    list_history,
    prune_history,
    record_run_stats,
    resolve_run_id,
    run_dir,
    runs_dir,
    update_latest_link,
    write_env,
)
from solouvm.providers import LocalTransport, Provider, get_provider
from solouvm.providers.launcher_make import MakeLauncher
from solouvm.providers.sync_none import NoneSync
from solouvm.providers.transport_ssh import SshTransport


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_project(tmp_path: Path, name: str = "proj") -> Path:
    root = tmp_path / name
    _write(root / "solouvm.conf.py", 'CFG = {"simulator": "fake", "top": "tb_top"}\n')
    _write(root / "case.py", "from solouvm import Case\nCASES = [Case(name='smoke')]\n")
    return root


def _write_plan_file(
    root: Path,
    run_id: str,
    created_at: datetime,
    *,
    builds: dict[str, str] | None = None,
    jobs: list[dict[str, object]] | None = None,
) -> Path:
    rdir = run_dir(root, run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "run_id": run_id,
        "created_at": created_at.isoformat(timespec="seconds"),
        "parent_run": None,
        "provider": "local",
        "selection": {"case": [], "tag": [], "exclude_tag": [], "build": None, "stage": ["wip"]},
        "builds": builds if builds is not None else {"default": "111111111111"},
        "jobs": jobs if jobs is not None else [],
    }
    (rdir / "plan.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return rdir


def _write_results_file(
    root: Path,
    run_id: str,
    judged_at: datetime,
    jobs: list[dict[str, object]],
    *,
    partial: bool = False,
) -> None:
    rdir = run_dir(root, run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    passed = sum(1 for j in jobs if j["status"] in ("pass", "xfail", "skip"))
    dur = round(
        sum(
            float(att.get("duration_s") or 0.0)
            for j in jobs
            for att in j.get("attempts", [])
            if not att.get("wave")
        ),
        3,
    )
    payload = {
        "schema": 1,
        "run_id": run_id,
        "judged_at": judged_at.isoformat(timespec="seconds"),
        "partial": partial,
        "summary": {
            "total": len(jobs),
            "passed": passed,
            "by_status": {},
            "by_category": {},
            "duration_s": dur,
        },
        "jobs": jobs,
        "signatures": [],
        "waivers": {"used": [], "unused": [], "expired": []},
        "hints": [],
    }
    (rdir / "results.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def test_runs_dir_and_resolve_latest_and_fallback(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    assert runs_dir(root) == root / RUNS_REL
    assert run_dir(root, "R1") == root / RUNS_REL / "R1"

    with pytest.raises(ConfigError) as exc_missing:
        resolve_run_id(root, None)
    assert exc_missing.value.code == "RUN_NOT_FOUND"

    t0 = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    _write_plan_file(root, "R1", t0)
    _write_plan_file(root, "R2", t0 + timedelta(minutes=5))

    # Without latest symlink: resolves newest run containing plan.json
    assert resolve_run_id(root, None) == "R2"
    assert resolve_run_id(root, "latest") == "R2"

    # With symlink pointing to R1: honors symlink and returns actual run_id
    update_latest_link(root, "R1")
    assert (runs_dir(root) / "latest").is_symlink()
    assert os.readlink(runs_dir(root) / "latest") == "R1"
    assert resolve_run_id(root, None) == "R1"
    assert resolve_run_id(root, "latest") == "R1"

    # Atomic update to R2
    update_latest_link(root, "R2")
    assert resolve_run_id(root, "latest") == "R2"
    assert resolve_run_id(root, "R1") == "R1"

    # Invalid / missing explicit run IDs
    for bad in ("NO_SUCH_RUN", "../R1", ".", ".."):
        with pytest.raises(ConfigError) as exc_bad:
            resolve_run_id(root, bad)
        assert exc_bad.value.code == "RUN_NOT_FOUND"


def test_capture_and_write_env_non_git_and_git_dirty(tmp_path: Path) -> None:
    root = _make_project(tmp_path, "nongit")
    cfg = load_config(root)
    backend = get_backend("fake")
    provider = get_provider(cfg)

    env = capture_env(
        root,
        run_id="R1",
        config=cfg,
        backend=backend,
        provider=provider,
        sim_version="FAKE-SIM 1.0",
    )
    assert list(env) == [
        "schema",
        "run_id",
        "solouvm",
        "python",
        "a_host",
        "b_host",
        "provider",
        "git",
        "simulator",
    ]
    assert env["schema"] == ENV_SCHEMA
    assert env["run_id"] == "R1"
    assert env["provider"] == {
        "name": "local",
        "transport": "local",
        "launcher": "make",
        "sync": "none",
    }
    assert env["git"] == {"commit": None, "branch": None, "dirty": False, "diff": None}
    assert env["simulator"] == {"name": "fake", "version": "FAKE-SIM 1.0"}
    assert env["b_host"] == env["a_host"]

    write_env(run_dir(root, "R1"), env)
    assert (run_dir(root, "R1") / "env.json").is_file()
    assert not (run_dir(root, "R1") / "git.diff").exists()

    # Now test a real git repository: clean then dirty
    repo = _make_project(tmp_path, "gitproj")
    _write(repo / "rtl.sv", "module dut; endmodule\n")
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "ci@example.com")
    _git(repo, "config", "user.name", "CI")
    _git(repo, "add", "solouvm.conf.py", "case.py", "rtl.sv")
    _git(repo, "commit", "-m", "init")

    cfg_git = load_config(repo)
    clean_env = capture_env(
        repo,
        run_id="R_CLEAN",
        config=cfg_git,
        backend=backend,
        provider=get_provider(cfg_git),
    )
    assert clean_env["git"]["dirty"] is False  # type: ignore[index]
    assert clean_env["git"]["diff"] is None  # type: ignore[index]
    assert isinstance(clean_env["git"]["commit"], str)  # type: ignore[index]
    assert len(clean_env["git"]["commit"]) == 40  # type: ignore[index,arg-type]

    # Modify tracked file -> dirty & git.diff saved
    _write(repo / "rtl.sv", "module dut; logic x; endmodule\n")
    ssh_prov = Provider(
        name="farm",
        transport=SshTransport(host="simbox01", workdir="/remote/proj", root=repo),
        launcher=MakeLauncher(),
        sync=NoneSync(),
    )
    dirty_env = capture_env(
        repo,
        run_id="R_DIRTY",
        config=cfg_git,
        backend=get_backend("vcs"),
        provider=ssh_prov,
    )
    assert dirty_env["b_host"] == "simbox01"
    assert dirty_env["git"]["dirty"] is True  # type: ignore[index]
    assert dirty_env["git"]["diff"] == "git.diff"  # type: ignore[index]

    rdir = run_dir(repo, "R_DIRTY")
    write_env(rdir, dirty_env)
    written_env = json.loads((rdir / "env.json").read_text(encoding="utf-8"))
    assert written_env["git"]["diff"] == "git.diff"
    diff_text = (rdir / "git.diff").read_text(encoding="utf-8")
    assert "logic x;" in diff_text


def test_detect_sim_version_success_and_failure(tmp_path: Path) -> None:
    root = _make_project(tmp_path)
    fake_backend = get_backend("fake")
    bin_dir = root / ".solouvm" / "gen" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    from solouvm.backends import fake_sim_source

    fake_script = bin_dir / "fake_sim.sh"
    fake_script.write_text(fake_sim_source(), encoding="utf-8")
    fake_script.chmod(0o755)

    prov = Provider(
        name="local",
        transport=LocalTransport(root=root),
        launcher=MakeLauncher(),
        sync=NoneSync(),
    )
    assert detect_sim_version(fake_backend, prov, root) == "FAKE-SIM 1.0"

    # Missing command or non-zero exit returns None without raising
    bad_backend = get_backend("vcs", {"version": "definitely_missing_sim_binary --version"})
    assert detect_sim_version(bad_backend, prov, root) is None
    no_ver_backend = get_backend("vcs", {"version": None})
    assert detect_sim_version(no_ver_backend, prov, root) is None
    fail_backend = get_backend("vcs", {"version": "false"})
    assert detect_sim_version(fail_backend, prov, root) is None


def test_list_history_and_case_history_flaky(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    t0 = datetime(2026, 11, 18, 8, 0, tzinfo=timezone.utc)

    for idx, (rid, dt, st) in enumerate(
        [
            ("R1", t0, "fail"),
            ("R2", t0 + timedelta(hours=1), "pass"),
            ("R3", t0 + timedelta(hours=2), "pass"),
        ]
    ):
        _write_plan_file(root, rid, dt)
        _write(
            run_dir(root, rid) / "env.json",
            json.dumps(
                {
                    "schema": 1,
                    "run_id": rid,
                    "git": {
                        "commit": f"abc{idx}",
                        "branch": "main",
                        "dirty": idx == 1,
                        "diff": None,
                    },
                }
            ),
        )
        _write_results_file(
            root,
            rid,
            dt + timedelta(minutes=5),
            [
                {
                    "job_id": "burst__1",
                    "case": "burst",
                    "seed": 1,
                    "status": st,
                    "judged_status": st,
                    "raw_status": st,
                    "reason": "clean" if st == "pass" else "UVM_ERROR=1 > 0",
                    "flaky": False,
                    "attempts": [{"try": 1, "duration_s": 2.5 + idx, "wave": False}],
                }
            ],
            partial=(rid == "R3"),
        )

    # Also add an active (running) unjudged run R4
    _write_plan_file(
        root,
        "R4",
        t0 + timedelta(hours=3),
        jobs=[{"job_id": "burst__1"}],
    )
    _write(
        run_dir(root, "R4") / "burst__1" / "try1" / "job.facts",
        (
            "schema=1\ngenerator=solouvm 0.1.0\nkind=job\nrun_id=R4\n"
            "job_id=burst__1\ntry=1\nhost=box\npid=99\ntimeout_impl=builtin\n"
            "started_at=2026-11-18T11:00:00+00:00\nstate=running\n"
        ),
    )

    hist = list_history(root)
    assert [h["run_id"] for h in hist] == ["R4", "R3", "R2", "R1"]
    assert hist[0]["status"] == "running"
    assert hist[0]["total"] == 1
    assert hist[0]["passed"] is None
    assert hist[1]["status"] == "partial"
    assert hist[1]["partial"] is True
    assert hist[2]["status"] == "pass"
    assert hist[2]["dirty"] is True
    assert hist[3]["status"] == "fail"
    assert len(list_history(root, limit=2)) == 2

    ch = case_history(root, "burst")
    assert [c["run_id"] for c in ch] == ["R3", "R2", "R1"]
    assert all(c["flaky"] is True for c in ch)
    # Even with limit=1, historical flaky status is preserved
    ch_lim = case_history(root, "burst", limit=1)
    assert len(ch_lim) == 1
    assert ch_lim[0]["flaky"] is True


def test_case_historical_medians_cache_and_fallback(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    t0 = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)

    # Run 1 via stats cache
    _write_plan_file(root, "R1", t0)
    record_run_stats(root, "R1", {"burst": {"sim_end_ns": [1000.0], "log_lines": [100]}})

    # Run 2 via raw results.json + sim.log fallback (no cache entry for R2)
    _write_plan_file(root, "R2", t0 + timedelta(hours=1))
    log2_rel = ".solouvm/runs/R2/burst__2/try1/sim.log"
    _write(
        root / log2_rel,
        "line 1\n"
        "UVM_INFO a.sv(1) @ 3000ns: reporter [TEST_DONE] done\n"
        + "\n".join(f"noise {i}" for i in range(198))
        + "\n",
    )
    _write_results_file(
        root,
        "R2",
        t0 + timedelta(hours=1),
        [
            {
                "job_id": "burst__2",
                "case": "burst",
                "seed": 2,
                "status": "pass",
                "attempts": [{"try": 1, "status": "pass", "duration_s": 1.0, "log": log2_rel}],
            }
        ],
    )

    # Run 3 (to be excluded)
    _write_plan_file(root, "R3", t0 + timedelta(hours=2))
    record_run_stats(root, "R3", {"burst": {"sim_end_ns": [999999.0], "log_lines": [999999]}})

    med_end, med_lines = case_historical_medians(root, "burst", exclude_run_id="R3")
    assert med_end == 2000.0  # median([1000.0, 3000.0])
    assert med_lines == 150.0  # median([100, 200])

    assert case_historical_medians(root, "unknown_case") == (None, None)


def test_prune_history_keeps_active_and_referenced_builds(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    t0 = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)

    # Create 3 runs referencing builds: R1->k_old, R2->k_shared, R3->k_shared
    _write_plan_file(root, "R1", t0, builds={"default": "k_old"})
    _write_plan_file(root, "R2", t0 + timedelta(hours=1), builds={"default": "k_shared"})
    _write_plan_file(root, "R3", t0 + timedelta(hours=2), builds={"default": "k_shared"})
    # Plus an older run R0 that is STILL active (running)
    _write_plan_file(root, "R0", t0 - timedelta(hours=1), builds={"default": "k_active"})
    _write(
        run_dir(root, "R0") / "smoke__1" / "try1" / "job.facts",
        (
            "schema=1\ngenerator=solouvm 0.1.0\nkind=job\nrun_id=R0\n"
            "job_id=smoke__1\ntry=1\nhost=box\npid=11\ntimeout_impl=builtin\n"
            "started_at=2026-11-18T09:00:00+00:00\nstate=running\n"
        ),
    )

    for key in ("k_old", "k_shared", "k_active", "k_orphan"):
        bdir = root / ".solouvm" / "build" / "default" / key
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / ".ok").write_text("", encoding="utf-8")

    update_latest_link(root, "R1")

    # Dry run keep=2 -> keeps R3, R2, and active R0; removes R1 and k_old, k_orphan
    preview = prune_history(root, keep=2, dry_run=True)
    assert preview["kept_runs"] == ["R3", "R2", "R0"]
    assert preview["removed_runs"] == ["R1"]
    assert preview["removed_builds"] == ["default/k_old", "default/k_orphan"]
    assert run_dir(root, "R1").exists()

    # Actual deletion
    res = prune_history(root, keep=2)
    assert res == preview
    assert not run_dir(root, "R1").exists()
    assert run_dir(root, "R0").exists()
    assert run_dir(root, "R2").exists()
    assert run_dir(root, "R3").exists()
    assert not (root / ".solouvm" / "build" / "default" / "k_old").exists()
    assert not (root / ".solouvm" / "build" / "default" / "k_orphan").exists()
    assert (root / ".solouvm" / "build" / "default" / "k_shared").exists()
    assert (root / ".solouvm" / "build" / "default" / "k_active").exists()
    # latest symlink updated because R1 was removed
    assert resolve_run_id(root, "latest") == "R3"


def test_prune_keeps_run_while_compiling_before_first_job(tmp_path: Path) -> None:
    now = datetime.now(timezone.utc)
    _write_plan_file(tmp_path, "compiling", now, builds={"default": "k_build"})
    lock = tmp_path / ".solouvm" / "build" / "default" / "k_build.lock"
    lock.mkdir(parents=True)

    assert prune_history(tmp_path, keep=0, dry_run=True)["kept_runs"] == ["compiling"]
    assert prune_history(tmp_path, keep=0)["removed_runs"] == []
    lock.rmdir()
    assert prune_history(tmp_path, keep=0)["removed_runs"] == ["compiling"]
