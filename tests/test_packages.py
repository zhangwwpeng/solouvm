"""Tests for CFG['packages'] configuration, git fetching, priority/DEPENDS checks, and CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from solouvm.backends import FAKE
from solouvm.cli import main
from solouvm.collect import collect_cases, collect_files, load_filelist
from solouvm.config import load_config
from solouvm.errors import ConfigError, InfraError
from solouvm.gen import write_generated_project_files
from solouvm.ops import validate_project
from solouvm.packages import (
    collect_project_files,
    list_packages,
    package_sync_files,
    resolve_packages,
    sync_packages,
    update_packages,
)
from solouvm.pipeline import clean_project
from solouvm.plan import build_plan
from solouvm.providers import get_provider


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _init_git_repo(repo: Path, files: dict[str, str], message: str = "init") -> str:
    for rel, content in files.items():
        _write(repo / rel, content)
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    _git(repo, "config", "user.email", "ci@example.com")
    _git(repo, "config", "user.name", "CI")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "--short", "HEAD").strip()


def _make_host_project(root: Path, cfg_text: str, filelist_text: str | None = None) -> Path:
    _write(root / "solouvm.conf.py", cfg_text)
    if filelist_text is None:
        filelist_text = (
            'FILES = {\n'
            '    "tb": ["verif/env_pkg.sv"],\n'
            '    "top": ["verif/tb_top.sv"],\n'
            '}\n'
        )
    _write(root / "filelist.py", filelist_text)
    _write(root / "verif/env_pkg.sv", "package env_pkg; endpackage\n")
    _write(root / "verif/tb_top.sv", "module tb_top; initial $finish; endmodule\n")
    _write(
        root / "case.py",
        'from solouvm import Case\nCASES = [Case(name="smoke", uvm_test="smoke_test")]\n',
    )
    return root


def test_packages_config_schema_shorthand_validation_and_local_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("VIP_REF", "v2.0")
    proj = tmp_path / "proj"
    _write(
        proj / "solouvm.conf.py",
        """
CFG = {
    "simulator": "fake",
    "top": "tb_top",
    "packages": {
        "base_uvc": "https://github.com/org/base_uvc.git",
        "apb_agent": {
            "git": "https://github.com/org/apb_agent.git",
            "ref": "${VIP_REF}",
            "subdir": "vip/apb/",
            "priority": 20,
        },
        "local_only": {"path": "../local_pkg", "priority": -5},
    },
}
""",
    )
    _write(
        proj / "solouvm.local.py",
        """
LOCAL = {
    "packages": {
        "apb_agent": {"path": "~/dev/apb_agent"},
        "local_only": {"git": "https://example.com/local_only.git", "ref": "dev"},
    },
}
""",
    )
    cfg = load_config(proj)
    assert cfg.packages["base_uvc"].git == "https://github.com/org/base_uvc.git"
    assert cfg.packages["base_uvc"].path is None
    assert cfg.packages["apb_agent"].path == "~/dev/apb_agent"
    assert cfg.packages["apb_agent"].git is None
    assert cfg.packages["apb_agent"].ref is None
    assert cfg.packages["apb_agent"].subdir == "vip/apb"
    assert cfg.packages["apb_agent"].priority == 20
    assert cfg.packages["local_only"].git == "https://example.com/local_only.git"
    assert cfg.packages["local_only"].ref == "dev"
    assert cfg.packages["local_only"].path is None
    assert cfg.packages["local_only"].priority == -5

    # Invalid cases
    bad_cases: list[tuple[str, str]] = [
        ('"packages": "not-dict"', "CFG_INVALID"),
        ('"packages": {"..": "https://example.com/a.git"}', "CFG_INVALID"),
        ('"packages": {"a/b": "https://example.com/a.git"}', "CFG_INVALID"),
        ('"packages": {"a": {"git": "u", "bogus": 1}}', "CFG_UNKNOWN_KEY"),
        ('"packages": {"a": {"git": "u", "path": "p"}}', "CFG_INVALID"),
        ('"packages": {"a": {"ref": "main"}}', "CFG_INVALID"),
        ('"packages": {"a": {"path": "p", "ref": "main"}}', "CFG_INVALID"),
        ('"packages": {"a": {"git": "u", "priority": True}}', "CFG_INVALID"),
        ('"packages": {"a": {"git": "-evil"}}', "CFG_INVALID"),
        ('"packages": {"a": {"git": "u", "ref": "-bad"}}', "CFG_INVALID"),
        ('"packages": {"a": {"git": "u", "subdir": "../escape"}}', "CFG_INVALID"),
        ('"packages": {"a": {"git": "u", "subdir": "/abs"}}', "CFG_INVALID"),
    ]
    for idx, (pkg_snippet, expected_code) in enumerate(bad_cases):
        bad_dir = tmp_path / f"bad_{idx}"
        _write(
            bad_dir / "solouvm.conf.py",
            f'CFG = {{"simulator": "fake", "top": "tb_top", {pkg_snippet}}}\n',
        )
        with pytest.raises(ConfigError) as exc_info:
            load_config(bad_dir)
        assert exc_info.value.code == expected_code, f"{pkg_snippet}: {exc_info.value}"


def test_git_clone_refs_subdir_existence_only_and_failed_clone_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    upstream = tmp_path / "apb_repo"
    sha1 = _init_git_repo(
        upstream,
        {
            "vip/apb/filelist.py": (
                'PRIORITY = 30\n'
                'FILES = {\n'
                '    "tb": ["+incdir+inc", "-f vip.f", "src/apb_pkg.sv"],\n'
                '    "top": ["ut/ut_top.sv"],\n'
                '}\n'
            ),
            "vip/apb/vip.f": "src/apb_if.sv\n",
            "vip/apb/inc/apb_macros.svh": "`define APB_W 32\n",
            "vip/apb/src/apb_if.sv": "interface apb_if; endinterface\n",
            "vip/apb/src/apb_pkg.sv": "package apb_pkg; endpackage\n",
            "vip/apb/ut/ut_top.sv": "module ut_top; endmodule\n",
        },
        message="v1",
    )
    _git(upstream, "tag", "v1.0")
    _write(upstream / "vip/apb/src/apb_extra.sv", "module apb_extra; endmodule\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "v2")

    # Standalone package repo still includes its own top group untouched
    monkeypatch.chdir(upstream / "vip/apb")
    standalone_fl = collect_files(upstream / "vip/apb", load_filelist(upstream / "vip/apb"))
    assert "ut/ut_top.sv" in standalone_fl.groups["top"]
    monkeypatch.chdir(tmp_path)

    # Pin host to raw commit SHA sha1 + subdir vip/apb
    proj = _make_host_project(
        tmp_path / "proj",
        f"""
CFG = {{
    "simulator": "fake",
    "top": "tb_top",
    "packages": {{
        "apb_agent": {{
            "git": {str(upstream)!r},
            "ref": {sha1!r},
            "subdir": "vip/apb",
        }},
    }},
}}
""",
    )
    cfg = load_config(proj)
    flist = collect_project_files(cfg)
    assert flist.groups["tb"] == [
        ".solouvm/packages/apb_agent/vip/apb/src/apb_if.sv",
        ".solouvm/packages/apb_agent/vip/apb/src/apb_pkg.sv",
        "verif/env_pkg.sv",
    ]
    assert flist.incdirs["tb"] == [".solouvm/packages/apb_agent/vip/apb/inc"]
    assert "ut_top" not in " ".join(flist.files)
    assert not (proj / ".solouvm/packages/apb_agent/vip/apb/src/apb_extra.sv").exists()
    assert flist.entries[0].source.startswith("pkg:apb_agent:")

    # Existence-only reuse: delete upstream AND alter git config -> existing directory is reused!
    shutil.rmtree(upstream)
    _write(
        proj / "solouvm.conf.py",
        """
CFG = {
    "simulator": "fake",
    "top": "tb_top",
    "packages": {
        "apb_agent": {
            "git": "/nonexistent/deleted_repo.git",
            "ref": "v9.9",
            "subdir": "vip/apb",
        },
    },
}
""",
    )
    cfg2 = load_config(proj)
    flist2 = collect_project_files(cfg2)
    assert flist2.groups["tb"] == flist.groups["tb"]

    # Failed clone leaves no partial target directory behind
    fail_proj = _make_host_project(
        tmp_path / "fail_proj",
        """
CFG = {
    "simulator": "fake",
    "top": "tb_top",
    "packages": {
        "broken": {"git": "/nonexistent/missing_repo.git"},
    },
}
""",
    )
    with pytest.raises(InfraError) as exc_info:
        collect_project_files(load_config(fail_proj))
    assert exc_info.value.code == "PROVIDER_FAILED"
    assert not (fail_proj / ".solouvm/packages/broken").exists()


def test_priority_ordering_and_strict_depends_checks(tmp_path: Path) -> None:
    base_repo = tmp_path / "base_uvc"
    _init_git_repo(
        base_repo,
        {
            "filelist.py": 'PRIORITY = 20\nFILES = {"tb": ["base_pkg.sv"]}\n',
            "base_pkg.sv": "package base_pkg; endpackage\n",
        },
    )
    apb_repo = tmp_path / "apb_agent"
    _init_git_repo(
        apb_repo,
        {
            "filelist.py": (
                'PRIORITY = 10\n'
                'DEPENDS = ["base_uvc"]\n'
                'FILES = {"tb": ["apb_pkg.sv"]}\n'
            ),
            "apb_pkg.sv": "package apb_pkg; endpackage\n",
        },
    )
    tie_repo = tmp_path / "tie_pkg"
    _init_git_repo(
        tie_repo,
        {
            "filelist.py": 'PRIORITY = 20\nFILES = {"tb": ["tie_pkg.sv"]}\n',
            "tie_pkg.sv": "package tie_pkg; endpackage\n",
        },
    )

    # Priority inversion (apb has 10 < base 20 while depending on base) -> CFG_INVALID
    inv_proj = _make_host_project(
        tmp_path / "inv_proj",
        f"""
CFG = {{
    "simulator": "fake",
    "top": "tb_top",
    "packages": {{
        "base_uvc": {{"path": {str(base_repo)!r}}},
        "apb_agent": {{"path": {str(apb_repo)!r}}},
    }},
}}
""",
    )
    with pytest.raises(ConfigError) as exc_info:
        resolve_packages(load_config(inv_proj))
    assert exc_info.value.code == "CFG_INVALID"
    assert "not been loaded yet" in exc_info.value.message

    # Host override fixes apb_agent priority to 50 (> 20), and tie-breaks priority 20 by CFG order
    ok_proj = _make_host_project(
        tmp_path / "ok_proj",
        f"""
CFG = {{
    "simulator": "fake",
    "top": "tb_top",
    "packages": {{
        "tie_pkg": {{"path": {str(tie_repo)!r}}},
        "base_uvc": {{"path": {str(base_repo)!r}}},
        "apb_agent": {{"path": {str(apb_repo)!r}, "priority": 50}},
    }},
}}
""",
        filelist_text=(
            'DEPENDS = ["apb_agent"]\n'
            'FILES = {"tb": ["verif/env_pkg.sv"], "top": ["verif/tb_top.sv"]}\n'
        ),
    )
    resolved = resolve_packages(load_config(ok_proj))
    assert [p.name for p in resolved] == ["tie_pkg", "base_uvc", "apb_agent"]
    merged = collect_project_files(load_config(ok_proj))
    assert [Path(p).name for p in merged.groups["tb"]] == [
        "tie_pkg.sv",
        "base_pkg.sv",
        "apb_pkg.sv",
        "env_pkg.sv",
    ]

    # Host DEPENDS on unconfigured package -> CFG_INVALID
    bad_host_proj = _make_host_project(
        tmp_path / "bad_host",
        'CFG = {"simulator": "fake", "top": "tb_top"}\n',
        filelist_text='DEPENDS = ["missing_vip"]\nFILES = {"top": ["verif/tb_top.sv"]}\n',
    )
    with pytest.raises(ConfigError) as host_exc:
        collect_project_files(load_config(bad_host_proj))
    assert host_exc.value.code == "CFG_INVALID"

    # Missing dep in CFG['packages'] -> CFG_INVALID
    miss_dep_proj = _make_host_project(
        tmp_path / "miss_dep",
        (
            f'CFG = {{"simulator": "fake", "top": "tb_top", '
            f'"packages": {{"apb": {{"path": {str(apb_repo)!r}}}}}}}\n'
        ),
    )
    with pytest.raises(ConfigError) as miss_exc:
        resolve_packages(load_config(miss_dep_proj))
    assert miss_exc.value.code == "CFG_INVALID"

    # Self-dependency -> CFG_INVALID
    self_pkg = tmp_path / "self_pkg"
    _write(self_pkg / "filelist.py", 'DEPENDS = ["self_pkg"]\nFILES = {"tb": ["a.sv"]}\n')
    _write(self_pkg / "a.sv", "package a; endpackage\n")
    self_proj = _make_host_project(
        tmp_path / "self_proj",
        (
            f'CFG = {{"simulator": "fake", "top": "tb_top", '
            f'"packages": {{"self_pkg": {{"path": {str(self_pkg)!r}}}}}}}\n'
        ),
    )
    with pytest.raises(ConfigError) as self_exc:
        resolve_packages(load_config(self_proj))
    assert self_exc.value.code == "CFG_INVALID"

    # Mutual cycle -> CFG_INVALID regardless of priority
    cyc_a = tmp_path / "cyc_a"
    cyc_b = tmp_path / "cyc_b"
    _write(cyc_a / "filelist.py", 'PRIORITY = 10\nDEPENDS = ["cyc_b"]\nFILES = {"tb": ["a.sv"]}\n')
    _write(cyc_a / "a.sv", "package a; endpackage\n")
    _write(cyc_b / "filelist.py", 'PRIORITY = 20\nDEPENDS = ["cyc_a"]\nFILES = {"tb": ["b.sv"]}\n')
    _write(cyc_b / "b.sv", "package b; endpackage\n")
    cyc_proj = _make_host_project(
        tmp_path / "cyc_proj",
        (
            f'CFG = {{"simulator": "fake", "top": "tb_top", '
            f'"packages": {{"cyc_a": {{"path": {str(cyc_a)!r}}}, '
            f'"cyc_b": {{"path": {str(cyc_b)!r}}}}}}}\n'
        ),
    )
    with pytest.raises(ConfigError) as cyc_exc:
        resolve_packages(load_config(cyc_proj))
    assert cyc_exc.value.code == "CFG_INVALID"

    # Missing filelist.py inside package -> FILES_MISSING
    no_fl_pkg = tmp_path / "no_fl"
    no_fl_pkg.mkdir()
    no_fl_proj = _make_host_project(
        tmp_path / "no_fl_proj",
        (
            f'CFG = {{"simulator": "fake", "top": "tb_top", '
            f'"packages": {{"no_fl": {{"path": {str(no_fl_pkg)!r}}}}}}}\n'
        ),
    )
    with pytest.raises(ConfigError) as no_fl_exc:
        resolve_packages(load_config(no_fl_proj))
    assert no_fl_exc.value.code == "FILES_MISSING"


def test_e2e_regress_validate_manifest_clean_and_pkg_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    upstream = tmp_path / "apb_vip"
    c1 = _init_git_repo(
        upstream,
        {
            "filelist.py": (
                'PRIORITY = 10\n'
                'FILES = {"tb": ["+incdir+inc", "src/apb_pkg.sv"]}\n'
            ),
            "inc/apb_hdr.svh": "// header v1\n",
            "src/apb_pkg.sv": "package apb_pkg; endpackage\n",
        },
        message="v1",
    )
    _write(upstream / "inc/apb_hdr.svh", "// header v2\n")
    _git(upstream, "add", ".")
    c2 = _git(upstream, "commit", "-m", "v2")
    _ = c2
    c2_short = _git(upstream, "rev-parse", "--short", "HEAD").strip()
    assert c2_short != c1

    # Local path package
    helper_dir = tmp_path / "local_helper"
    _write(helper_dir / "filelist.py", 'PRIORITY = 20\nFILES = {"tb": ["helper_pkg.sv"]}\n')
    _write(helper_dir / "helper_pkg.sv", "package helper_pkg; endpackage\n")

    proj = _make_host_project(
        tmp_path / "proj",
        f"""
CFG = {{
    "simulator": "fake",
    "top": "tb_top",
    "packages": {{
        "apb_vip": {{"git": {str(upstream)!r}, "ref": "main"}},
        "local_helper": {{"path": {str(helper_dir)!r}}},
    }},
}}
""",
    )
    _write(
        proj / "case.py",
        """
from solouvm import Case, Expect

CASES = [
    Case(
        name="smoke",
        uvm_test="smoke_test",
        owner="wp",
        stage="ready",
        intent="verify apb package integration",
        method="fake sim pass",
        spec_refs=["APB-1"],
        expect=[Expect(sim_time_min="1ns")],
    )
]
""",
    )

    # pkg list before sync (read-only, allowed in triage, does NOT clone)
    assert main(["-c", str(proj), "--role", "triage", "--json", "pkg", "list"]) == 0
    before = json.loads(capsys.readouterr().out)["data"]["packages"]
    assert before[0]["name"] == "apb_vip"
    assert before[0]["exists"] is False
    assert not (proj / ".solouvm/packages/apb_vip").exists()

    # pkg sync denied under --role triage
    assert main(["-c", str(proj), "--role", "triage", "--json", "pkg", "sync"]) == 2
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "ROLE_DENIED"

    for command in (["files"], ["validate"], ["regress", "--dry-run"]):
        assert main(["-c", str(proj), "--json", *command]) == 2
        capsys.readouterr()
        assert not (proj / ".solouvm/packages/apb_vip").exists()
    assert main(["-c", str(proj), "--json", "pkg", "sync"]) == 0
    capsys.readouterr()
    assert main(["-c", str(proj), "--json", "files"]) == 0
    files_data = json.loads(capsys.readouterr().out)["data"]
    assert files_data["entries"][0]["path"] == ".solouvm/packages/apb_vip/src/apb_pkg.sv"
    assert files_data["entries"][0]["source"].startswith("pkg:apb_vip:")
    assert files_data["entries"][1]["outside_root"] is True

    # pkg sync now skips existing apb_vip
    assert main(["-c", str(proj), "--json", "pkg", "sync"]) == 0
    sync_data = json.loads(capsys.readouterr().out)["data"]
    assert sync_data["packages"][0]["status"] == "cached"
    assert sync_data["resolved_order"] == ["apb_vip", "local_helper"]

    # pkg list shows ready + commit
    assert main(["-c", str(proj), "--json", "pkg", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)["data"]["packages"]
    assert listed[0]["exists"] is True
    assert listed[0]["filelist_ok"] is True
    assert listed[0]["commit"] == c2_short

    # Add a third commit upstream and run pkg update
    _write(upstream / "inc/apb_hdr.svh", "// header v3\n")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "v3")
    c3_short = _git(upstream, "rev-parse", "--short", "HEAD").strip()
    assert main(["-c", str(proj), "--json", "pkg", "update", "apb_vip"]) == 0
    upd = json.loads(capsys.readouterr().out)["data"]
    assert upd["packages"][0]["status"] == "updated"
    assert upd["packages"][0]["commit"] == c3_short
    assert "v3" in (proj / ".solouvm/packages/apb_vip/inc/apb_hdr.svh").read_text(encoding="utf-8")

    # Failed pkg update preserves existing working directory atomically
    _write(
        proj / "solouvm.local.py",
        'LOCAL = {"packages": {"apb_vip": {"git": "/nonexistent/upstream.git"}}}\n',
    )
    with pytest.raises(InfraError):
        update_packages(load_config(proj), names=["apb_vip"])
    assert (proj / ".solouvm/packages/apb_vip/filelist.py").is_file()
    (proj / "solouvm.local.py").unlink()

    # Validate + regress end-to-end
    cfg = load_config(proj)
    assert validate_project(proj).ok
    assert main(["-c", str(proj), "--json", "regress"]) == 0
    raw_reg = capsys.readouterr().out
    env_start = raw_reg.rfind("\n{")
    reg_payload = json.loads(raw_reg[env_start + 1 :] if env_start >= 0 else raw_reg)
    assert reg_payload["ok"] is True

    # Human CLI output for pkg list & sync
    assert main(["-c", str(proj), "pkg", "list"]) == 0
    list_text = capsys.readouterr().out
    assert "apb_vip" in list_text and "local_helper" in list_text
    assert main(["-c", str(proj), "pkg", "sync"]) == 0
    sync_text = capsys.readouterr().out
    assert "cached" in sync_text and "apb_vip" in sync_text

    # Remote sync manifest includes cloned package files & headers but excludes .git
    sync_list = package_sync_files(proj, cfg)
    assert ".solouvm/packages/apb_vip/inc/apb_hdr.svh" in sync_list
    assert ".solouvm/packages/apb_vip/src/apb_pkg.sv" in sync_list
    assert all("/.git/" not in p and not p.endswith("/.git") for p in sync_list)

    plan = build_plan(
        cases=collect_cases(cfg),
        config=cfg,
        selection={},
        run_id="r_pkg_test",
    )
    gen_manifest = write_generated_project_files(
        config=cfg,
        plan=plan,
        files=collect_project_files(cfg),
        backend=FAKE,
    )
    assert ".solouvm/packages/apb_vip/inc/apb_hdr.svh" in gen_manifest

    # Duplicate SV module/package across package and host caught by validate_project
    dup_host = _write(proj / "verif/dup_apb.sv", "package apb_pkg; endpackage\n")
    _write(
        proj / "filelist.py",
        'FILES = {"tb": ["verif/env_pkg.sv", "verif/dup_apb.sv"], "top": ["verif/tb_top.sv"]}\n',
    )
    dup_rep = validate_project(proj)
    assert any("apb_pkg" in issue.message for issue in dup_rep.warnings)
    dup_host.unlink()
    _write(
        proj / "filelist.py",
        'FILES = {"tb": ["verif/env_pkg.sv"], "top": ["verif/tb_top.sv"]}\n',
    )

    # Prune clean keeps .solouvm/packages; clean --all removes it
    provider = get_provider(cfg)
    clean_project(config=cfg, provider=provider)
    assert (proj / ".solouvm/packages/apb_vip").is_dir()
    clean_project(config=cfg, provider=provider, all_files=True)
    assert not (proj / ".solouvm/packages").exists()

    # After clean --all, sync_packages restores missing git package
    resync = sync_packages(cfg)
    assert resync["packages"][0]["status"] == "cloned"
    assert list_packages(cfg)[0]["exists"] is True
