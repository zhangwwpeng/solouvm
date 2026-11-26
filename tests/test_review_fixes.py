"""Regression tests for full-repository review fixes (P0/P1/P2, architecture, UX)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from solouvm.backends import get_backend
from solouvm.buildkey import compute_build_keys, strip_sv_comments
from solouvm.check.slang import parse_slang_diagnostics
from solouvm.cli import main
from solouvm.collect import collect_cases, collect_files, load_filelist
from solouvm.config import load_config
from solouvm.cov import cov_merge, cov_report
from solouvm.errors import ConfigError, RunActiveError
from solouvm.gen.makefile import render_run_mk
from solouvm.history import run_dir
from solouvm.judge import judge_run, write_results
from solouvm.model import Expect, Job, Plan
from solouvm.notes import bug_add, bug_update, query_notes
from solouvm.pipeline import clean_project, read_job_log, run_regression
from solouvm.plan import build_plan
from solouvm.providers import MakeLauncher, NoneSync, Provider, get_provider
from solouvm.providers.launcher_lsf import LsfLauncher
from solouvm.providers.transport_local import LocalTransport
from solouvm.report.json import compare_results, read_results_json
from solouvm.uvmgen import Env, create_component, render_component
from solouvm.wave import query_wave


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def test_p0_2_budget_early_stop_marks_unstarted_incomplete_and_exits_nonzero(
    tmp_path: Path,
) -> None:
    root = tmp_path / "proj"
    _write(
        root / "solouvm.conf.py",
        """
        CFG = {
            "simulator": "fake",
            "top": "tb_top",
            "regress": {"jobs": 1},
        }
        """,
    )
    _write(root / "filelist.py", 'FILES = {"rtl": ["dut.sv"], "tb": [], "top": []}\n')
    _write(root / "dut.sv", "module tb_top; endmodule\n")
    _write(
        root / "case.py",
        """
        from solouvm import Case

        CASES = [
            Case(name="c1", seeds=[1]),
            Case(name="c2", seeds=[1], opts=["+SOLOUVM_FAKE_MODE=hang"]),
            Case(name="c3", seeds=[1]),
        ]
        """,
    )
    cfg = load_config(root)
    files = collect_files(root, load_filelist(root))
    plan = build_plan(cases=collect_cases(cfg), config=cfg, run_id="r-budget")
    prov = get_provider(cfg)

    results, payload, code = run_regression(
        config=cfg,
        plan=plan,
        provider=prov,
        files=files,
        jobs=1,
        budget_s=0.4,
    )
    assert code == 1
    assert payload is not None and payload["partial"] is True
    by_case = {r.case: r for r in results}
    assert by_case["c3"].status == "incomplete"
    assert "stopped early" in by_case["c3"].reason
    assert payload["summary"]["by_status"]["skip"] == 0


def test_p0_3_clean_project_prune_uses_full_solouvm_relative_paths(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write(
        root / "solouvm.conf.py",
        """
        CFG = {
            "simulator": "fake",
            "top": "tb_top",
            "history": {"keep": 1},
        }
        """,
    )
    _write(root / "case.py", "from solouvm import Case\nCASES = [Case(name='a')]\n")
    cfg = load_config(root)

    t0 = datetime(2026, 11, 18, 10, 0, tzinfo=timezone.utc)
    for idx, (rid, bkey) in enumerate([("R1", "k_old"), ("R2", "k_keep")]):
        rdir = run_dir(root, rid)
        rdir.mkdir(parents=True, exist_ok=True)
        (rdir / "plan.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "run_id": rid,
                    "created_at": (t0 + timedelta(hours=idx)).isoformat(),
                    "parent_run": None,
                    "provider": "remote",
                    "selection": {},
                    "builds": {"default": bkey},
                    "jobs": [],
                }
            ),
            encoding="utf-8",
        )
        bdir = root / ".solouvm" / "build" / "default" / bkey
        bdir.mkdir(parents=True, exist_ok=True)
        (bdir / ".ok").write_text("", encoding="utf-8")

    removed_remote: list[str] = []

    class DummyRemoteTransport:
        kind = "ssh"

        def exists(self, path: str) -> bool:
            return not path.endswith(".lock")

        def read_many(self, pattern: str) -> dict[str, str]:
            return {}

        def rm_tree(self, path: str) -> None:
            removed_remote.append(path)

    remote_prov = Provider(
        name="remote",
        transport=DummyRemoteTransport(),  # type: ignore[arg-type]
        launcher=MakeLauncher(),
        sync=NoneSync(),
    )
    clean_project(config=cfg, provider=remote_prov)
    assert ".solouvm/runs/R1" in removed_remote
    assert ".solouvm/build/default/k_old" in removed_remote


def test_p0_5_buildkey_tracks_sv_include_headers_outside_incdirs(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write(root / "solouvm.conf.py", 'CFG = {"simulator": "fake", "top": "tb_top"}\n')
    _write(root / "filelist.py", 'FILES = {"rtl": ["rtl/dut.sv"], "tb": [], "top": []}\n')
    _write(root / "rtl" / "defs.svh", "localparam int W = 8;\n")
    _write(
        root / "rtl" / "dut.sv",
        """
        module tb_top;
          initial $display("// fake comment in string"); `include "defs.svh"
        endmodule
        """,
    )
    cfg = load_config(root)
    files = collect_files(root, load_filelist(root))
    backend = get_backend("fake")

    k1 = compute_build_keys(
        config=cfg, files=files, backend=backend, modes=["default"], run_id="r1"
    )
    _write(root / "rtl" / "defs.svh", "localparam int W = 16;\n")
    k2 = compute_build_keys(
        config=cfg, files=files, backend=backend, modes=["default"], run_id="r2"
    )
    assert k1.keys["default"] != k2.keys["default"]
    assert not k1.non_reusable
    assert "`include" in strip_sv_comments('initial $display("// x"); `include "a.svh"')


def test_p1_6_slang_12_json_with_trailing_summary_parses_cleanly(tmp_path: Path) -> None:
    stdout_err = (
        '[\n  {\n    "severity": "error",\n    "message": "expected \';\'",\n'
        '    "location": "rtl/dut.sv:5:3"\n  }\n]\n\nBuild failed: 1 error, 0 warnings\n'
    )
    diags = parse_slang_diagnostics(stdout_err, "", tmp_path)
    assert len(diags) == 1
    assert diags[0].severity == "error"
    assert diags[0].file == "rtl/dut.sv"
    assert diags[0].line == 5
    assert diags[0].message == "expected ';'"

    stdout_uvm = (
        '[\n  {\n    "severity": "error",\n    "message": "unknown package \'uvm_pkg\'",\n'
        '    "location": "tb/top.sv:2:10"\n  }\n]\n\nBuild failed: 1 error, 0 warnings\n'
    )
    uvm_diags = parse_slang_diagnostics(stdout_uvm, "", tmp_path)
    assert uvm_diags[0].hint is not None and "UVM_HOME" in uvm_diags[0].hint


def test_p1_7_and_p1_8_lsf_bsub_preserves_spaces_and_questa_wave_no_duplicate_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    q_cfg_dir = tmp_path / "qproj"
    _write(q_cfg_dir / "solouvm.conf.py", 'CFG = {"simulator": "questa", "top": "tb_top"}\n')
    _write(q_cfg_dir / "filelist.py", 'FILES = {"rtl": ["dut.sv"], "tb": [], "top": []}\n')
    _write(q_cfg_dir / "dut.sv", "module tb_top; endmodule\n")
    _write(q_cfg_dir / "case.py", "from solouvm import Case\nCASES = [Case(name='smoke')]\n")
    q_cfg = load_config(q_cfg_dir)
    q_files = collect_files(q_cfg_dir, load_filelist(q_cfg_dir))
    q_plan = build_plan(
        cases=collect_cases(q_cfg), config=q_cfg, selection={"seeds": "1"}, run_id="rq"
    )
    q_mk = render_run_mk(
        plan=q_plan,
        config=q_cfg,
        files=q_files,
        build_keys={"default": "111122223333"},
        backend=get_backend("questa"),
    )
    job_recipe = q_mk.split("run/smoke__1:")[1]
    assert "solouvm_opt" in job_recipe
    assert "../../../../build/default/111122223333/solouvm_opt" not in job_recipe
    expected_wave_cond = (
        "$(if $(filter 1,$(WAVE)),"
        "-wlf ./wave.wlf -do 'log -r /*; run -all; quit -f',"
        "-do 'run -all; quit -f')"
    )
    assert expected_wave_cond in job_recipe

    if shutil.which("make") is None:
        return

    ws = tmp_path / "dir with spaces"
    _write(
        ws / "solouvm.conf.py",
        """
        CFG = {
            "simulator": "fake",
            "top": "tb_top",
            "provider": "cluster",
            "providers": {
                "cluster": {
                    "launcher": {"kind": "lsf", "queue": "regr"},
                    "setup": "export MY_SETUP='hello lsf world'",
                }
            },
        }
        """,
    )
    _write(ws / "filelist.py", 'FILES = {"rtl": ["dut.sv"], "tb": [], "top": []}\n')
    _write(ws / "dut.sv", "module tb_top; endmodule\n")
    _write(ws / "case.py", "from solouvm import Case\nCASES = [Case(name='smoke', seeds=[1])]\n")

    bin_dir = tmp_path / "lsf_bin"
    bin_dir.mkdir()
    bsub_stub = bin_dir / "bsub"
    bsub_stub.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do\n"
        '  case "$1" in\n'
        "    -K) shift ;;\n"
        "    -q|-R|-J|-P) shift 2 ;;\n"
        "    *) break ;;\n"
        "  esac\n"
        "done\n"
        'exec /bin/sh -c "$*"\n',
        encoding="utf-8",
    )
    bsub_stub.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    cfg = load_config(ws)
    prov = get_provider(cfg, "cluster")
    assert isinstance(prov.launcher, LsfLauncher)
    files = collect_files(ws, load_filelist(ws))
    plan = build_plan(cases=collect_cases(cfg), config=cfg, run_id="r-lsf")
    results, _, code = run_regression(config=cfg, plan=plan, provider=prov, files=files, jobs=1)
    assert code == 0
    assert results[0].status == "pass"


def test_p1_9_and_p1_10_extra_sim_keys_xezim_cov_and_dry_run_side_effect_free(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "proj"
    _write(
        root / "solouvm.conf.py",
        """
        CFG = {
            "simulator": "xezim",
            "top": "tb_top",
            "sim": {
                "wave_export": "my_vcd_export {wave} -o {out}",
            },
            "builds": {"default": {"coverage": True}},
        }
        """,
    )
    _write(root / "filelist.py", 'FILES = {"rtl": ["dut.sv"], "tb": [], "top": []}\n')
    _write(root / "dut.sv", "module tb_top; endmodule\n")
    _write(root / "case.py", "from solouvm import Case\nCASES = [Case(name='smoke', seeds=[1])]\n")

    assert main(["-c", str(root), "regress", "--dry-run"]) == 0
    _ = capsys.readouterr()
    assert not (root / ".solouvm").exists()
    assert not (root / "Makefile").exists()

    cfg = load_config(root)
    prov = get_provider(cfg)
    plan = build_plan(cases=collect_cases(cfg), config=cfg, run_id="r-xez")
    j1 = root / ".solouvm" / "runs" / "r-xez" / "smoke__1" / "try1"
    j1.mkdir(parents=True)
    (j1 / "xezim_cov.json").write_text(
        json.dumps({"lines": {"dut.sv:1": 2, "dut.sv:2": 0}, "branches": {"dut.sv:5": 1}}),
        encoding="utf-8",
    )
    res_data = {"schema": 1, "run_id": "r-xez", "jobs": [{"job_id": "smoke__1", "status": "pass"}]}
    merged = cov_merge(config=cfg, plan=plan, provider=prov, results_data=res_data)
    assert merged["tool"] == "xezim-json"
    rep = cov_report(config=cfg, plan=plan, provider=prov)
    assert rep["metrics"]["line"] == 50.0
    assert rep["metrics"]["branch"] == 100.0
    assert rep["metrics"]["total"] == 75.0


def test_judge_dead_local_pid_vs_live_pid_and_compare_random_seeds(tmp_path: Path) -> None:
    import socket

    root = tmp_path / "proj"
    _write(root / "solouvm.conf.py", 'CFG = {"simulator": "fake", "top": "tb_top"}\n')
    _write(root / "case.py", "from solouvm import Case\nCASES = [Case(name='smoke')]\n")
    cfg = load_config(root)
    prov = Provider(
        name="local", transport=LocalTransport(root=root), launcher=MakeLauncher(), sync=NoneSync()
    )

    bdir = root / ".solouvm" / "build" / "default" / "k1"
    bdir.mkdir(parents=True)
    (bdir / ".ok").write_text("", encoding="utf-8")

    job = Job(
        job_id="smoke__1",
        case="smoke",
        seed=1,
        build="default",
        uvm_test=None,
        params={},
        opts=[],
        env={},
        timeout_s=10.0,
        hang_s=None,
        retry=0,
        expect_fail=False,
        tags=(),
        dir=".solouvm/runs/r1/smoke__1",
    )
    plan = Plan(
        run_id="r1",
        created_at=datetime.now(timezone.utc),
        parent_run=None,
        provider="local",
        selection={},
        builds={"default": "k1"},
        jobs=[job],
    )
    facts_path = root / ".solouvm" / "runs" / "r1" / "smoke__1" / "try1" / "job.facts"
    facts_path.parent.mkdir(parents=True)
    local_host = socket.gethostname()

    # Live PID (current process) -> raises RunActiveError
    facts_path.write_text(
        f"schema=1\ngenerator=solouvm 0.1.0\nkind=job\nrun_id=r1\njob_id=smoke__1\n"
        f"try=1\nhost={local_host}\npid={os.getpid()}\ntimeout_impl=builtin\n"
        f"started_at=2026-11-18T10:00:00+00:00\nstate=running\n",
        encoding="utf-8",
    )
    with pytest.raises(RunActiveError):
        judge_run(config=cfg, plan=plan, provider=prov, partial=False)

    # Dead local PID -> judged as incomplete without getting stuck in RunActiveError
    facts_path.write_text(
        f"schema=1\ngenerator=solouvm 0.1.0\nkind=job\nrun_id=r1\njob_id=smoke__1\n"
        f"try=1\nhost={local_host}\npid=99999999\ntimeout_impl=builtin\n"
        f"started_at=2026-11-18T10:00:00+00:00\nstate=running\n",
        encoding="utf-8",
    )
    res = judge_run(config=cfg, plan=plan, provider=prov, partial=False)
    assert res[0].status == "incomplete"

    # Random-seed compare_results fallback by case name
    base = {
        "run_id": "base",
        "jobs": [
            {"job_id": "burst__101", "case": "burst", "seed": 101, "status": "fail"},
            {"job_id": "smoke__202", "case": "smoke", "seed": 202, "status": "pass"},
        ],
    }
    curr = {
        "run_id": "curr",
        "jobs": [
            {"job_id": "burst__909", "case": "burst", "seed": 909, "status": "pass"},
            {"job_id": "smoke__808", "case": "smoke", "seed": 808, "status": "fail"},
        ],
    }
    cmp_out = compare_results(curr, base)
    assert [x["case"] for x in cmp_out["newly_fixed"]] == ["burst"]  # type: ignore[index]
    assert [x["case"] for x in cmp_out["new_failures"]] == ["smoke"]  # type: ignore[index]


def test_wave_window_initial_snapshot_and_vcd_vector_extension(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write(root / "solouvm.conf.py", 'CFG = {"simulator": "fake", "top": "tb_top"}\n')
    cfg = load_config(root)
    prov = get_provider(cfg)
    vcd_path = root / ".solouvm" / "runs" / "r1" / "c__1" / "try1" / "wave.vcd"
    _write(
        vcd_path,
        """
        $timescale 1ns $end
        $scope module tb $end
        $var wire 1 ! rst_n $end
        $var reg 8 " data [7:0] $end
        $upscope $end
        $enddefinitions $end
        #0
        0!
        bx "
        #10
        1!
        bz "
        #30
        b101 "
        """,
    )
    r_vec = query_wave(config=cfg, run_id="r1", job_id="c__1", provider=prov, signals=["tb.data"])
    vals = [row["values"]["tb.data"] for row in r_vec["rows"]]  # type: ignore[index]
    assert vals == ["bxxxxxxxx", "bzzzzzzzz", "0x5"]

    # rst_n changed at 10ns and stayed 1 across [15ns, 25ns] with no ticks in window
    r_win = query_wave(
        config=cfg,
        run_id="r1",
        job_id="c__1",
        provider=prov,
        signals=["tb.rst_n"],
        time_from="15ns",
        time_to="25ns",
    )
    assert len(r_win["rows"]) == 1  # type: ignore[arg-type]
    assert r_win["rows"][0]["values"]["tb.rst_n"] == "1"  # type: ignore[index]


def test_uvmgen_symbolic_signal_width_and_shared_iface_merge(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    _write(
        root / "tbspec.py",
        """
        from solouvm.uvm import Agent, Env, Field, Interface, Signal

        BUS = Interface(
            name="bus",
            clock="clk",
            reset=("rst_n", "low"),
            params=["int ADDR_W = 32", "type T = logic [7:0]"],
            signals=[
                Signal("addr", "ADDR_W", "m2s"),
                Signal("wstrb", "ADDR_W/8", "m2s"),
                Signal("ready", 1, "s2m"),
            ],
        )
        AGENTS = [
            Agent(name="bus_mst", interface=BUS, role="master", fields=[Field("addr", 32)]),
        ]
        """,
    )
    create_component(root, "agent", "bus_mst")
    if_text = (root / "verif" / "agents" / "bus" / "bus_if.sv").read_text(encoding="utf-8")
    assert "parameter int ADDR_W = 32" in if_text
    assert "parameter type T = logic [7:0]" in if_text
    assert "logic [ADDR_W-1:0] addr;" in if_text
    assert "logic [(ADDR_W/8)-1:0] wstrb;" in if_text

    # Add slave agent on same interface -> merges slv_drv_cb into bus_if.sv instead of UVMGEN_EXISTS
    _write(
        root / "tbspec.py",
        """
        from solouvm.uvm import Agent, Interface, Signal

        BUS = Interface(
            name="bus",
            clock="clk",
            reset=("rst_n", "low"),
            signals=[Signal("addr", 32, "m2s"), Signal("ready", 1, "s2m")],
        )
        AGENTS = [
            Agent(name="bus_mst", interface=BUS, role="master"),
            Agent(name="bus_slv", interface=BUS, role="slave"),
        ]
        """,
    )
    create_component(root, "agent", "bus_slv")
    if_merged = (root / "verif" / "agents" / "bus" / "bus_if.sv").read_text(encoding="utf-8")
    assert "slv_drv_cb" in if_merged
    _ = Env, render_component


def test_positional_case_bug_reopen_notes_triage_expect_and_json_cli_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Expect orphan field checks
    with pytest.raises(ConfigError):
        Expect(sim_time_min="10ns", id="ORPHAN_ID")
    with pytest.raises(ConfigError):
        Expect(scoreboard="SCB", min=1, count_min=2)
    with pytest.raises(ConfigError):
        Expect(scoreboard="SCB", min=10, max=5)

    # Positional Case("smoke") + bug_add / bug_update reopen + notes query
    root = tmp_path / "proj"
    _write(root / "solouvm.conf.py", 'CFG = {"simulator": "fake", "top": "tb_top"}\n')
    _write(
        root / "case.py",
        """
        from solouvm import Case, Triage

        CASES = [
            Case(
                "smoke",
                notes=[
                    Triage(
                        task="r1/001",
                        verdict="tb_bug",
                        confidence="high",
                        summary="tb reset bug",
                        evidence=["log:10"],
                        judged_status={"smoke__1": "fail"},
                    )
                ],
            )
        ]
        """,
    )
    cfg = load_config(root)
    added = bug_add(config=cfg, case_name="smoke", title="reset glitch", seed=7)
    assert added.id == 1
    verified = bug_update(config=cfg, ref="smoke#1", status="verified", owner="alice")
    assert verified.fixed is not None and verified.verified is not None
    reopened = bug_update(config=cfg, ref="smoke#1", status="open", owner="")
    assert reopened.fixed is None and reopened.verified is None and reopened.owner is None

    tri_q = query_notes(config=cfg, filters={"types": ["triage"]})
    assert tri_q["total"] == 1
    unassigned_q = query_notes(config=cfg, filters={"owner": "unassigned"})
    assert unassigned_q["total"] == 1

    # Invalid --grep regex raises ConfigError instead of re.error
    rdir = run_dir(root, "r1")
    (rdir / "smoke__1" / "try1").mkdir(parents=True)
    (rdir / "smoke__1" / "try1" / "sim.log").write_text("hello\n", encoding="utf-8")
    (rdir / "plan.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "run_id": "r1",
                "created_at": "2026-11-18T10:00:00+00:00",
                "parent_run": None,
                "provider": "local",
                "selection": {},
                "builds": {"default": "k1"},
                "jobs": [
                    {
                        "job_id": "smoke__1",
                        "case": "smoke",
                        "seed": 1,
                        "build": "default",
                        "dir": ".solouvm/runs/r1/smoke__1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        read_job_log(
            config=cfg, provider=get_provider(cfg), job_id="smoke__1", run_id="r1", grep="[unclosed"
        )

    # --json with bad CLI flag outputs valid JSON envelope with exit code 2
    rc_bad = main(["--json", "bogus_cmd"])
    assert rc_bad == 2
    err_env = json.loads(capsys.readouterr().out)
    assert err_env["ok"] is False and err_env["error"]["code"] == "CFG_INVALID"

    # post:main hook failure in --json mode emits ONLY one error envelope (no concatenated JSON)
    _write(root / "post_hook_main.py", "def hook(ctx):\n    raise RuntimeError('post boom')\n")
    rc_hook = main(["--json", "-c", str(root), "list"])
    assert rc_hook == 2
    hook_env = json.loads(capsys.readouterr().out)
    assert hook_env["ok"] is False and hook_env["error"]["code"] == "HOOK_FAILED"
    _ = write_results, read_results_json, subprocess


def test_questa_live_transcript_scan_and_makefile_cov_wave(tmp_path: Path) -> None:
    """QuestaSim 2024 '# ' transcript prefix, 2-line $finish Time, +acc, and -do cov_run."""
    from solouvm.judge.scan import scan
    from solouvm.model import Checks

    backend = get_backend("questa")
    lines = textwrap.dedent(
        """\
        # vsim -c -lib work solouvm_opt +UVM_TESTNAME=smoke_test
        # UVM_INFO @ 0: reporter [RNTST] Running test smoke_test...
        # UVM_INFO tb/tb_top.sv(12) @ 50000: uvm_test_top [adder_scb] compared=4 errors=0
        # UVM_INFO tb/tb_top.sv(13) @ 50000: uvm_test_top [TEST] SOLOUVM_DONE
        # --- UVM Report Summary ---
        # ** Report counts by severity
        # UVM_INFO :    6
        # UVM_WARNING :    0
        # UVM_ERROR :    0
        # UVM_FATAL :    0
        # ** Note: $finish    : /opt/uvm/src/base/uvm_root.svh(527)
        #    Time: 50 ns  Iteration: 54  Instance: /tb_top
        """
    ).splitlines()
    res = scan(lines, backend=backend, checks=Checks())
    assert res.uvm_counts == {"INFO": 6, "WARNING": 0, "ERROR": 0, "FATAL": 0}
    assert res.scoreboard["adder_scb"] == 4
    assert res.sim_end_ns == pytest.approx(50.0)
    assert res.is_uvm is True
    assert res.sim_errors == []

    # Also verify '# ** Error:' is detected as a simulator error
    err_res = scan(
        ["# ** Error: (vsim-3033) Instantiation of 'foo' failed."],
        backend=backend,
        checks=Checks(),
    )
    assert len(err_res.sim_errors) == 1

    # Verify Makefile generation for Questa with coverage=True preserves both +acc,
    # cov_run's -do 'coverage save -onexit ...', and conditional WAVE -do
    root = tmp_path / "qproj"
    _write(
        root / "solouvm.conf.py",
        """
        CFG = {
            "simulator": "questa",
            "top": "tb_top",
            "builds": {"cov": {"coverage": True}},
        }
        """,
    )
    _write(root / "filelist.py", 'FILES = {"rtl": ["dut.sv"], "tb": [], "top": []}\n')
    _write(root / "dut.sv", "module tb_top; endmodule\n")
    _write(root / "case.py", 'from solouvm import Case\nCASES = [Case(name="c1", build="cov")]\n')
    cfg = load_config(root)
    files = collect_files(root, load_filelist(root))
    plan = build_plan(cases=collect_cases(cfg), config=cfg, run_id="r-q")
    mk = render_run_mk(
        config=cfg, plan=plan, backend=backend, files=files, build_keys={"cov": "abc123"}
    )
    assert "qrun -64 -sv -optimize +acc" in mk
    assert "qrun -64 -c -simulate -outdir" in mk
    assert "-coverage -do 'coverage save -onexit ./cov.ucdb'" in mk
    assert "-wlf ./wave.wlf -do 'log -r /*; run -all; quit -f'" in mk
    assert "-covdb" not in mk


def test_detect_sim_version_ssh_single_line_and_remote_cwd(tmp_path: Path) -> None:
    """detect_sim_version must not emit newlines in ssh command or pass local root as cwd."""
    from solouvm.history import detect_sim_version

    captured: list[tuple[list[str], str | None]] = []

    class _FakeSshTransport:
        # Mimics SshTransport having kind="ssh" and a local .root attribute
        kind = "ssh"
        root = tmp_path

        def exec_output(
            self, cmd: list[str], *, cwd: str | None = None, check: bool = True
        ) -> tuple[int, str]:
            captured.append((cmd, cwd))
            assert all("\n" not in part for part in cmd)
            assert cwd == "."
            return 0, "QuestaSim-64 vsim 2024.1 Simulator\n"

    prov = Provider(
        name="ubuntu",
        transport=_FakeSshTransport(),  # type: ignore[arg-type]
        launcher=MakeLauncher(),
        sync=NoneSync(),
        setup='export PATH="/opt/questa/bin:$PATH" && export LM_LICENSE_FILE="/opt/lic.dat"',
    )
    ver = detect_sim_version(get_backend("questa"), prov, root=tmp_path)
    assert ver == "QuestaSim-64 vsim 2024.1 Simulator"
    assert len(captured) == 1
