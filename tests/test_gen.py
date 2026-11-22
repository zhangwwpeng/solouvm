"""Tests for solouvm.gen (shell, filelist, makefile, write_generated_project_files)."""

from __future__ import annotations

import os
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from solouvm.backends import get_backend
from solouvm.collect.files import FileEntry, FileList
from solouvm.config.schema import (
    BuildConfig,
    CheckConfig,
    Config,
    DocConfig,
    HistoryConfig,
    JobHooks,
    LimitsConfig,
    ProviderConfig,
    RegressConfig,
    RunConfig,
    TriageConfig,
    UvmConfig,
)
from solouvm.errors import ConfigError
from solouvm.gen import (
    make_escape,
    relpath,
    render_entry_makefile,
    render_root_makefile,
    render_run_mk,
    sh_quote,
    write_filelists,
    write_generated_project_files,
)
from solouvm.judge.facts import read_facts
from solouvm.judge.scan import scan
from solouvm.model import Checks, Expect, Job, Plan

TZ = timezone(timedelta(hours=8))


def _make_config(root: Path, **overrides: object) -> Config:
    defaults: dict[str, object] = {
        "root": root,
        "project": "demo",
        "simulator": "vcs",
        "top": "tb_top",
        "uvm_test": "axi_smoke_test",
        "incdirs": [],
        "defines": [],
        "builds": {"default": BuildConfig()},
        "regress": RegressConfig(),
        "run": RunConfig(),
        "checks": Checks(),
        "job": JobHooks(),
        "sim": {},
        "providers": {"local": ProviderConfig(name="local")},
        "provider": "local",
        "check": CheckConfig(),
        "case_dirs": [],
        "system_doc": [],
        "triage": TriageConfig(),
        "doc": DocConfig(),
        "history": HistoryConfig(),
        "limits": LimitsConfig(),
        "plugins": [],
        "uvm": UvmConfig(),
    }
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


def _make_files() -> FileList:
    return FileList(
        groups={
            "rtl": ["rtl/pkg.sv", "rtl/dut.sv"],
            "tb": ["verif/tb_pkg.sv"],
            "top": ["verif/tb_top.sv"],
        },
        incdirs={"rtl": [], "tb": [], "top": []},
        defines={"rtl": [], "tb": [], "top": []},
        entries=[
            FileEntry("rtl/pkg.sv", "rtl", "rtl/pkg.sv"),
            FileEntry("rtl/dut.sv", "rtl", "rtl/dut.sv"),
            FileEntry("verif/tb_pkg.sv", "tb", "verif/tb_pkg.sv"),
            FileEntry("verif/tb_top.sv", "top", "verif/tb_top.sv"),
        ],
        libs={"rtl": [], "tb": [], "top": []},
    )


def _make_job(
    *,
    job_id: str = "burst__4417",
    case: str = "burst",
    seed: int = 4417,
    build: str = "default",
    uvm_test: str | None = "axi_burst_test",
    params: dict[str, object] | None = None,
    opts: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout_s: float = 1800.0,
    hang_s: float | None = 600.0,
    run_id: str = "20261103-101500-a1b2",
) -> Job:
    return Job(
        job_id=job_id,
        case=case,
        seed=seed,
        build=build,
        uvm_test=uvm_test,
        params=params if params is not None else {},
        opts=opts if opts is not None else [],
        env=env if env is not None else {},
        timeout_s=timeout_s,
        hang_s=hang_s,
        retry=0,
        expect_fail=False,
        tags=(),
        dir=f".solouvm/runs/{run_id}/{job_id}",
    )


def _make_plan(
    *,
    run_id: str = "20261103-101500-a1b2",
    builds: dict[str, str] | None = None,
    jobs: list[Job] | None = None,
    provider: str = "local",
) -> Plan:
    return Plan(
        run_id=run_id,
        created_at=datetime(2026, 11, 3, 10, 15, 0, tzinfo=TZ),
        parent_run=None,
        provider=provider,
        selection={"stage": ["wip", "ready", "signoff"]},
        builds=builds if builds is not None else {"default": "3c9e1f2a1234"},
        jobs=jobs if jobs is not None else [_make_job(run_id=run_id)],
    )


# ---------- shell.py ----------


def test_sh_quote_and_make_escape_cases() -> None:
    assert sh_quote("a b") == "'a b'"
    assert sh_quote("it's") == "'it'\"'\"'s'"
    assert sh_quote('a"b') == "'a\"b'"
    assert sh_quote("a$b") == "'a$b'"
    assert make_escape("a$b") == "'a$$b'"
    assert sh_quote("") == "''"
    assert sh_quote("中文") == "'中文'"
    assert make_escape("$(touch pwned)") == "'$$(touch pwned)'"


@pytest.mark.parametrize("bad", ["a\nb", "a\rb", "line1\r\nline2"])
def test_sh_quote_rejects_newlines_with_field_context(bad: str) -> None:
    with pytest.raises(ConfigError) as exc:
        sh_quote(bad, field="case.opts")
    assert exc.value.code == "CFG_INVALID"
    assert "case.opts" in exc.value.message


def test_relpath_cases(monkeypatch: pytest.MonkeyPatch) -> None:
    assert (
        relpath(".solouvm/build/default/k/simv", ".solouvm/runs/r/j/try1")
        == "../../../../build/default/k/simv"
    )
    assert relpath("a/b", "a/b") == "."
    assert relpath(".", ".solouvm/build/default/k") == "../../../.."
    assert relpath(".", ".solouvm/runs/r/j/try1") == "../../../../.."
    assert relpath("${UVM_HOME}/src", ".solouvm/build/default/k") == "${UVM_HOME}/src"
    assert relpath("{simv}", ".solouvm/runs/r/j/try1") == "{simv}"

    def fake_relpath(_t: str, _s: str) -> str:
        raise ValueError("path is on mount 'D:', start on mount 'C:'")

    monkeypatch.setattr(os.path, "relpath", fake_relpath)
    with pytest.raises(ConfigError) as exc:
        relpath("D:/foo", "C:/bar")
    assert exc.value.code == "CFG_INVALID"


# ---------- filelist.py ----------


def test_write_filelists_pure_paths_and_order(tmp_path: Path) -> None:
    files = _make_files()
    plan = _make_plan()
    build_dirs = {"default": ".solouvm/build/default/3c9e1f2a1234"}
    written = write_filelists(tmp_path, plan, files, build_dirs)

    bdir = tmp_path / ".solouvm" / "build" / "default" / "3c9e1f2a1234"
    gdir = tmp_path / ".solouvm" / "gen"

    merged_text = (bdir / "filelist.f").read_text(encoding="utf-8")
    assert merged_text.endswith("\n")
    assert merged_text.splitlines() == [
        "../../../../rtl/pkg.sv",
        "../../../../rtl/dut.sv",
        "../../../../verif/tb_pkg.sv",
        "../../../../verif/tb_top.sv",
    ]
    assert (bdir / "filelist.rtl.f").read_text(encoding="utf-8").splitlines() == [
        "../../../../rtl/pkg.sv",
        "../../../../rtl/dut.sv",
    ]
    assert (bdir / "filelist.tb.f").read_text(encoding="utf-8").splitlines() == [
        "../../../../verif/tb_pkg.sv",
    ]
    assert (bdir / "filelist.top.f").read_text(encoding="utf-8").splitlines() == [
        "../../../../verif/tb_top.sv",
    ]

    assert (gdir / "filelist.f").read_text(encoding="utf-8").splitlines() == [
        "rtl/pkg.sv",
        "rtl/dut.sv",
        "verif/tb_pkg.sv",
        "verif/tb_top.sv",
    ]
    assert ".solouvm/build/default/3c9e1f2a1234/filelist.f" in written
    assert ".solouvm/gen/filelist.f" in written


def test_write_filelists_directives_and_verbatim_paths(tmp_path: Path) -> None:
    files = FileList(
        groups={
            "rtl": ["rtl/a.sv", "/opt/vip/ext.sv", "$ENV_VIP/v.sv"],
            "tb": ["verif/t.sv"],
            "top": [],
        },
        incdirs={"rtl": ["rtl/inc"], "tb": ["${UVM_HOME}/src"], "top": []},
        defines={"rtl": ["RTL_DEF=1"], "tb": [], "top": []},
        entries=[
            FileEntry("rtl/a.sv", "rtl", "rtl/a.sv"),
            FileEntry("/opt/vip/ext.sv", "rtl", "/opt/vip/ext.sv", outside_root=True),
            FileEntry("$ENV_VIP/v.sv", "rtl", "$ENV_VIP/v.sv"),
            FileEntry("verif/t.sv", "tb", "verif/t.sv"),
        ],
        libs={"rtl": ["-v rtl/lib.v", "-y /opt/y_lib", "+libext+.sv"], "tb": [], "top": []},
    )
    cfg = _make_config(
        tmp_path,
        defines=["SIM"],
        incdirs=["common/inc"],
        builds={
            "gate": BuildConfig(defines=["GATE_SIM"], incdirs=["gate/inc"]),
        },
    )
    plan = _make_plan(builds={"gate": "abcdef123456"})
    write_filelists(
        tmp_path,
        plan,
        files,
        {"gate": ".solouvm/build/gate/abcdef123456"},
        config=cfg,
    )

    lines = (
        (tmp_path / ".solouvm" / "build" / "gate" / "abcdef123456" / "filelist.f")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert "+define+SIM" in lines
    assert "+define+GATE_SIM" in lines
    assert "+define+RTL_DEF=1" in lines
    assert "+incdir+../../../../common/inc" in lines
    assert "+incdir+../../../../gate/inc" in lines
    assert "+incdir+../../../../rtl/inc" in lines
    assert "+incdir+${UVM_HOME}/src" in lines
    assert "-v ../../../../rtl/lib.v" in lines
    assert "-y /opt/y_lib" in lines
    assert "+libext+.sv" in lines
    assert "/opt/vip/ext.sv" in lines
    assert "$ENV_VIP/v.sv" in lines
    assert (tmp_path / ".solouvm" / "build" / "gate" / "abcdef123456" / "filelist.top.f").read_text(
        encoding="utf-8"
    ) == ""


def test_write_filelists_preserves_parent_relative_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    dv_dir = repo / "dv"
    rtl_dir = repo / "rtl"
    dv_dir.mkdir(parents=True)
    rtl_dir.mkdir(parents=True)
    files = FileList(
        groups={"rtl": ["../rtl/dut.sv"], "tb": ["verif/tb_pkg.sv"], "top": []},
        incdirs={"rtl": ["../rtl/inc"], "tb": [], "top": []},
        defines={"rtl": [], "tb": [], "top": []},
        entries=[
            FileEntry("../rtl/dut.sv", "rtl", "../rtl/filelist.py:dut.sv"),
            FileEntry("verif/tb_pkg.sv", "tb", "verif/tb_pkg.sv"),
        ],
        libs={"rtl": ["-v ../rtl/cells.v", "-y ../rtl/libdir"], "tb": [], "top": []},
    )
    cfg = _make_config(dv_dir)
    plan = _make_plan(builds={"default": "112233445566"})
    write_filelists(
        dv_dir,
        plan,
        files,
        {"default": ".solouvm/build/default/112233445566"},
        config=cfg,
    )

    build_lines = (
        (dv_dir / ".solouvm" / "build" / "default" / "112233445566" / "filelist.f")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    assert "+incdir+../../../../../rtl/inc" in build_lines
    assert "-v ../../../../../rtl/cells.v" in build_lines
    assert "-y ../../../../../rtl/libdir" in build_lines
    assert "../../../../../rtl/dut.sv" in build_lines
    assert "../../../../verif/tb_pkg.sv" in build_lines

    gen_lines = (
        (dv_dir / ".solouvm" / "gen" / "filelist.f").read_text(encoding="utf-8").splitlines()
    )
    assert "+incdir+../rtl/inc" in gen_lines
    assert "-v ../rtl/cells.v" in gen_lines
    assert "-y ../rtl/libdir" in gen_lines
    assert "../rtl/dut.sv" in gen_lines


# ---------- makefile.py ----------


def test_render_entry_and_root_makefiles() -> None:
    entry = render_entry_makefile("0.1.0")
    assert "solouvm 0.1.0" in entry and "do not edit" in entry
    assert "-include .solouvm/runs/latest/run.mk" in entry

    root_mk = render_root_makefile("0.1.0")
    assert "solouvm 0.1.0" in root_mk and "do not edit" in root_mk
    assert "include .solouvm/Makefile" in root_mk


def test_render_run_mk_structure_and_determinism(tmp_path: Path) -> None:
    cfg = _make_config(
        tmp_path,
        defines=["SIM"],
        builds={
            "default": BuildConfig(compile_opts=["+define+EXTRA"], run_opts=["+BLD_OPT"]),
            "cov": BuildConfig(coverage=True, timeout="1h"),
        },
        run=RunConfig(timeout="30m", hang="10m", opts=["+GLOBAL_OPT"], env={"A_ENV": "val$1"}),
        job=JobHooks(pre="ulimit -c 0", post="echo done"),
        providers={
            "local": ProviderConfig(name="local", setup="export TOOL=vcs"),
        },
    )
    jobs = [
        _make_job(
            job_id="axi-burst.v1__4417",
            case="axi-burst.v1",
            seed=4417,
            build="default",
            params={"NUM": 32, "FAST": True, "SKIP": False, "MSG": "a$b"},
            opts=["+CASE_OPT"],
            env={"J_ENV": "hello world"},
        ),
        _make_job(
            job_id="cov_case__7",
            case="cov_case",
            seed=7,
            build="cov",
            uvm_test=None,
            hang_s=None,
        ),
    ]
    keys = {"default": "3c9e1f2a1234", "cov": "998877665544"}
    plan = _make_plan(builds=keys, jobs=jobs)
    files = _make_files()
    backend = get_backend("vcs")

    mk1 = render_run_mk(
        plan=plan, config=cfg, files=files, build_keys=keys, backend=backend, version="0.1.0"
    )
    mk2 = render_run_mk(
        plan=plan, config=cfg, files=files, build_keys=keys, backend=backend, version="0.1.0"
    )
    assert mk1 == mk2

    assert "# generated by solouvm 0.1.0 -- do not edit" in mk1
    assert "TRY     ?= 1" in mk1
    assert "WAVE    ?= 0" in mk1
    assert "SUBMIT  ?=" in mk1
    assert "build: $(B_cov)/.ok $(B_default)/.ok" in mk1
    assert "build/default: $(B_default)/.ok" in mk1
    assert "build/cov: $(B_cov)/.ok" in mk1
    assert "$(B_default)/.ok:" in mk1
    assert " -f filelist.f -top tb_top -o simv" in mk1
    assert "+define+EXTRA" in mk1
    assert "-cm line+cond+fsm+tgl+branch" in mk1
    assert "-cm_dir ./cov.vdb" in mk1
    assert "run/axi-burst.v1__4417: $(B_default)/.ok" in mk1
    assert "run/cov_case__7: $(B_cov)/.ok" in mk1
    assert "regress: run/axi-burst.v1__4417 run/cov_case__7" in mk1
    assert "\t$(SUBMIT) $(RUN_JOB)" in mk1
    assert "../../../../build/default/3c9e1f2a1234/simv" in mk1
    assert "+UVM_TESTNAME=axi_burst_test" in mk1
    assert "+ntb_random_seed=4417" in mk1
    assert "+SOLOUVM_SEED=4417" in mk1
    assert "+SOLOUVM_JOB_ID=axi-burst.v1__4417" in mk1
    assert "+NUM=32" in mk1
    assert "+FAST" in mk1
    assert "+SKIP" not in mk1
    assert "'+MSG=a$$b'" in mk1
    assert "'A_ENV=val$$1'" in mk1
    assert "'J_ENV=hello world'" in mk1
    assert "+BLD_OPT" in mk1
    assert "+GLOBAL_OPT" in mk1
    assert "+CASE_OPT" in mk1
    assert "$(if $(filter 1,$(WAVE)),+fsdb+all=on,)" in mk1
    # Job without uvm_test omits +UVM_TESTNAME
    cov_section = mk1.split("run/cov_case__7:")[1]
    assert "+UVM_TESTNAME" not in cov_section


def test_render_run_mk_avoids_duplicating_already_merged_opts(tmp_path: Path) -> None:
    cfg = _make_config(
        tmp_path,
        builds={"default": BuildConfig(run_opts=["+B"])},
        run=RunConfig(opts=["+G"]),
    )
    job = _make_job(opts=["+B", "+G", "+C"])
    plan = _make_plan(jobs=[job])
    mk = render_run_mk(
        plan=plan,
        config=cfg,
        files=_make_files(),
        build_keys=plan.builds,
        backend=get_backend("vcs"),
    )
    run_line = mk.split("run/burst__4417:")[1]
    assert run_line.count("+B") == 1
    assert run_line.count("+G") == 1
    assert run_line.count("+C") == 1


def test_render_run_mk_rejects_unknown_placeholder_but_allows_b_env_var(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    plan = _make_plan()
    files = _make_files()

    bad_backend = get_backend("vcs", {"build": "vcs -f {filelist} {bogus}"})
    with pytest.raises(ConfigError) as exc:
        render_run_mk(
            plan=plan, config=cfg, files=files, build_keys=plan.builds, backend=bad_backend
        )
    assert exc.value.code == "CFG_INVALID"
    assert "bogus" in exc.value.message

    env_backend = get_backend("vcs", {"build": "${VCS_BIN}/vcs -f {filelist} -top {top} -o {simv}"})
    mk = render_run_mk(
        plan=plan, config=cfg, files=files, build_keys=plan.builds, backend=env_backend
    )
    assert "'$${VCS_BIN}/vcs'" in mk


def test_render_run_mk_does_not_duplicate_seed_when_already_in_run(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    plan = _make_plan()
    backend = get_backend("vcs", {"run": "{simv} -custom_seed={seed}"})
    mk = render_run_mk(
        plan=plan, config=cfg, files=_make_files(), build_keys=plan.builds, backend=backend
    )
    assert "-custom_seed=4417" in mk
    assert "+ntb_random_seed=4417" not in mk


def test_write_generated_project_files_and_real_make_with_fake_backend(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    files = FileList(
        groups={"rtl": ["rtl/dut.sv"], "tb": [], "top": []},
        incdirs={"rtl": [], "tb": [], "top": []},
        defines={"rtl": [], "tb": [], "top": []},
        entries=[FileEntry("rtl/dut.sv", "rtl", "rtl/dut.sv")],
        libs={"rtl": [], "tb": [], "top": []},
    )
    cfg = _make_config(tmp_path, simulator="fake")
    run_id = "20261103-120000-abcd"
    job = _make_job(
        job_id="smoke__1",
        case="smoke",
        seed=1,
        params={"SOLOUVM_FAKE_SCB": "AXI_SCB:100"},
        run_id=run_id,
    )
    plan = _make_plan(run_id=run_id, builds={"default": "112233445566"}, jobs=[job])
    backend = get_backend("fake")

    manifest = write_generated_project_files(
        config=cfg, plan=plan, files=files, backend=backend, version="0.1.0"
    )

    assert ".solouvm/gen/bin/run_job.sh" in manifest
    assert ".solouvm/gen/bin/launch.sh" in manifest
    assert ".solouvm/gen/bin/fake_sim.sh" in manifest
    assert f".solouvm/runs/{run_id}/run.mk" in manifest
    root_mk_text = (tmp_path / "Makefile").read_text(encoding="utf-8")
    assert root_mk_text.endswith("include .solouvm/Makefile\n")

    for rel in (".solouvm/gen/bin/run_job.sh", ".solouvm/gen/bin/launch.sh"):
        mode = (tmp_path / rel).stat().st_mode
        assert mode & stat.S_IXUSR

    # Existing root Makefile must NOT be overwritten (G-03)
    (tmp_path / "Makefile").write_text("# custom makefile\n", encoding="utf-8")
    write_generated_project_files(
        config=cfg, plan=plan, files=files, backend=backend, version="0.1.0"
    )
    assert (tmp_path / "Makefile").read_text(encoding="utf-8") == "# custom makefile\n"

    run_mk = f".solouvm/runs/{run_id}/run.mk"
    dry_build = subprocess.run(
        ["make", "-n", "-f", run_mk, "build"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert dry_build.returncode == 0, dry_build.stderr

    dry_reg = subprocess.run(
        ["make", "-n", "-f", run_mk, "regress"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert dry_reg.returncode == 0, dry_reg.stderr

    # Actually execute build & regress via make with WAVE=1
    res = subprocess.run(
        ["make", "-f", run_mk, "WAVE=1", "regress"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr

    build_dir = tmp_path / ".solouvm" / "build" / "default" / "112233445566"
    assert (build_dir / ".ok").is_file()
    build_facts = read_facts(build_dir / "job.facts")
    assert build_facts is not None
    assert build_facts.kind == "build"
    assert build_facts.rc == 0

    try_dir = tmp_path / ".solouvm" / "runs" / run_id / "smoke__1" / "try1"
    job_facts = read_facts(try_dir / "job.facts")
    assert job_facts is not None
    assert job_facts.kind == "job"
    assert job_facts.state == "done"
    assert job_facts.rc == 0

    sim_log = (try_dir / "sim.log").read_text(encoding="utf-8")
    scan_res = scan(
        sim_log.splitlines(),
        checks=cfg.checks,
        backend=backend,
        expects=[Expect(scoreboard="AXI_SCB", min=100)],
    )
    assert scan_res.scoreboard["AXI_SCB"] == 100

    # Manual repro with non-numeric TRY writes to tryr1 without clobbering try1
    repro = subprocess.run(
        ["make", "-f", run_mk, "TRY=r1", "WAVE=1", "run/smoke__1"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert repro.returncode == 0, repro.stdout + repro.stderr
    assert (tmp_path / ".solouvm" / "runs" / run_id / "smoke__1" / "tryr1" / "sim.log").is_file()


def test_make_with_spaces_in_project_root_succeeds(tmp_path: Path) -> None:
    proj = tmp_path / "with space"
    (proj / "rtl").mkdir(parents=True)
    (proj / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    files = FileList(
        groups={"rtl": ["rtl/dut.sv"], "tb": [], "top": []},
        incdirs={"rtl": [], "tb": [], "top": []},
        defines={"rtl": [], "tb": [], "top": []},
        entries=[FileEntry("rtl/dut.sv", "rtl", "rtl/dut.sv")],
        libs={"rtl": [], "tb": [], "top": []},
    )
    cfg = _make_config(proj, simulator="fake")
    run_id = "20261103-120000-spce"
    job = _make_job(job_id="smoke__1", case="smoke", seed=1, run_id=run_id)
    plan = _make_plan(run_id=run_id, builds={"default": "112233445566"}, jobs=[job])
    write_generated_project_files(
        config=cfg,
        plan=plan,
        files=files,
        backend=get_backend("fake"),
        version="0.1.0",
    )

    run_mk = f".solouvm/runs/{run_id}/run.mk"
    res = subprocess.run(
        ["make", "-f", run_mk, "regress"],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    assert (proj / ".solouvm" / "build" / "default" / "112233445566" / ".ok").is_file()
    facts = read_facts(proj / ".solouvm" / "runs" / run_id / "smoke__1" / "try1" / "job.facts")
    assert facts is not None and facts.rc == 0


def test_render_run_mk_wraps_compound_shell_commands(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, simulator="questa")
    job = _make_job(job_id="smoke__1", case="smoke", seed=1)
    plan = _make_plan(jobs=[job])
    mk = render_run_mk(
        plan=plan,
        config=cfg,
        files=_make_files(),
        build_keys=plan.builds,
        backend=get_backend("questa"),
    )
    assert "bash -c 'vlib work && vlog -work work -sv -f filelist.f && " in mk
    assert "'&&'" not in mk

    compound_run = get_backend(
        "questa",
        {"run": "echo prep && vsim -c {top}"},
    )
    mk_run = render_run_mk(
        plan=plan,
        config=cfg,
        files=_make_files(),
        build_keys=plan.builds,
        backend=compound_run,
    )
    assert "bash -c 'echo prep && vsim -c tb_top " in mk_run
    assert "+SOLOUVM_JOB_ID=smoke__1 \"$$@\"' _ " in mk_run
    assert "$(if $(filter 1,$(WAVE)),-wlf ./wave.wlf" in mk_run


def test_make_compound_build_executes_sequentially_and_propagates_failure(tmp_path: Path) -> None:
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    files = FileList(
        groups={"rtl": ["rtl/dut.sv"], "tb": [], "top": []},
        incdirs={"rtl": [], "tb": [], "top": []},
        defines={"rtl": [], "tb": [], "top": []},
        entries=[FileEntry("rtl/dut.sv", "rtl", "rtl/dut.sv")],
        libs={"rtl": [], "tb": [], "top": []},
    )
    run_id = "20261103-120000-qcmd"
    job = _make_job(job_id="smoke__1", case="smoke", seed=1, run_id=run_id)
    plan = _make_plan(run_id=run_id, builds={"default": "aabbccddeeff"}, jobs=[job])
    cfg = _make_config(tmp_path, simulator="fake")
    ok_backend = get_backend(
        "fake",
        {"build": "touch step1.flag && touch step2.flag && touch step3.flag"},
    )
    write_generated_project_files(config=cfg, plan=plan, files=files, backend=ok_backend)

    run_mk = f".solouvm/runs/{run_id}/run.mk"
    res_ok = subprocess.run(
        ["make", "-f", run_mk, "build"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res_ok.returncode == 0, res_ok.stdout + res_ok.stderr
    bdir = tmp_path / ".solouvm" / "build" / "default" / "aabbccddeeff"
    assert (bdir / ".ok").is_file()
    for step in ("step1.flag", "step2.flag", "step3.flag"):
        assert (bdir / step).is_file()

    # Step 2 fails with exit code 7 -> step 3 must not run, no .ok, rc=7 recorded
    fail_plan = _make_plan(run_id=run_id, builds={"default": "112233445500"}, jobs=[job])
    fail_backend = get_backend(
        "fake",
        {"build": "touch s1.flag && sh -c 'exit 7' && touch s3.flag"},
    )
    write_generated_project_files(config=cfg, plan=fail_plan, files=files, backend=fail_backend)
    res_fail = subprocess.run(
        ["make", "-f", run_mk, "build"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert res_fail.returncode != 0
    fail_bdir = tmp_path / ".solouvm" / "build" / "default" / "112233445500"
    assert (fail_bdir / "s1.flag").is_file()
    assert not (fail_bdir / "s3.flag").exists()
    assert not (fail_bdir / ".ok").exists()
    bfacts = read_facts(fail_bdir / "job.facts")
    assert bfacts is not None and bfacts.rc == 7
