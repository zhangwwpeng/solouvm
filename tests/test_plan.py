from __future__ import annotations

import json
from pathlib import Path

import pytest

from solouvm import makefile
from solouvm import plan as plan_mod
from solouvm.config import load_config
from solouvm.model import Case


def make_cfg(tmp_path: Path, **extra):
    (tmp_path / "solouvm.conf.py").write_text(
        "CFG = {'project': 'demo', 'simulator': 'fake',"
        " 'sim': {'build': 'echo build', 'run': 'echo run'},"
        " 'regress': {'seeds': [2, 1]}}\n"
    )
    return load_config(tmp_path / "solouvm.conf.py", env={})


def test_plan_expands_case_by_seed(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    cases = [Case(name="smoke"), Case(name="fast")]
    plan = plan_mod.build(cfg, cases)

    assert len(plan["cases"]) == 4
    assert [item["case_id"] for item in plan["cases"]] == [
        "fast__1",
        "fast__2",
        "smoke__1",
        "smoke__2",
    ]
    assert plan["cases"][0]["outdir"] == ".solouvm/out/fast__1"
    assert plan["cases"][0]["timeout_s"] == 1200.0
    assert plan["schema"] == plan_mod.SCHEMA


def test_plan_roundtrip(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    plan = plan_mod.build(cfg, [Case(name="smoke")])
    path = plan_mod.write(cfg, plan)

    loaded = plan_mod.load(path)
    specs = plan_mod.specs(loaded)
    assert specs[0].case_id == "smoke__1"
    assert specs[0].outdir == ".solouvm/out/smoke__1"


def test_plan_rejects_empty_inputs(tmp_path: Path):
    from solouvm.errors import InfraError

    cfg = make_cfg(tmp_path)
    with pytest.raises(InfraError, match="no cases"):
        plan_mod.build(cfg, [])


def test_plan_select_filters(tmp_path: Path):
    from solouvm.errors import InfraError

    cfg = make_cfg(tmp_path)
    plan = plan_mod.build(cfg, [Case(name="smoke"), Case(name="fast")])
    only = plan_mod.select(plan, ["smoke"], seeds=[1])
    assert [item["case_id"] for item in only["cases"]] == ["smoke__1"]
    assert len(plan["cases"]) == 4

    with pytest.raises(InfraError, match="no planned case"):
        plan_mod.select(plan, ["nope"])


def test_filelist_text_includes_incdirs_and_defines(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    cfg.incdirs = ["tb"]
    cfg.defines = ["DEBUG"]
    text = makefile.filelist_text(cfg, ["rtl/a.sv"])
    assert "+incdir+tb" in text
    assert "+define+DEBUG" in text
    assert text.rstrip().endswith("rtl/a.sv")


def test_makefile_contains_run_targets_and_finalize(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    plan = plan_mod.build(cfg, [Case(name="smoke")])
    text = makefile.render(cfg, plan)

    assert "run/smoke__1: build" in text
    assert "regress: run/smoke__1" in text
    assert "_finalize:" in text
    assert "-m solouvm" in text


def test_makefile_write_creates_root_delegator(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    plan = plan_mod.build(cfg, [Case(name="smoke")])
    written = makefile.write(cfg, plan, ["rtl/a.sv"])

    assert (tmp_path / "filelist.f").is_file()
    assert (tmp_path / ".solouvm" / "Makefile").is_file()
    assert (tmp_path / "Makefile").is_file()
    assert "include .solouvm/Makefile" in (tmp_path / "Makefile").read_text()
    assert len(written) == 3


def test_makefile_write_keeps_existing_root_makefile(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    plan = plan_mod.build(cfg, [Case(name="smoke")])
    (tmp_path / "Makefile").write_text("# user makefile\n")

    makefile.write(cfg, plan, ["rtl/a.sv"])
    assert (tmp_path / "Makefile").read_text() == "# user makefile\n"


def test_plan_json_is_readable_offline(tmp_path: Path):
    cfg = make_cfg(tmp_path)
    plan = plan_mod.build(cfg, [Case(name="smoke")])
    payload = json.loads(json.dumps(plan))
    assert payload["exec"]["sim_cmd"]
    assert payload["checks"]["max_uvm_error"] == 0
