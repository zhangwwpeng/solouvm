from __future__ import annotations

from pathlib import Path

import pytest

from solouvm import collect
from solouvm.config import load_config
from solouvm.errors import ConfigError


def config(tmp_path: Path, **overrides) -> object:
    files = overrides.pop("files", ["a.sv"])
    lines = ["CFG = {", "  'project': 'demo',", "}"]
    (tmp_path / "solouvm.conf.py").write_text("\n".join(lines) + "\n")
    for name in files:
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("// stub\n")
    return load_config(tmp_path / "solouvm.conf.py", env={})


def test_load_filelist_expands_globs_and_dedupes(tmp_path: Path):
    (tmp_path / "rtl").mkdir()
    (tmp_path / "rtl" / "a.sv").write_text("")
    (tmp_path / "rtl" / "b.sv").write_text("")
    (tmp_path / "solouvm.conf.py").write_text("CFG = {'project': 'demo'}\n")
    (tmp_path / "filelist.py").write_text("FILES = ['rtl/*.sv', 'rtl/a.sv']\n")
    cfg = load_config(tmp_path / "solouvm.conf.py", env={})

    files = collect.load_filelist(cfg)
    assert files == ["rtl/a.sv", "rtl/b.sv"]


def test_load_filelist_accepts_function(tmp_path: Path):
    cfg = config(tmp_path)
    (tmp_path / "filelist.py").write_text("def files():\n    return ['a.sv']\n")
    assert collect.load_filelist(cfg) == ["a.sv"]


def test_load_filelist_rejects_missing_entries(tmp_path: Path):
    cfg = config(tmp_path)
    (tmp_path / "filelist.py").write_text("FILES = ['nope/*.sv']\n")
    with pytest.raises(ConfigError, match="match no files"):
        collect.load_filelist(cfg)


def test_load_filelist_requires_list(tmp_path: Path):
    cfg = config(tmp_path)
    (tmp_path / "filelist.py").write_text("FILES = 'a.sv'\n")
    with pytest.raises(ConfigError, match="not a single string"):
        collect.load_filelist(cfg)


def test_load_cases_applies_defaults(tmp_path: Path):
    cfg = config(tmp_path)
    (tmp_path / "solouvm.conf.py").write_text(
        "CFG = {'project': 'demo', 'top': 'tb_top', 'uvm_test': 'base_test',"
        " 'regress': {'retry': 2}, 'run': {'timeout': '5s'}}\n"
    )
    cfg = load_config(tmp_path / "solouvm.conf.py", env={})
    (tmp_path / "case.py").write_text(
        "from solouvm import Case\n"
        "CASES = [Case(name='smoke'), Case(name='fast', top='other_top', retry=0), 'plain']\n"
    )
    cases = collect.load_cases(cfg)

    assert [case.name for case in cases] == ["smoke", "fast", "plain"]
    assert cases[0].top == "tb_top"
    assert cases[0].uvm_test == "base_test"
    assert cases[0].retry == 2
    assert cases[0].timeout == "5s"
    assert cases[1].top == "other_top"
    assert cases[1].retry == 0
    assert cases[2].top == "tb_top"


def test_load_cases_accepts_dicts(tmp_path: Path):
    cfg = config(tmp_path)
    (tmp_path / "case.py").write_text("CASES = [{'name': 'smoke', 'params': {'N': 2}}]\n")
    cases = collect.load_cases(cfg)
    assert cases[0].name == "smoke"
    assert cases[0].params == {"N": 2}


def test_load_cases_rejects_duplicates(tmp_path: Path):
    cfg = config(tmp_path)
    (tmp_path / "case.py").write_text("CASES = ['smoke', 'smoke']\n")
    with pytest.raises(ConfigError, match="duplicate case name"):
        collect.load_cases(cfg)


def test_load_cases_rejects_bad_entries(tmp_path: Path):
    cfg = config(tmp_path)
    (tmp_path / "case.py").write_text("CASES = [42]\n")
    with pytest.raises(ConfigError, match="expected Case"):
        collect.load_cases(cfg)


def test_load_cases_reports_missing_module(tmp_path: Path):
    cfg = config(tmp_path)
    with pytest.raises(ConfigError, match="case module not found"):
        collect.load_cases(cfg)
