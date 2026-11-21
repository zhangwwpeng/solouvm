"""Tests for solouvm.init (task 24, Z-01, U-03, AI-03, AI-12)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from solouvm.backends import fake_sim_source, get_backend
from solouvm.collect.cases import collect_cases
from solouvm.collect.files import collect_files, load_filelist
from solouvm.config.load import load_config
from solouvm.errors import ConfigError
from solouvm.gen.filelist import write_filelists
from solouvm.gen.makefile import render_entry_makefile, render_run_mk
from solouvm.gen.scripts import render_run_job
from solouvm.init import GITIGNORE_ENTRIES, init_project
from solouvm.judge.facts import read_facts
from solouvm.judge.rules import decide
from solouvm.judge.scan import scan
from solouvm.model import validate_records
from solouvm.plan import build_plan
from solouvm.uvmgen import find_svh_inconsistencies, lint_style, load_tbspec, uvm_sync


def _snapshot_tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_init_full_project_validates_and_lints_clean(tmp_path: Path) -> None:
    created = init_project(root=tmp_path)
    assert "solouvm.conf.py" in created
    assert "filelist.py" in created
    assert "tbspec.py" in created
    assert "case.py" in created
    assert "doc/verif/system.md" in created
    assert "rtl/axi_slv_dummy.sv" in created
    assert "Makefile" in created
    assert ".gitignore" in created
    assert "verif/tb/tb_top.sv" in created
    assert "verif/tb/binds.sv" in created
    assert "verif/tests/axi/axi_smoke_test.sv" in created

    cfg = load_config(tmp_path)
    assert cfg.simulator == "fake"
    assert cfg.top == "tb_top"

    fl = collect_files(tmp_path, load_filelist(tmp_path))
    assert "rtl/axi_slv_dummy.sv" in fl.groups["rtl"]
    assert "verif/tb/tb_top.sv" in fl.groups["top"]
    assert "verif/agents/axi/axi_if.sv" in fl.groups["tb"]
    assert "verif/tests/axi/axi_test_pkg.sv" in fl.groups["tb"]

    spec = load_tbspec(tmp_path)
    assert len(spec.envs) == 1 and spec.envs[0].name == "axi"
    assert find_svh_inconsistencies(tmp_path) == []
    assert lint_style(tmp_path) == []

    sync_report = uvm_sync(tmp_path, dry_run=True)
    assert sync_report.created == []
    assert sync_report.updated == []
    assert sync_report.modified_outside == []
    assert sync_report.extra_files == []

    cases = collect_cases(cfg)
    assert len(cases) == 1
    case = cases[0]
    assert case.stage == "wip"
    assert case.intent
    assert case.method
    assert case.spec_refs
    assert len(case.expect) == 2
    assert "+SOLOUVM_FAKE_SCB=AXI_SCB:10" in case.opts
    validate_records(case)

    plan = build_plan(cases=cases, config=cfg, run_id="20261118-000001-init")
    assert len(plan.jobs) >= 1


def test_init_fake_project_runs_and_judges_pass(tmp_path: Path) -> None:
    init_project(root=tmp_path)
    cfg = load_config(tmp_path)
    fl = collect_files(tmp_path, load_filelist(tmp_path))
    cases = collect_cases(cfg)
    backend = get_backend(cfg.simulator)
    run_id = "20261118-000002-init"
    plan = build_plan(cases=cases, config=cfg, run_id=run_id)
    build_keys = {"default": "init00000001"}
    build_dirs = {"default": ".solouvm/build/default/init00000001"}

    bin_dir = tmp_path / ".solouvm" / "gen" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    run_job_sh = bin_dir / "run_job.sh"
    run_job_sh.write_text(render_run_job(), encoding="utf-8")
    run_job_sh.chmod(0o755)
    fake_sh = bin_dir / "fake_sim.sh"
    fake_sh.write_text(fake_sim_source(), encoding="utf-8")
    fake_sh.chmod(0o755)

    write_filelists(tmp_path, plan, fl, build_dirs, config=cfg)
    sol_dir = tmp_path / ".solouvm"
    (sol_dir / "Makefile").write_text(render_entry_makefile(), encoding="utf-8")
    run_mk = sol_dir / "runs" / run_id / "run.mk"
    run_mk.parent.mkdir(parents=True, exist_ok=True)
    run_mk.write_text(
        render_run_mk(plan=plan, config=cfg, files=fl, build_keys=build_keys, backend=backend),
        encoding="utf-8",
    )

    proc = subprocess.run(
        ["make", "-C", str(tmp_path), "-f", str(run_mk), "regress"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    job = plan.jobs[0]
    try_dir = tmp_path / job.dir / "try1"
    facts = read_facts(try_dir / "job.facts")
    build_facts = read_facts(tmp_path / build_dirs["default"] / "job.facts")
    log_lines = (try_dir / "sim.log").read_text(encoding="utf-8").splitlines()
    scan_res = scan(log_lines, checks=cfg.checks, backend=backend, expects=cases[0].expect)
    verdict = decide(
        facts=facts,
        scan=scan_res,
        build_ok=True,
        build_facts=build_facts,
        build_key=build_keys["default"],
        checks=cfg.checks,
        case=cases[0],
        allow_missing=False,
        run_id=run_id,
    )
    assert verdict.status == "pass", verdict.reason


def test_init_refuses_overwrite_without_modifying_files(tmp_path: Path) -> None:
    init_project(root=tmp_path)
    before = _snapshot_tree(tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        init_project(root=tmp_path)
    assert exc_info.value.code == "CFG_INVALID"
    msg = str(exc_info.value)
    assert "solouvm.conf.py" in msg
    assert "case.py" in msg
    assert "filelist.py" in msg
    assert "tbspec.py" in msg
    assert _snapshot_tree(tmp_path) == before


def test_init_bare_mode(tmp_path: Path) -> None:
    created = init_project(root=tmp_path, bare=True)
    assert set(created) == {"solouvm.conf.py", "filelist.py", "tbspec.py", "case.py", ".gitignore"}
    assert not (tmp_path / "verif").exists()
    assert not (tmp_path / "rtl").exists()
    assert not (tmp_path / "doc").exists()
    assert not (tmp_path / "Makefile").exists()

    cfg = load_config(tmp_path)
    assert cfg.system_doc == []


def test_init_simulator_option_and_agents(tmp_path: Path) -> None:
    proj = tmp_path / "vcs_proj"
    created = init_project(root=proj, simulator="vcs", agents="claude")
    assert ".claude/agents/solouvm-triage.md" in created

    cfg = load_config(proj)
    assert cfg.simulator == "vcs"
    cases = collect_cases(cfg)
    assert cases[0].opts == []

    agent_md = (proj / ".claude/agents/solouvm-triage.md").read_text(encoding="utf-8")
    assert "--role triage" in agent_md

    with pytest.raises(ConfigError) as exc_info:
        init_project(root=tmp_path / "bad_agents", agents="bogus")
    assert exc_info.value.code == "CFG_INVALID"
    assert not (tmp_path / "bad_agents").exists()


def test_init_gitignore_append_and_existing_makefile(tmp_path: Path) -> None:
    gi = tmp_path / ".gitignore"
    gi.write_text("*.log\n.solouvm/\n", encoding="utf-8")
    mk = tmp_path / "Makefile"
    mk.write_text("# custom user Makefile\nall:\n\t@echo custom\n", encoding="utf-8")

    created = init_project(root=tmp_path)
    assert "Makefile" not in created
    assert mk.read_text(encoding="utf-8") == "# custom user Makefile\nall:\n\t@echo custom\n"

    gi_lines = gi.read_text(encoding="utf-8").splitlines()
    assert gi_lines[0] == "*.log"
    assert gi_lines.count(".solouvm/") == 1
    for entry in GITIGNORE_ENTRIES:
        assert entry in gi_lines


def test_init_determinism_and_system_doc_sections(tmp_path: Path) -> None:
    dir_a = tmp_path / "proj_a"
    dir_b = tmp_path / "proj_b"
    init_project(root=dir_a, agents="claude")
    init_project(root=dir_b, agents="claude")

    assert _snapshot_tree(dir_a) == _snapshot_tree(dir_b)

    sys_doc = (dir_a / "doc/verif/system.md").read_text(encoding="utf-8")
    for heading in (
        "DUT 功能概述与接口",
        "验证环境结构",
        "日志约定",
        "已知限制与不验证的部分",
        "调试指引",
    ):
        assert heading in sys_doc


def test_init_partial_conflict_writes_nothing(tmp_path: Path) -> None:
    binds = tmp_path / "verif" / "tb" / "binds.sv"
    binds.parent.mkdir(parents=True)
    binds.write_text("// existing binds\n", encoding="utf-8")
    before = _snapshot_tree(tmp_path)

    with pytest.raises(ConfigError) as exc_info:
        init_project(root=tmp_path)
    assert exc_info.value.code == "CFG_INVALID"
    assert "verif/tb/binds.sv" in str(exc_info.value)
    assert _snapshot_tree(tmp_path) == before
    assert not (tmp_path / "solouvm.conf.py").exists()
    assert not (tmp_path / ".gitignore").exists()
