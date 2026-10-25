from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from solouvm.config import expand_vars, find_config, load_config, resolve_config_path
from solouvm.errors import ConfigError


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text))
    return path


def test_find_config_walks_up(tmp_path: Path):
    cfg = write(tmp_path / "proj" / "solouvm.conf.py", "CFG = {'project': 'x'}\n")
    nested = tmp_path / "proj" / "a" / "b"
    nested.mkdir(parents=True)
    assert find_config(nested) == cfg


def test_find_config_missing(tmp_path: Path):
    with pytest.raises(ConfigError, match="cannot find solouvm.conf.py"):
        find_config(tmp_path)


def test_resolve_config_accepts_directory(tmp_path: Path):
    cfg = write(tmp_path / "solouvm.conf.py", "CFG = {}\n")
    assert resolve_config_path(str(tmp_path), tmp_path) == cfg


def test_resolve_config_rejects_non_python(tmp_path: Path):
    target = write(tmp_path / "solouvm.yaml", "CFG: {}\n")
    with pytest.raises(ConfigError, match="must be a .py file"):
        resolve_config_path(str(target), tmp_path)


def test_expand_vars_forms():
    env = {"SIM": "xrun"}
    assert expand_vars("${SIM}", env) == "xrun"
    assert expand_vars("${MISSING:-vcs}", env) == "vcs"
    assert expand_vars(["${SIM}", {"k": "${MISSING:-1}"}], env) == ["xrun", {"k": "1"}]
    with pytest.raises(ConfigError, match="MISSING2"):
        expand_vars("${MISSING2}", env)


def test_load_config_defaults(tmp_path: Path):
    write(tmp_path / "solouvm.conf.py", "CFG = {'project': 'demo', 'top': 'tb_top'}\n")
    cfg = load_config(tmp_path / "solouvm.conf.py", env={})
    assert cfg.project == "demo"
    assert cfg.top == "tb_top"
    assert cfg.workdir == tmp_path / ".solouvm"
    assert cfg.checks["max_uvm_error"] == 0
    assert cfg.seeds == [1] or len(cfg.seeds) == 1


def test_load_config_requires_cfg(tmp_path: Path):
    write(tmp_path / "solouvm.conf.py", "OTHER = {}\n")
    with pytest.raises(ConfigError, match="CFG"):
        load_config(tmp_path / "solouvm.conf.py", env={})


def test_load_config_rejects_bad_types(tmp_path: Path):
    write(tmp_path / "solouvm.conf.py", "CFG = {'project': 3}\n")
    with pytest.raises(ConfigError, match="project"):
        load_config(tmp_path / "solouvm.conf.py", env={})

    write(tmp_path / "b.py", "CFG = {'regress': {'jobs': 0}}\n")
    with pytest.raises(ConfigError, match="jobs"):
        load_config(tmp_path / "b.py", env={})


def test_load_config_expands_env(tmp_path: Path):
    write(
        tmp_path / "solouvm.conf.py",
        """\
        CFG = {'simulator': '${SIM:-vcs}', 'regress': {'seeds': '${SEEDS:-1-2}'}}
        """,
    )
    cfg = load_config(tmp_path / "solouvm.conf.py", env={"SIM": "xrun"})
    assert cfg.simulator == "xrun"
    assert cfg.seeds == [1, 2]
