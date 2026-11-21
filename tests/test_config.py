"""Tests for solouvm.config (task 10)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from solouvm.config import (
    DEFAULTS,
    BuildConfig,
    Config,
    deep_merge,
    expand_env,
    find_config,
    load_config,
)
from solouvm.errors import ConfigError
from solouvm.model import Checks, Waiver

MINIMAL = 'CFG = {"simulator": "fake", "top": "tb_top"}\n'


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    write(root / "solouvm.conf.py", MINIMAL)
    return root


def conf(root: Path, body: str) -> Path:
    return write(root / "solouvm.conf.py", body)


def raises(code: str, func, *args, **kwargs) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        func(*args, **kwargs)
    assert info.value.code == code, str(info.value)
    return info.value


# ---------------------------------------------------------------- 查找


def test_find_config_walks_up(project: Path) -> None:
    sub = project / "a" / "b"
    sub.mkdir(parents=True)
    assert find_config(sub) == project / "solouvm.conf.py"


def test_find_config_dir_and_file(project: Path) -> None:
    assert find_config(project) == project / "solouvm.conf.py"
    assert find_config(project / "solouvm.conf.py") == project / "solouvm.conf.py"


def test_find_config_missing_path(tmp_path: Path) -> None:
    err = raises("CFG_NOT_FOUND", find_config, tmp_path / "nope")
    assert "nope" in str(err)


def test_find_config_not_found_anywhere(tmp_path: Path) -> None:
    raises("CFG_NOT_FOUND", find_config, tmp_path, search_parents=False)


def test_load_config_accepts_dir_and_file(project: Path) -> None:
    assert load_config(project).root == project
    assert load_config(project / "solouvm.conf.py").root == project
    raises("CFG_NOT_FOUND", load_config, project / "missing.py")


# ---------------------------------------------------------------- 最小配置与默认值


def test_minimal_config_loads(project: Path) -> None:
    cfg = load_config(project / "solouvm.conf.py")
    assert isinstance(cfg, Config)
    assert cfg.simulator == "fake"
    assert cfg.top == "tb_top"
    assert cfg.project == "proj"
    assert cfg.uvm_test is None
    assert cfg.builds == {"default": BuildConfig()}
    assert cfg.builds["default"].timeout == "2h"
    assert cfg.regress.jobs == 4
    assert cfg.regress.seeds == "random:1"
    assert cfg.regress.retry_on == ["error"]
    assert cfg.run.timeout == "20m"
    assert cfg.checks == Checks()
    assert cfg.provider == "local"
    assert set(cfg.providers) == {"local"}
    assert cfg.providers["local"].transport.kind == "local"
    assert cfg.providers["local"].launcher.kind == "make"
    assert cfg.providers["local"].sync.kind == "none"
    assert cfg.check.uvm_src == "${UVM_HOME}/src"  # where=auto：不展开
    assert cfg.limits.max_jobs == 2000
    assert cfg.history.keep == 50
    assert cfg.doc.lang == "zh"
    assert cfg.triage.pass_policy == "changed"
    assert cfg.uvm.templates is None


def test_defaults_match_doc_table() -> None:
    assert DEFAULTS["regress"]["jobs"] == 4
    assert DEFAULTS["run"]["timeout"] == "20m"
    assert DEFAULTS["checks"]["max_uvm_error"] == 0
    assert DEFAULTS["provider"] == "local"
    assert DEFAULTS["limits"]["max_jobs"] == 2000
    assert DEFAULTS["history"]["keep"] == 50
    assert DEFAULTS["doc"]["lang"] == "zh"
    assert DEFAULTS["triage"]["pass_policy"] == "changed"
    assert DEFAULTS["triage"]["packet_limit"] == 20000
    assert DEFAULTS["check"]["slang"] == "slang"
    assert DEFAULTS["builds"] == {"default": {}}
    assert DEFAULTS["simulator"] is None
    assert DEFAULTS["top"] is None


def test_configs_do_not_share_lists(project: Path) -> None:
    a = load_config(project)
    a.incdirs.append("x")
    a.regress.retry_on.append("timeout")
    b = load_config(project)
    assert b.incdirs == []
    assert b.regress.retry_on == ["error"]
    assert DEFAULTS["incdirs"] == []


def test_full_example_loads(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UVM_HOME", "/tools/uvm")
    conf(
        project,
        """
        from solouvm import Waiver

        CFG = {
            "project": "axi_vip",
            "simulator": "vcs",
            "top": "tb_top",
            "uvm_test": None,
            "incdirs": ["ip/common/include"],
            "defines": ["SIM"],
            "builds": {
                "default": {},
                "gate": {"defines": ["GATE_SIM"], "compile_opts": ["+delay_mode_zero"]},
                "cov": {"coverage": True},
            },
            "regress": {"seeds": "random:1", "jobs": 16, "retry": 1,
                        "wave_on_fail": "per_signature", "max_fail": None, "budget": None},
            "run": {"timeout": "30m", "hang": "10m", "opts": ["+UVM_VERBOSITY=UVM_LOW"],
                    "env": {"AXI_VIP_DATA": "${PROJ_DATA}/axi"}},
            "checks": {"max_uvm_error": 0, "max_uvm_fatal": 0, "max_uvm_warning": None,
                       "fail_regex": ["TEST FAILED"], "pass_regex": [],
                       "env_fail_regex": ["Failed to obtain license"],
                       "waivers": [Waiver(id="AXI/OUTSTANDING", reason="known")]},
            "job": {"pre": "ulimit -c 0", "post": ""},
            "providers": {"farm": {
                "launcher": {"kind": "lsf", "queue": "normal", "resources": "rusage[mem=8000]"},
                "setup": "module load vcs/2025.06"}},
            "check": {"slang": "slang", "uvm_src": "${UVM_HOME}/src", "exclude": ["ip/enc/**"],
                      "where": "local"},
            "case_dirs": ["cases"],
            "system_doc": ["doc/verif/system.md"],
            "triage": {"pass_policy": "changed", "packet_limit": 20000, "debug_dir": "docs/debug"},
            "doc": {"lang": "zh", "out": "docs/cases"},
            "history": {"keep": 50},
            "limits": {"max_jobs": 2000},
            "plugins": [],
        }
        """,
    )
    cfg = load_config(project)
    assert cfg.project == "axi_vip"
    assert cfg.builds["gate"].defines == ["GATE_SIM"]
    assert cfg.builds["gate"].timeout == "2h"
    assert cfg.builds["cov"].coverage is True
    assert set(cfg.builds) == {"default", "gate", "cov"}
    assert cfg.regress.wave_on_fail == "per_signature"
    assert cfg.checks.fail_regex == ("TEST FAILED",)
    assert cfg.checks.waivers[0].id == "AXI/OUTSTANDING"
    assert isinstance(cfg.checks.waivers[0], Waiver)
    assert cfg.providers["farm"].launcher.queue == "normal"
    assert cfg.providers["farm"].transport.kind == "local"
    assert cfg.providers["farm"].setup == "module load vcs/2025.06"
    assert "local" in cfg.providers
    assert cfg.check.uvm_src == "/tools/uvm/src"  # where=local：在 A 上展开
    assert cfg.job.pre == "ulimit -c 0"


# ---------------------------------------------------------------- 错误


def test_unknown_top_key_suggests(project: Path) -> None:
    conf(project, 'CFG = {"simulator": "fake", "top": "t", "regres": {}}\n')
    err = raises("CFG_UNKNOWN_KEY", load_config, project)
    assert "regres" in err.message
    assert "regress" in err.message.replace("'regres'", "")
    assert err.file == str(project / "solouvm.conf.py")


def test_unknown_nested_key_suggests(project: Path) -> None:
    conf(project, 'CFG = {"simulator": "fake", "top": "t", "regress": {"jobz": 3}}\n')
    err = raises("CFG_UNKNOWN_KEY", load_config, project)
    assert "regress.jobz" in err.message
    assert "regress.jobs" in err.message


def test_unknown_key_in_named_build(project: Path) -> None:
    conf(project, 'CFG = {"simulator": "fake", "top": "t", "builds": {"g": {"defnes": []}}}\n')
    err = raises("CFG_UNKNOWN_KEY", load_config, project)
    assert "builds.g.defnes" in err.message
    assert "defines" in err.message


def test_unknown_sim_key(project: Path) -> None:
    conf(project, 'CFG = {"simulator": "fake", "top": "t", "sim": {"buld": "x"}}\n')
    err = raises("CFG_UNKNOWN_KEY", load_config, project)
    assert "sim.build" in err.message


def test_wrong_type_names_key_path(project: Path) -> None:
    conf(project, 'CFG = {"simulator": "fake", "top": "t", "regress": {"jobs": "16"}}\n')
    err = raises("CFG_INVALID", load_config, project)
    assert "regress.jobs" in err.message
    assert "int" in err.message
    assert "'16'" in err.message


@pytest.mark.parametrize(
    "body",
    [
        '"regress": {"jobs": True}',
        '"regress": {"jobs": 0}',
        '"regress": {"seeds": 3}',
        '"regress": {"seeds": "5-1"}',
        '"regress": {"retry_on": ["nope"]}',
        '"regress": {"wave_on_fail": True}',
        '"run": {"timeout": "soon"}',
        '"run": {"env": {"A": 1}}',
        '"defines": "SIM"',
        '"checks": {"fail_regex": ["("]}',
        '"checks": {"waivers": ["x"]}',
        '"check": {"where": "moon"}',
        '"triage": {"pass_policy": "some"}',
        '"regress": 4',
        '"provider": "farm"',
        '"providers": {"farm": {"transport": {"kind": "telnet"}}}',
        '"sim": {"build": 3}',
        '"builds": {"bad name": {}}',
    ],
)
def test_invalid_values(project: Path, body: str) -> None:
    conf(project, f'CFG = {{"simulator": "fake", "top": "t", {body}}}\n')
    raises("CFG_INVALID", load_config, project)


def test_custom_simulator_needs_full_sim(project: Path) -> None:
    conf(project, 'CFG = {"simulator": "mysim", "top": "t"}\n')
    raises("CFG_INVALID", load_config, project)


def test_sim_commands_may_be_lists(project: Path) -> None:
    conf(project, 'CFG = {"simulator": "fake", "top": "t", "sim": {"run": ["{simv}", "-l"]}}\n')
    cfg = load_config(project)
    assert cfg.sim == {"run": ["{simv}", "-l"]}


@pytest.mark.parametrize("missing", ["top", "simulator"])
def test_missing_required(project: Path, missing: str) -> None:
    values = {"simulator": "fake", "top": "t"}
    del values[missing]
    conf(project, f"CFG = {values!r}\n")
    err = raises("CFG_INVALID", load_config, project)
    assert missing in err.message


def test_config_raises(project: Path) -> None:
    conf(project, 'x = 1\nraise RuntimeError("boom")\n')
    err = raises("CFG_LOAD_FAILED", load_config, project)
    assert "boom" in err.message
    assert err.line == 2


def test_config_syntax_error(project: Path) -> None:
    conf(project, "CFG = {\n")
    raises("CFG_LOAD_FAILED", load_config, project)


def test_config_without_cfg(project: Path) -> None:
    conf(project, "X = 1\n")
    err = raises("CFG_LOAD_FAILED", load_config, project)
    assert "must define CFG" in err.message


def test_cfg_not_a_dict(project: Path) -> None:
    conf(project, "CFG = [1]\n")
    raises("CFG_INVALID", load_config, project)


def test_shadowing_solouvm_py(project: Path) -> None:
    write(project / "solouvm.py", "")
    err = raises("CFG_INVALID", load_config, project)
    assert "shadow" in err.message


def test_config_can_import_project_module(project: Path) -> None:
    write(project / "proj_settings_t10.py", 'TOP = "imported_top"\n')
    conf(project, 'from proj_settings_t10 import TOP\nCFG = {"simulator": "fake", "top": TOP}\n')
    assert load_config(project).top == "imported_top"


# ---------------------------------------------------------------- 合并与来源


def test_local_overrides_cfg(project: Path) -> None:
    conf(
        project,
        'CFG = {"simulator": "fake", "top": "t", "regress": {"jobs": 16}, "defines": ["A"]}\n',
    )
    write(project / "solouvm.local.py", 'LOCAL = {"regress": {"jobs": 32}, "defines": ["B"]}\n')
    cfg = load_config(project)
    assert cfg.regress.jobs == 32
    assert cfg.defines == ["B"]
    assert cfg.regress.retry == 0
    assert cfg.sources["regress.jobs"] == "local"
    assert cfg.sources["defines"] == "local"


def test_local_errors_point_at_local_file(project: Path) -> None:
    write(project / "solouvm.local.py", 'LOCAL = {"regress": {"jobs": "x"}}\n')
    err = raises("CFG_INVALID", load_config, project)
    assert err.file == str(project / "solouvm.local.py")


def test_local_without_local_var(project: Path) -> None:
    write(project / "solouvm.local.py", "X = 1\n")
    err = raises("CFG_LOAD_FAILED", load_config, project)
    assert "must define LOCAL" in err.message


def test_local_merges_provider_fields(project: Path) -> None:
    conf(
        project,
        """
        CFG = {"simulator": "fake", "top": "t", "providers": {"farm": {
            "transport": {"kind": "ssh", "host": "a", "workdir": "/w"},
            "launcher": {"kind": "lsf", "queue": "normal"},
            "setup": "module load vcs"}}}
        """,
    )
    write(
        project / "solouvm.local.py",
        """
        LOCAL = {"provider": "farm",
                 "providers": {"farm": {"transport": {"host": "simbox"},
                                        "sync": {"kind": "rsync"}}}}
        """,
    )
    cfg = load_config(project)
    farm = cfg.providers["farm"]
    assert cfg.provider == "farm"
    assert farm.transport.host == "simbox"
    assert farm.transport.kind == "ssh"
    assert farm.transport.workdir == "/w"
    assert farm.launcher.queue == "normal"
    assert farm.sync.kind == "rsync"
    assert farm.setup == "module load vcs"


def test_cli_overrides_local(project: Path) -> None:
    write(project / "solouvm.local.py", 'LOCAL = {"regress": {"jobs": 32}}\n')
    cfg = load_config(project, cli={"regress": {"jobs": 8}})
    assert cfg.regress.jobs == 8
    assert cfg.sources["regress.jobs"] == "cli"
    assert cfg.sources["top"] == "conf"
    assert cfg.sources["run.timeout"] == "default"


def test_cli_values_are_validated(project: Path) -> None:
    err = raises("CFG_UNKNOWN_KEY", load_config, project, cli={"provder": "x"})
    assert "provider" in err.message
    assert err.file is None


def test_deep_merge_semantics() -> None:
    base = {"a": {"x": 1, "y": [1, 2]}, "l": ["A"], "s": 1}
    over = {"a": {"y": [3]}, "l": ["B"], "n": {"k": 1}}
    out = deep_merge(base, over)
    assert out == {"a": {"x": 1, "y": [3]}, "l": ["B"], "s": 1, "n": {"k": 1}}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "l": ["A"], "s": 1}
    out["a"]["x"] = 9
    out["n"]["k"] = 9
    assert base["a"]["x"] == 1
    assert over["n"]["k"] == 1


def test_deep_merge_does_not_share_lists() -> None:
    base = {"l": [1]}
    out = deep_merge(base, {})
    out["l"].append(2)
    assert base["l"] == [1]


# ---------------------------------------------------------------- 环境变量


def test_expand_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/home/me")
    monkeypatch.delenv("NOPE", raising=False)
    monkeypatch.setenv("EMPTY", "")
    assert expand_env("${HOME}/x") == "/home/me/x"
    assert expand_env("$HOME/x") == "/home/me/x"
    assert expand_env("${NOPE:-fallback}") == "fallback"
    assert expand_env("${EMPTY:-fallback}") == "fallback"
    assert expand_env("${HOME:-fallback}") == "/home/me"
    assert expand_env("${EMPTY}") == ""
    assert expand_env("no vars") == "no vars"
    err = raises("CFG_INVALID", expand_env, "${NOPE}")
    assert "NOPE" in err.message


def test_b_side_fields_not_expanded(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROJ_DATA", "/a/data")
    conf(
        project,
        """
        CFG = {"simulator": "fake", "top": "t",
               "incdirs": ["${PROJ_DATA}/inc"], "defines": ["D=${PROJ_DATA}"],
               "builds": {"default": {"compile_opts": ["-f ${PROJ_DATA}/x.f"]}},
               "run": {"env": {"P": "${PROJ_DATA}/x"}, "opts": ["+d=${PROJ_DATA}"]},
               "job": {"pre": "cd ${PROJ_DATA}"},
               "providers": {"farm": {"setup": "source ${PROJ_DATA}/env.sh"}}}
        """,
    )
    cfg = load_config(project)
    assert cfg.run.env == {"P": "${PROJ_DATA}/x"}
    assert cfg.run.opts == ["+d=${PROJ_DATA}"]
    assert cfg.incdirs == ["${PROJ_DATA}/inc"]
    assert cfg.defines == ["D=${PROJ_DATA}"]
    assert cfg.builds["default"].compile_opts == ["-f ${PROJ_DATA}/x.f"]
    assert cfg.job.pre == "cd ${PROJ_DATA}"
    assert cfg.providers["farm"].setup == "source ${PROJ_DATA}/env.sh"


def test_a_side_fields_expanded(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SIMHOST", "box1")
    monkeypatch.delenv("NOPE", raising=False)
    conf(
        project,
        """
        CFG = {"simulator": "fake", "top": "t",
               "providers": {"farm": {"transport": {"kind": "ssh", "host": "${SIMHOST}",
                                                    "workdir": "${NOPE:-/work}",
                                                    "ssh_opts": ["-p", "${SIMPORT:-22}"]}}},
               "system_doc": ["${NOPE:-doc}/system.md"],
               "triage": {"debug_dir": "${NOPE:-docs}/debug"},
               "doc": {"out": "${SIMHOST}/cases"}}
        """,
    )
    cfg = load_config(project)
    transport = cfg.providers["farm"].transport
    assert transport.host == "box1"
    assert transport.workdir == "/work"
    assert transport.ssh_opts == ["-p", "22"]
    assert cfg.system_doc == ["doc/system.md"]
    assert cfg.triage.debug_dir == "docs/debug"
    assert cfg.doc.out == "box1/cases"


def test_a_side_unset_var_errors(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE", raising=False)
    conf(
        project,
        'CFG = {"simulator": "fake", "top": "t", '
        '"providers": {"farm": {"transport": {"kind": "ssh", "host": "${NOPE}"}}}}\n',
    )
    err = raises("CFG_INVALID", load_config, project)
    assert "NOPE" in err.message
    assert "providers.farm.transport.host" in err.message
