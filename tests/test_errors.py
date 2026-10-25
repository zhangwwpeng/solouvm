"""Branches that the happy-path tests never reach."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from solouvm import simulator
from solouvm import status as status_mod
from solouvm.cli import main
from solouvm.config import load_config
from solouvm.errors import ConfigError
from solouvm.model import Case
from solouvm.report import render_text


def test_build_command(make_project, capfd):
    project = make_project()
    assert project.run("build") == 0
    assert "build-ok" in capfd.readouterr().out


def test_launch_failure_is_an_error_status(make_project):
    project = make_project()
    project.root.joinpath("solouvm.conf.py").write_text(
        "CFG = {"
        "'project': 'demo', 'simulator': 'fake',"
        " 'regress': {'seeds': [1], 'jobs': 1}, 'run': {'timeout': '10s'},"
        " 'checks': {'max_uvm_error': 0},"
        " 'sim': {'build': 'echo build-ok',"
        " 'run': ['definitely-not-a-real-binary-xyz']}"
        "}\n"
    )

    assert project.run("regress") == 1
    status = project.status("smoke__1")
    assert status["status"] == "error"
    assert "definitely-not-a-real-binary-xyz" in status["reason"]


def test_malformed_status_file_is_an_error(make_project):
    project = make_project()
    assert project.run("gen") == 0
    outdir = project.workdir / "out" / "smoke__1"
    outdir.mkdir(parents=True)
    (outdir / "status.json").write_text("{not json")
    assert project.run("end") == 1
    assert project.results()["cases"][0]["status"] == "error"


def test_plan_schema_mismatch_is_rejected(make_project, capsys):
    project = make_project()
    assert project.run("gen") == 0
    plan_path = project.workdir / "plan.json"
    payload = json.loads(plan_path.read_text())
    payload["schema"] = 99
    plan_path.write_text(json.dumps(payload))

    assert main(["_exec", "--plan", str(plan_path), "--case-id", "smoke__1"]) == 2
    assert "schema" in capsys.readouterr().err


def test_plan_missing_file_is_an_infra_error(make_project, capsys):
    project = make_project()
    assert main(["_exec", "--plan", str(project.workdir / "nope.json"), "--case-id", "x__1"]) == 2
    assert "missing file" in capsys.readouterr().err


def test_python_dash_m_works_from_project_root(make_project):
    """Regression test for D9: a config file must never shadow the package."""
    project = make_project()
    result = subprocess.run(
        [sys.executable, "-m", "solouvm", "-c", "solouvm.conf.py", "gen"],
        cwd=str(project.root),
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert (project.workdir / "plan.json").is_file()
    # And a bare `python -m solouvm` must import the package, not the config.
    version = subprocess.run(
        [sys.executable, "-m", "solouvm", "--version"],
        cwd=str(project.root),
        check=False,
        capture_output=True,
        text=True,
    )
    assert "solouvm 0.1.0" in version.stdout


def test_unknown_simulator_requires_sim_section(tmp_path: Path):
    (tmp_path / "solouvm.conf.py").write_text("CFG = {'simulator': 'mystery'}\n")
    cfg = load_config(tmp_path / "solouvm.conf.py", env={})
    with pytest.raises(ConfigError, match="unknown simulator"):
        simulator.run_argv(cfg)


def test_sim_command_placeholders_and_rejects_bad_types(tmp_path: Path):
    (tmp_path / "solouvm.conf.py").write_text(
        "CFG = {'simulator': 'x', 'sim': {'build': 'gcc -o {simv} {root}', 'run': './{simv}'}}\n"
    )
    cfg = load_config(tmp_path / "solouvm.conf.py", env={})
    argv = simulator.build_argv(cfg)
    assert argv[0] == "gcc"
    assert argv[-1] == str(tmp_path)

    (tmp_path / "bad.conf.py").write_text(
        "CFG = {'simulator': 'x', 'sim': {'build': 42, 'run': './simv'}}\n"
    )
    bad = load_config(tmp_path / "bad.conf.py", env={})
    with pytest.raises(ConfigError, match="str or list"):
        simulator.build_argv(bad)


def test_case_validation_messages():
    with pytest.raises(ConfigError, match="non-empty string"):
        Case(name="")
    with pytest.raises(ConfigError, match="retry"):
        Case(name="x", retry=-1)
    with pytest.raises(ConfigError, match="mappings"):
        Case(name="x", params=[("a", 1)])  # type: ignore[arg-type]


def test_report_verbose_shows_log_and_cmd():
    results = {
        "project": "demo",
        "run_id": "r1",
        "simulator": "fake",
        "counts": {"pass": 0, "fail": 1},
        "cases": [
            {
                "case_id": "smoke__1",
                "seed": 1,
                "status": "fail",
                "reason": "UVM_ERROR=1 > 0",
                "log": "sim.log",
                "cwd": ".solouvm/out/smoke__1",
                "cmd": ["simv", "+UVM_TESTNAME=x"],
            }
        ],
    }
    text = render_text(results, verbose=True)
    assert "FAIL" in text
    assert "sim.log" in text
    assert "+UVM_TESTNAME=x" in text


def test_report_handles_list_shaped_reason():
    results = {
        "counts": {},
        "cases": [
            {
                "case_id": "a__1",
                "status": "error",
                "checks_fired": [{"kind": "infra", "message": "boom"}],
            }
        ],
    }
    assert "boom" in render_text(results)


def test_aggregate_defaults_and_exit_code():
    plan = {"run_id": "r", "project": "p", "simulator": "s"}
    results = status_mod.aggregate(plan, [{"status": "pass"}])
    assert results["counts"]["pass"] == 1
    assert results["counts"]["fail"] == 0
    assert status_mod.exit_code(results) == 0

    bad = status_mod.aggregate(plan, [{"status": "xpass"}])
    assert status_mod.exit_code(bad) == 1
    assert status_mod.is_bad("missing") is True
    assert status_mod.is_bad("pass") is False
