"""Tests for solouvm.pipeline (task 17 and M2/M4 execution controls)."""

from __future__ import annotations

import stat
import textwrap
import threading
import time
from pathlib import Path

import pytest

from solouvm.collect import collect_cases, collect_files, load_filelist
from solouvm.config import load_config
from solouvm.errors import BuildError, ConfigError, InfraError
from solouvm.history import resolve_run_id, run_dir
from solouvm.init import init_project
from solouvm.pipeline import (
    _launch_pid_alive,
    build,
    clean_project,
    gen,
    make_run_id,
    read_job_log,
    regress,
    rerun,
    run_regression,
    status_run,
    stop_run,
)
from solouvm.plan import build_plan
from solouvm.providers import LocalTransport, Provider, get_provider
from solouvm.report.json import read_results_json


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def _setup_project(root: Path, case_body: str | None = None, conf_extra: str = "") -> Path:
    init_project(root=root, simulator="fake", bare=False, agents=None)
    if case_body is not None:
        _write(root / "case.py", case_body)
    if conf_extra:
        _write(root / "solouvm.conf.py", conf_extra)
    return root


def _make_plan(root: Path, run_id: str, *, case_patterns: list[str] | None = None):
    cfg = load_config(root)
    cases = collect_cases(cfg)
    files = collect_files(root, load_filelist(root))
    plan = build_plan(
        cases=cases,
        config=cfg,
        selection={"case": list(case_patterns or ()), "seeds": "1"},
        run_id=run_id,
    )
    return cfg, files, plan, get_provider(cfg)


def test_gen_creates_full_artifact_tree_and_preserves_existing_root_makefile(
    tmp_path: Path,
) -> None:
    root = _setup_project(tmp_path / "proj")
    (root / "Makefile").write_text("# custom user makefile\n", encoding="utf-8")

    cfg, files, plan1, provider = _make_plan(root, "run-gen-1")
    out1 = gen(config=cfg, plan=plan1, files=files, provider=provider)
    r1 = run_dir(root, "run-gen-1")

    for expected in (
        root / ".solouvm" / "gen" / "bin" / "run_job.sh",
        root / ".solouvm" / "gen" / "bin" / "launch.sh",
        root / ".solouvm" / "gen" / "bin" / "fake_sim.sh",
        root / ".solouvm" / "gen" / "filelist.f",
        root / ".solouvm" / "Makefile",
        r1 / "run.mk",
        r1 / "plan.json",
        r1 / "env.json",
    ):
        assert expected.exists(), str(expected)

    mode_bits = stat.S_IMODE((root / ".solouvm" / "gen" / "bin" / "run_job.sh").stat().st_mode)
    assert mode_bits == 0o755
    assert (root / "Makefile").read_text(encoding="utf-8") == "# custom user makefile\n"

    # Second gen with same inputs is deterministic in build key and run.mk
    _, _, plan2, _ = _make_plan(root, "run-gen-1")
    out2 = gen(config=cfg, plan=plan2, files=files, provider=provider)
    assert out1["builds"] == out2["builds"]
    assert (r1 / "run.mk").read_text(encoding="utf-8") == (
        run_dir(root, "run-gen-1") / "run.mk"
    ).read_text(encoding="utf-8")

    # Without pre-existing root Makefile, one is created
    root2 = tmp_path / "proj2"
    _setup_project(root2)
    (root2 / "Makefile").unlink()
    cfg2, files2, plan3, prov2 = _make_plan(root2, "run-gen-2")
    gen(config=cfg2, plan=plan3, files=files2, provider=prov2)
    assert "include .solouvm/Makefile" in (root2 / "Makefile").read_text(encoding="utf-8")


def test_build_reuses_ok_and_rebuild_forces_new_dir_and_fail_reports_line(tmp_path: Path) -> None:
    root = _setup_project(tmp_path / "proj")
    marker = root / "compile_runs.log"
    comp_script = root / "custom_build.sh"
    _write(
        comp_script,
        f"""\
        #!/usr/bin/env bash
        echo "compile" >> {str(marker)!r}
        echo "fake compiler ok"
        touch simv
        """,
    )
    comp_script.chmod(0o755)
    _write(
        root / "solouvm.conf.py",
        f"""\
        CFG = {{
            "simulator": "fake",
            "top": "tb_top",
            "sim": {{"build": "bash {comp_script}"}},
        }}
        """,
    )

    cfg, files, plan1, prov = _make_plan(root, "r-b1")
    res1 = build(config=cfg, plan=plan1, provider=prov, files=files)
    assert res1["ok"] is True
    key1 = plan1.builds["default"]
    assert len(marker.read_text().splitlines()) == 1

    # Second build with identical build key skips compilation
    cfg2, files2, plan2, prov2 = _make_plan(root, "r-b2")
    build(config=cfg2, plan=plan2, provider=prov2, files=files2)
    assert plan2.builds["default"] == key1
    assert len(marker.read_text().splitlines()) == 1

    # --rebuild creates a different build directory and compiles again
    cfg3, files3, plan3, prov3 = _make_plan(root, "r-b3")
    build(config=cfg3, plan=plan3, provider=prov3, files=files3, rebuild=True)
    assert plan3.builds["default"] != key1
    assert len(marker.read_text().splitlines()) == 2

    # Failing build exits cleanly as BuildError(BUILD_FAILED) with first error context
    _write(
        comp_script,
        """\
        #!/usr/bin/env bash
        echo "line1 ok"
        echo "FAKE-ERROR: bad syntax in dut.sv"
        exit 2
        """,
    )
    comp_script.chmod(0o755)
    cfg4, files4, plan4, prov4 = _make_plan(root, "r-b4")
    with pytest.raises(BuildError) as exc:
        build(config=cfg4, plan=plan4, provider=prov4, files=files4, rebuild=True)
    assert exc.value.code == "BUILD_FAILED"
    assert exc.value.line == 2
    assert "FAKE-ERROR" in exc.value.message


def test_regress_mixed_build_failure_runs_healthy_mode_and_exits_2(tmp_path: Path) -> None:
    root = _setup_project(
        tmp_path / "proj",
        case_body="""\
        from solouvm import Case, Expect

        CASES = [
            Case(
                name="good_case",
                build="good",
                opts=["+SOLOUVM_FAKE_SCB=AXI_SCB:5"],
                expect=[Expect(scoreboard="AXI_SCB", min=1)],
            ),
            Case(
                name="bad_case",
                build="broken",
                expect=[Expect(sim_time_min="1us")],
            ),
        ]
        """,
        conf_extra="""\
        CFG = {
            "simulator": "fake",
            "top": "tb_top",
            "builds": {
                "good": {},
                "broken": {"compile_opts": ["+SOLOUVM_FAKE_MODE=unknown_fail"]},
            },
        }
        """,
    )
    cfg, files, plan, prov = _make_plan(root, "r-mix")
    results, payload, code = run_regression(
        config=cfg, plan=plan, provider=prov, files=files, jobs=2
    )
    assert code == 2
    assert payload is not None
    by_case = {r.case: r for r in results}
    assert by_case["good_case"].status == "pass"
    assert by_case["bad_case"].status == "error"
    assert "build" in by_case["bad_case"].reason
    assert (run_dir(root, "r-mix") / "launch.pid").is_file()
    pid_parts = (run_dir(root, "r-mix") / "launch.pid").read_text().split()
    assert len(pid_parts) == 2 and pid_parts[1].isdigit()


def test_regress_pass_fail_dry_run_detach_and_stop(tmp_path: Path) -> None:
    root = _setup_project(tmp_path / "proj")
    cfg, files, plan, prov = _make_plan(root, "r-dry")
    results, dry_payload, code = run_regression(
        config=cfg, plan=plan, provider=prov, files=files, dry_run=True
    )
    assert code == 0 and results == []
    assert dry_payload is not None and dry_payload["dry_run"] is True
    assert not list((root / ".solouvm" / "build").rglob(".ok"))
    assert not (run_dir(root, "r-dry") / "results.json").exists()

    # Pass regress
    cfg, files, plan, prov = _make_plan(root, "r-ok")
    code = regress(config=cfg, plan=plan, provider=prov, files=files)
    assert code == 0
    assert (run_dir(root, "r-ok") / "results.json").is_file()

    # Detach hang case -> status shows active -> stop kills cleanly -> partial results
    _write(
        root / "case.py",
        """\
        from solouvm import Case

        CASES = [Case(name="sleeper", opts=["+SOLOUVM_FAKE_MODE=hang"])]
        """,
    )
    cfg_h, files_h, plan_h, prov_h = _make_plan(root, "r-det")
    code_det = regress(config=cfg_h, plan=plan_h, provider=prov_h, files=files_h, detach=True)
    assert code_det == 0

    time.sleep(0.6)
    snap = status_run(config=cfg_h, provider=prov_h, run_id="r-det")
    assert snap["active"] is True

    stopped = stop_run(config=cfg_h, provider=prov_h, run_id="r-det")
    assert stopped["stopped"] is True
    assert stopped["judged"] is True
    res = read_results_json(run_dir(root, "r-det") / "results.json")
    assert res["partial"] is True
    assert res["jobs"][0]["status"] == "incomplete"


def test_keyboard_interrupt_stops_b_and_returns_130(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _setup_project(
        tmp_path / "proj",
        case_body="""\
        from solouvm import Case

        CASES = [Case(name="hang_sig", opts=["+SOLOUVM_FAKE_MODE=hang"])]
        """,
    )
    cfg, files, plan, prov = _make_plan(root, "r-int")

    orig_sleep = time.sleep
    calls = {"n": 0}

    def interrupt_on_poll(secs: float) -> None:
        calls["n"] += 1
        orig_sleep(secs)
        if calls["n"] >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr("solouvm.pipeline.time.sleep", interrupt_on_poll)
    results, payload, code = run_regression(
        config=cfg, plan=plan, provider=prov, files=files, jobs=1
    )
    assert code == 130
    assert payload is not None and payload["partial"] is True
    assert results[0].status == "incomplete"


def test_retry_wave_on_fail_and_rerun_and_log_and_clean(tmp_path: Path) -> None:
    root = _setup_project(tmp_path / "proj")
    flaky_flag = root / "flaky_once.flag"
    # Script fails on first run (license -> error) and passes on try2!
    flaky_sim = root / "flaky_sim.sh"
    _write(
        flaky_sim,
        f"""\
        #!/usr/bin/env bash
        if [ ! -f {str(flaky_flag)!r} ]; then
            touch {str(flaky_flag)!r}
            echo "Failed to obtain license for fake_sim"
            exit 1
        fi
        echo 'SOLOUVM_DONE status=pass'
        """,
    )
    flaky_sim.chmod(0o755)
    _write(
        root / "solouvm.conf.py",
        f"""\
        CFG = {{
            "simulator": "fake",
            "top": "tb_top",
            "regress": {{"retry_on": ["error"], "wave_on_fail": "per_signature"}},
            "sim": {{"run": "bash {flaky_sim}"}},
        }}
        """,
    )
    _write(
        root / "case.py",
        """\
        from solouvm import Case, Bug

        CASES = [
            Case(
                name="flaky_c",
                retry=1,
                notes=[
                    Bug(id=1, title="known seed bug", status="open", found="2026-11-18", seed=99)
                ],
            ),
        ]
        """,
    )
    cfg, files, plan, prov = _make_plan(root, "r-ret")
    results, _payload, code = run_regression(
        config=cfg, plan=plan, provider=prov, files=files, jobs=1
    )
    assert code == 0
    assert results[0].flaky is True
    assert len(results[0].attempts) == 2

    # Test wave_on_fail with standard uvm_error case
    _write(
        root / "solouvm.conf.py",
        """\
        CFG = {
            "simulator": "fake",
            "top": "tb_top",
            "regress": {"wave_on_fail": "per_signature"},
        }
        """,
    )
    _write(
        root / "case.py",
        """\
        from solouvm import Case, Bug

        CASES = [
            Case(
                name="bad_c",
                opts=["+SOLOUVM_FAKE_MODE=uvm_error"],
                notes=[Bug(id=1, title="bug", status="open", found="2026-11-18", seed=42)],
            ),
        ]
        """,
    )
    cfg_w, files_w, plan_w, prov_w = _make_plan(root, "r-wave")
    res_w, _, code_w = run_regression(config=cfg_w, plan=plan_w, provider=prov_w, files=files_w)
    assert code_w == 1
    assert len(res_w[0].attempts) == 2
    assert res_w[0].attempts[1]["wave"] is True
    assert res_w[0].status == "fail"

    # Log reading with grep & lines & build
    log_res = read_job_log(
        config=cfg_w, provider=prov_w, job_id="bad_c__1", run_id="r-wave", grep="UVM_ERROR"
    )
    assert log_res["total"] >= 1
    assert "UVM_ERROR" in log_res["lines"][0]["text"]
    build_log = read_job_log(config=cfg_w, provider=prov_w, run_id="r-wave", build_mode="default")
    assert "build" in build_log["path"]

    # Rerun --failed preserves seed=1
    re_plan, re_res, _, re_code = rerun(config=cfg_w, provider=prov_w, run_id="r-wave", failed=True)
    assert re_code == 1
    assert re_plan.parent_run == "r-wave"
    assert re_res[0].seed == 1

    # Rerun --note bad_c#1 uses seed=42
    note_plan, note_res, _, _ = rerun(config=cfg_w, provider=prov_w, note_ref="bad_c#1")
    assert note_res[0].seed == 42

    # Clean rejects --run latest
    with pytest.raises(ConfigError) as exc_clean:
        clean_project(config=cfg_w, provider=prov_w, run_id="latest")
    assert exc_clean.value.code == "CFG_INVALID"

    # Clean removes one run and resolves latest cleanly
    cleaned = clean_project(config=cfg_w, provider=prov_w, run_id="r-ret")
    assert ".solouvm/runs/r-ret" in cleaned["removed"]
    assert resolve_run_id(root, "latest") == note_plan.run_id


def test_make_run_id_honors_env_var(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOLOUVM_RUN_ID", "ci-build-123")
    assert make_run_id(tmp_path) == "ci-build-123"
    monkeypatch.setenv("SOLOUVM_RUN_ID", "latest")
    with pytest.raises(ConfigError):
        make_run_id(tmp_path)


def test_reusing_solouvm_run_id_or_clobbering_artifacts_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _setup_project(tmp_path / "proj")
    monkeypatch.setenv("SOLOUVM_RUN_ID", "fixed-id")

    cfg, files, plan, prov = _make_plan(root, make_run_id(root))
    code = regress(config=cfg, plan=plan, provider=prov, files=files)
    assert code == 0

    with pytest.raises(ConfigError) as exc_reuse:
        make_run_id(root)
    assert exc_reuse.value.code == "CFG_INVALID"

    with pytest.raises(ConfigError) as exc_rerun:
        rerun(config=cfg, provider=prov, run_id="fixed-id", failed=False)
    assert exc_rerun.value.code == "CFG_INVALID"

    # Direct gen() with an existing run_id that already has execution artifacts must fail
    _, _, plan_dup, _ = _make_plan(root, "fixed-id")
    with pytest.raises(ConfigError) as exc_gen:
        gen(config=cfg, plan=plan_dup, files=files, provider=prov)
    assert exc_gen.value.code == "CFG_INVALID"

    # Explicit allow_overwrite=True succeeds
    out = gen(config=cfg, plan=plan_dup, files=files, provider=prov, allow_overwrite=True)
    assert out["run_id"] == "fixed-id"


def test_gen_uses_passed_provider_setup_for_build_key(tmp_path: Path) -> None:
    root = _setup_project(
        tmp_path / "proj",
        conf_extra="""\
        CFG = {
            "simulator": "fake",
            "top": "tb_top",
            "provider": "local",
            "providers": {
                "local": {},
                "alt": {"setup": "export SOLOUVM_ALT_ENV=1"},
            },
        }
        """,
    )
    cfg = load_config(root)
    cases = collect_cases(cfg)
    files = collect_files(root, load_filelist(root))

    plan_local = build_plan(cases=cases, config=cfg, selection={"seeds": "1"}, run_id="r-local")
    res_local = gen(
        config=cfg,
        plan=plan_local,
        files=files,
        provider=get_provider(cfg, "local"),
    )

    plan_alt = build_plan(cases=cases, config=cfg, selection={"seeds": "1"}, run_id="r-alt")
    res_alt = gen(
        config=cfg,
        plan=plan_alt,
        files=files,
        provider=get_provider(cfg, "alt"),
    )

    assert res_local["builds"] != res_alt["builds"]
    assert plan_alt.provider == "alt"
    run_mk_alt = (run_dir(root, "r-alt") / "run.mk").read_text(encoding="utf-8")
    assert "--setup 'export SOLOUVM_ALT_ENV=1'" in run_mk_alt


def test_concurrent_make_run_id_reserve_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _setup_project(tmp_path / "proj")
    monkeypatch.setenv("SOLOUVM_RUN_ID", "race-id")

    n_threads = 6
    barrier = threading.Barrier(n_threads)
    winners: list[str] = []
    errors: list[ConfigError] = []
    lock = threading.Lock()

    def worker() -> None:
        barrier.wait()
        try:
            rid = make_run_id(root, reserve=True)
            with lock:
                winners.append(rid)
        except ConfigError as exc:
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert winners == ["race-id"]
    assert len(errors) == n_threads - 1
    assert all(e.code == "CFG_INVALID" for e in errors)

    # Pure reserved dir has no artifacts yet, so gen() succeeds on it
    cfg, files, plan, prov = _make_plan(root, "race-id")
    out = gen(config=cfg, plan=plan, files=files, provider=prov)
    assert out["run_id"] == "race-id"


def test_regress_budget_exceeded_during_slow_build_is_incomplete(tmp_path: Path) -> None:
    root = _setup_project(
        tmp_path / "proj",
        case_body='from solouvm import Case\nCASES = [Case(name="smoke", expect=[])]\n',
        conf_extra="""\
        CFG = {
            "simulator": "fake",
            "top": "tb_top",
            "sim": {"build": "sleep 5"},
        }
        """,
    )
    cfg, files, plan, prov = _make_plan(root, "r-slow-build")
    results, payload, code = run_regression(
        config=cfg,
        plan=plan,
        provider=prov,
        files=files,
        budget_s=0.1,
    )
    assert code == 1
    assert payload is not None
    assert payload["partial"] is True
    assert results[0].status == "incomplete"


def test_regress_budget_shared_across_retry_and_wave_on_fail(tmp_path: Path) -> None:
    root = _setup_project(tmp_path / "proj")
    sim_sh = root / "slow_fail.sh"
    _write(
        sim_sh,
        """\
        #!/usr/bin/env bash
        sleep 0.25
        echo "Failed to obtain license for fake_sim"
        exit 1
        """,
    )
    sim_sh.chmod(0o755)
    _write(
        root / "solouvm.conf.py",
        f"""\
        CFG = {{
            "simulator": "fake",
            "top": "tb_top",
            "regress": {{"retry_on": ["error"], "wave_on_fail": "per_signature"}},
            "sim": {{"run": "bash {sim_sh}"}},
        }}
        """,
    )
    _write(
        root / "case.py",
        'from solouvm import Case\nCASES = [Case(name="c1", retry=1, expect=[])]\n',
    )
    cfg, files, plan, prov = _make_plan(root, "r-budget-retry")
    # Build first so budget tests retry + wave phases rather than build
    build(config=cfg, plan=plan, provider=prov, files=files)
    # Give enough budget for try1 (~0.25s) + retry launch (~0.1s), cut off retry/wave!
    results, payload, code = run_regression(
        config=cfg,
        plan=plan,
        provider=prov,
        files=files,
        budget_s=0.35,
    )
    assert code == 1
    assert payload is not None and payload["partial"] is True
    # Must NOT have run a third wave attempt on top of try1/try2 within 0.35s
    assert not any(att.get("wave") for att in results[0].attempts)


def test_regress_detach_respects_budget_s(tmp_path: Path) -> None:
    root = _setup_project(
        tmp_path / "proj",
        case_body="""\
        from solouvm import Case
        CASES = [Case(name="sleeper", opts=["+SOLOUVM_FAKE_MODE=hang"], expect=[])]
        """,
    )
    cfg, files, plan, prov = _make_plan(root, "r-det-budget")
    _, payload, code = run_regression(
        config=cfg,
        plan=plan,
        provider=prov,
        files=files,
        detach=True,
        budget_s=0.5,
    )
    assert code == 0 and payload is None

    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline and _launch_pid_alive(prov, "r-det-budget"):
        time.sleep(0.1)
    assert not _launch_pid_alive(prov, "r-det-budget")


def test_max_fail_counts_zero_exit_uvm_error(tmp_path: Path) -> None:
    root = _setup_project(tmp_path / "proj")
    marker_dir = root / "started"
    marker_dir.mkdir()
    sim_sh = root / "uvm_sim.sh"
    _write(
        sim_sh,
        f"""\
        #!/usr/bin/env bash
        job_id=""
        for arg in "$@"; do
            case "$arg" in
                +SOLOUVM_JOB_ID=*) job_id="${{arg#+SOLOUVM_JOB_ID=}}" ;;
            esac
        done
        touch {str(marker_dir)!r}/"$job_id"
        if [ "$job_id" = "c1__1" ]; then
            echo 'UVM_ERROR verif/scb.sv(10) @ 10ns: uvm_test_top [AXI_SCB] mismatch'
            echo 'UVM_INFO verif/t.sv(20) @ 20ns: reporter [TEST_DONE] done'
            echo '--- UVM Report Summary ---'
            echo '** Report counts by severity'
            echo 'UVM_INFO : 1'
            echo 'UVM_WARNING : 0'
            echo 'UVM_ERROR : 1'
            echo 'UVM_FATAL : 0'
            exit 0
        fi
        sleep 0.6
        echo 'SOLOUVM_DONE status=pass'
        exit 0
        """,
    )
    sim_sh.chmod(0o755)
    _write(
        root / "solouvm.conf.py",
        f"""\
        CFG = {{
            "simulator": "fake",
            "top": "tb_top",
            "sim": {{"run": "bash {sim_sh}"}},
        }}
        """,
    )
    _write(
        root / "case.py",
        """\
        from solouvm import Case
        CASES = [
            Case(name="c1", expect=[]),
            Case(name="c2", expect=[]),
            Case(name="c3", expect=[]),
        ]
        """,
    )
    cfg, files, plan, prov = _make_plan(root, "r-mf-uvm")
    build(config=cfg, plan=plan, provider=prov, files=files)
    results, payload, code = run_regression(
        config=cfg,
        plan=plan,
        provider=prov,
        files=files,
        jobs=1,
        max_fail=1,
    )
    assert code == 1
    assert payload is not None and payload["partial"] is True
    by_case = {r.case: r for r in results}
    assert by_case["c1"].status == "fail"
    assert not (marker_dir / "c3__1").exists()


def test_polling_read_many_caps_timeout_to_budget_and_handles_timeout(tmp_path: Path) -> None:
    root = _setup_project(
        tmp_path / "proj",
        case_body="""\
        from solouvm import Case
        CASES = [Case(name="sleeper", opts=["+SOLOUVM_FAKE_MODE=hang"], expect=[])]
        """,
    )
    recorded_timeouts: list[float | None] = []

    class SlowPollTransport(LocalTransport):
        op_timeout: float = 5.0

        def read_many(self, pattern: str, *, timeout: float | None = None) -> dict[str, str]:
            if timeout is not None:
                recorded_timeouts.append(timeout)
                if len(recorded_timeouts) >= 2:
                    time.sleep(0.35)
                    raise InfraError("poll timed out", code="PROVIDER_FAILED")
            return super().read_many(pattern)

    cfg, files, plan, prov = _make_plan(root, "r-poll-to")
    wrapped_prov = Provider(
        name=prov.name,
        transport=SlowPollTransport(root=root),
        launcher=prov.launcher,
        sync=prov.sync,
        setup=prov.setup,
    )
    build(config=cfg, plan=plan, provider=wrapped_prov, files=files)
    results, payload, code = run_regression(
        config=cfg,
        plan=plan,
        provider=wrapped_prov,
        files=files,
        budget_s=0.3,
    )
    assert code == 1
    assert payload is not None and payload["partial"] is True
    assert results[0].status == "incomplete"
    assert any(t is not None and t <= 0.3 for t in recorded_timeouts)
