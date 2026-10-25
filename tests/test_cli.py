from __future__ import annotations

import json


def test_gen_writes_artifacts(make_project, capsys):
    project = make_project()
    assert project.run("gen") == 0

    assert (project.root / "filelist.f").is_file()
    assert (project.workdir / "plan.json").is_file()
    assert (project.workdir / "Makefile").is_file()
    assert (project.root / "Makefile").is_file()

    plan = project.plan()
    assert plan["project"] == "demo"
    assert plan["cases"][0]["case_id"] == "smoke__1"
    assert plan["cases"][0]["uvm_test"] == "smoke_test"

    out = capsys.readouterr().out
    assert "planned 1 case(s)" in out


def test_regress_pass(make_project, capsys):
    project = make_project([{"name": "smoke", "uvm_test": "smoke_test", "params": {"MODE": "pass"}}])
    assert project.run("regress") == 0

    status = project.status("smoke__1")
    assert status["status"] == "pass"
    assert status["raw_status"] == "pass"
    assert status["verdict"] is None
    assert status["rc"] == 0
    assert status["signal"] is None
    assert status["timeout"] is False
    assert status["uvm"]["error"] == 0
    assert status["log"] == "sim.log"
    assert status["run_id"] == project.plan()["run_id"]

    results = project.results()
    assert results["counts"]["pass"] == 1
    assert results["run_id"] == status["run_id"]
    assert (project.workdir / "results.txt").is_file()


def test_regress_uvm_error_fails(make_project):
    project = make_project([{"name": "bad", "params": {"MODE": "uvm_error"}}])
    assert project.run("regress") == 1
    status = project.status("bad__1")
    assert status["status"] == "fail"
    assert "UVM_ERROR" in status["reason"]


def test_regress_nonzero_rc_fails(make_project):
    project = make_project([{"name": "rc", "params": {"MODE": "rc1"}}])
    assert project.run("regress") == 1
    assert project.status("rc__1")["status"] == "fail"


def test_regress_timeout_is_killed(make_project):
    project = make_project(
        [{"name": "hang", "params": {"MODE": "hang"}}], timeout="400ms"
    )
    assert project.run("regress") == 1
    status = project.status("hang__1")
    assert status["status"] == "timeout"
    assert status["timeout"] is True


def test_regress_signal_is_killed(make_project):
    project = make_project([{"name": "crash", "params": {"MODE": "crash"}}])
    assert project.run("regress") == 1
    status = project.status("crash__1")
    assert status["status"] == "killed"
    assert status["signal"] == "SIGSEGV"


def test_expect_fail_maps_to_xfail(make_project):
    project = make_project(
        [{"name": "neg", "params": {"MODE": "uvm_error"}, "expect_fail": True}]
    )
    assert project.run("regress") == 0
    status = project.status("neg__1")
    assert status["status"] == "xfail"
    assert status["raw_status"] == "fail"
    assert status["verdict"] == "XFAIL"
    assert project.results()["counts"]["xfail"] == 1


def test_expect_fail_passing_case_is_xpass(make_project):
    project = make_project(
        [{"name": "neg", "params": {"MODE": "pass"}, "expect_fail": True}]
    )
    assert project.run("regress") == 1
    assert project.status("neg__1")["status"] == "xpass"


def test_retry_records_every_attempt(make_project):
    project = make_project(
        [{"name": "flaky", "params": {"MODE": "uvm_error"}, "retry": 2}], retry=0
    )
    assert project.run("regress") == 1
    status = project.status("flaky__1")
    assert len(status["attempts"]) == 3
    assert [item["n"] for item in status["attempts"]] == [1, 2, 3]
    assert status["status"] == "fail"


def test_multiple_seeds_and_cases(make_project):
    project = make_project(
        [{"name": "a", "params": {"MODE": "pass"}}, {"name": "b", "params": {"MODE": "pass"}}],
        seeds="1-2",
    )
    assert project.run("regress") == 0
    ids = [item["case_id"] for item in project.plan()["cases"]]
    assert ids == ["a__1", "a__2", "b__1", "b__2"]
    assert project.results()["counts"]["pass"] == 4


def test_seed_cli_override(make_project):
    project = make_project(seeds=1)
    assert project.run("gen", "--seeds", "7,9") == 0
    assert [item["seed"] for item in project.plan()["cases"]] == [7, 9]


def test_fail_regex_from_config(make_project):
    project = make_project(
        [{"name": "smoke", "params": {"MODE": "pass"}}],
        checks={"fail_regex": ["TEST PASSED"]},
    )
    assert project.run("regress") == 1
    status = project.status("smoke__1")
    assert status["checks_fired"][0]["kind"] == "fail_regex"


def test_non_uvm_case_needs_done_marker(make_project):
    project = make_project([{"name": "plain", "params": {"MODE": "plain_pass"}}])
    assert project.run("regress") == 0
    assert project.status("plain__1")["status"] == "pass"

    project = make_project([{"name": "plain", "params": {"MODE": "plain_none"}}])
    assert project.run("regress") == 1
    assert project.status("plain__1")["status"] == "fail"


def test_end_strict_missing_cases_fail(make_project):
    project = make_project([{"name": "a", "params": {"MODE": "pass"}}])
    assert project.run("gen") == 0
    assert project.run("end") == 1
    assert project.results()["counts"]["error"] == 1


def test_end_allow_missing_downgrades_to_skip(make_project):
    project = make_project([{"name": "a", "params": {"MODE": "pass"}}])
    assert project.run("gen") == 0
    assert project.run("end", "--allow-missing") == 0
    assert project.results()["counts"]["skip"] == 1


def test_end_is_idempotent(make_project):
    project = make_project([{"name": "a", "params": {"MODE": "pass"}}])
    assert project.run("regress") == 0
    first = project.results()
    assert project.run("end") == 0
    second = project.results()
    assert first["cases"] == second["cases"]
    assert first["run_id"] == second["run_id"]


def test_end_detects_pending_status(make_project):
    project = make_project([{"name": "a", "params": {"MODE": "pass"}}])
    assert project.run("gen") == 0
    outdir = project.workdir / "out" / "a__1"
    outdir.mkdir(parents=True)
    plan = project.plan()
    payload = {
        "schema": 1,
        "run_id": plan["run_id"],
        "case_id": "a__1",
        "status": "pending",
        "raw_status": "pending",
    }
    (outdir / "status.json").write_text(json.dumps(payload))
    assert project.run("end") == 1
    assert "pending" in project.results()["cases"][0]["reason"]


def test_end_detects_stale_run_id(make_project):
    project = make_project([{"name": "a", "params": {"MODE": "pass"}}])
    assert project.run("gen") == 0
    outdir = project.workdir / "out" / "a__1"
    outdir.mkdir(parents=True)
    (outdir / "status.json").write_text(
        json.dumps({"schema": 1, "run_id": "old-run", "case_id": "a__1", "status": "pass"})
    )
    assert project.run("end") == 1
    assert "stale" in project.results()["cases"][0]["reason"]


def test_run_command_selects_a_case(make_project):
    project = make_project(
        [{"name": "a", "params": {"MODE": "pass"}}, {"name": "b", "params": {"MODE": "uvm_error"}}]
    )
    assert project.run("run", "--case", "a") == 0
    outdirs = sorted(path.name for path in (project.workdir / "out").iterdir())
    assert outdirs == ["a__1"]


def test_run_unknown_case_is_a_config_error(make_project, capsys):
    project = make_project()
    code = project.run("run", "--case", "nope")
    assert code == 2
    assert "unknown case" in capsys.readouterr().err


def test_exec_direct_invocation(make_project):
    from solouvm.cli import main

    project = make_project([{"name": "a", "params": {"MODE": "pass"}}])
    assert project.run("gen") == 0
    plan_path = project.workdir / "plan.json"
    assert main(["_exec", "--plan", str(plan_path), "--case-id", "a__1"]) == 0
    assert project.status("a__1")["status"] == "pass"


def test_exec_unknown_case_id_is_an_infra_error(make_project, capsys):
    from solouvm.cli import main

    project = make_project()
    assert project.run("gen") == 0
    plan_path = project.workdir / "plan.json"
    assert main(["_exec", "--plan", str(plan_path), "--case-id", "nope__1"]) == 2
    assert "not in the plan" in capsys.readouterr().err


def test_clean_removes_outputs(make_project):
    project = make_project()
    assert project.run("regress") == 0
    assert project.run("clean") == 0
    assert not (project.workdir / "out").exists()
    assert not (project.workdir / "results.json").exists()


def test_verbose_streams_simulator_output(make_project, capfd):
    project = make_project()
    assert project.run("regress", "-v") == 0
    # _exec runs in a subprocess, so capture at the file-descriptor level.
    assert "Running test smoke_test" in capfd.readouterr().out


def test_version_without_config(tmp_path, capsys):
    from solouvm.cli import main

    assert main(["--version"]) == 0
    assert "solouvm" in capsys.readouterr().out


def test_missing_config_is_reported(tmp_path, capsys, monkeypatch):
    from solouvm.cli import main

    monkeypatch.chdir(tmp_path)
    assert main(["gen"]) == 2
    assert "cannot find solouvm.conf.py" in capsys.readouterr().err
