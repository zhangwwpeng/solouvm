"""Tests for root install.sh and online git-based `solouvm update`."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from solouvm.cli import main
from solouvm.errors import ConfigError
from solouvm.update import (
    DEFAULT_UPDATE_REPO,
    read_install_meta,
    resolve_update_target,
    run_update,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "install.sh"


def _copy_dist_tree(dst: Path) -> Path:
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(INSTALL_SH, dst / "install.sh")
    (dst / "install.sh").chmod(0o755)
    shutil.copytree(
        REPO_ROOT / "solouvm",
        dst / "solouvm",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    if (REPO_ROOT / "doc").is_dir():
        shutil.copytree(REPO_ROOT / "doc", dst / "doc")
    for fname in ("README.md", "LICENSE"):
        src = REPO_ROOT / fname
        if src.is_file():
            shutil.copy2(src, dst / fname)
    return dst


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def test_install_sh_user_precompiles_pyc_and_prints_path_guidance(tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".zshrc").write_text("# empty zshrc\n", encoding="utf-8")

    env = {
        "PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": str(fake_home),
        "SHELL": "/bin/zsh",
    }
    proc = subprocess.run(
        ["bash", str(INSTALL_SH), "--user", "--python", sys.executable],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "precompiled .pyc" in proc.stdout
    assert "NOT in your current PATH" in proc.stdout
    assert 'export PATH="$HOME/.local/bin:$PATH"' in proc.stdout
    assert "~/.zshrc" in proc.stdout and "~/.bashrc" in proc.stdout
    assert "Shell completion:" in proc.stdout
    assert ".local/share/zsh/site-functions" in proc.stdout
    assert ".local/share/bash-completion/completions/solouvm" in proc.stdout

    bin_path = fake_home / ".local" / "bin" / "solouvm"
    lib_dir = fake_home / ".local" / "lib" / "solouvm"
    zsh_comp = fake_home / ".local" / "share" / "zsh" / "site-functions" / "_solouvm"
    bash_comp = fake_home / ".local" / "share" / "bash-completion" / "completions" / "solouvm"
    assert bin_path.is_file()
    assert stat.S_IMODE(bin_path.stat().st_mode) == 0o755
    assert zsh_comp.is_file()
    assert bash_comp.is_file()
    assert stat.S_IMODE(zsh_comp.stat().st_mode) == 0o644
    assert stat.S_IMODE(bash_comp.stat().st_mode) == 0o644
    assert zsh_comp.read_text(encoding="utf-8").splitlines()[0] == "#compdef solouvm"
    assert "complete -F _solouvm_complete solouvm" in bash_comp.read_text(encoding="utf-8")
    assert (lib_dir / "doc" / "verdict.md").is_file()

    meta = read_install_meta(lib_dir)
    assert meta is not None
    assert meta["mode"] == "user"
    assert meta["version"] == "0.1.0"
    assert meta["prefix"] == str(fake_home / ".local")
    assert meta["zsh_completion"] == str(zsh_comp)
    assert meta["bash_completion"] == str(bash_comp)

    # Precompiled bytecode exists
    pyc_files = list((lib_dir / "solouvm" / "__pycache__").glob("cli.*.pyc"))
    assert pyc_files, "expected precompiled cli.*.pyc in installed package"

    # Run installed CLI inside a clean user project without any solouvm library installed there
    user_proj = tmp_path / "clean_proj"
    user_proj.mkdir()
    clean_env = {"PATH": f"{fake_home / '.local' / 'bin'}:/usr/bin:/bin", "HOME": str(fake_home)}

    ver = subprocess.run(
        [str(bin_path), "--version"],
        cwd=user_proj,
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert ver.returncode == 0
    assert ver.stdout.strip() == "solouvm 0.1.0"

    help_p = subprocess.run(
        [str(bin_path), "help", "verdict"],
        cwd=user_proj,
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_p.returncode == 0
    assert "Report Summary" in help_p.stdout

    init_p = subprocess.run(
        [str(bin_path), "init"],
        cwd=user_proj,
        env=clean_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert init_p.returncode == 0, init_p.stdout + init_p.stderr
    assert not (user_proj / "solouvm").exists()

    reg_p = subprocess.run(
        [str(bin_path), "regress"],
        cwd=user_proj,
        env=clean_env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert reg_p.returncode == 0, reg_p.stdout + reg_p.stderr


def test_install_sh_prefix_permissions_atomic_upgrade_uninstall_and_errors(tmp_path: Path) -> None:
    prefix = tmp_path / "opt_solouvm"
    res1 = subprocess.run(
        ["bash", str(INSTALL_SH), "--prefix", str(prefix), "--python", sys.executable],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert res1.returncode == 0, res1.stdout + res1.stderr

    bin_path = prefix / "bin" / "solouvm"
    lib_dir = prefix / "lib" / "solouvm"
    zsh_comp = prefix / "share" / "zsh" / "site-functions" / "_solouvm"
    bash_comp = prefix / "share" / "bash-completion" / "completions" / "solouvm"
    assert stat.S_IMODE(bin_path.stat().st_mode) == 0o755
    cli_mode = stat.S_IMODE((lib_dir / "solouvm" / "cli.py").stat().st_mode)
    assert cli_mode & stat.S_IROTH
    assert zsh_comp.is_file() and (stat.S_IMODE(zsh_comp.stat().st_mode) & stat.S_IROTH)
    assert bash_comp.is_file() and (stat.S_IMODE(bash_comp.stat().st_mode) & stat.S_IROTH)
    assert f'fpath=("{zsh_comp.parent}" $fpath)' in res1.stdout
    assert f'source "{bash_comp}"' in res1.stdout

    # Plant stale file & stale completion content to verify upgrade refreshes atomically
    stale = lib_dir / "solouvm" / "stale_removed.py"
    stale.write_text("# stale\n", encoding="utf-8")
    zsh_comp.write_text("# stale zsh comp\n", encoding="utf-8")
    bash_comp.write_text("# stale bash comp\n", encoding="utf-8")

    res2 = subprocess.run(
        ["bash", str(INSTALL_SH), "--prefix", str(prefix), "--python", sys.executable],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert res2.returncode == 0, res2.stdout + res2.stderr
    assert "Upgrading solouvm 0.1.0 -> 0.1.0" in res2.stdout
    assert not stale.exists()
    assert zsh_comp.read_text(encoding="utf-8").splitlines()[0] == "#compdef solouvm"
    assert "complete -F _solouvm_complete solouvm" in bash_comp.read_text(encoding="utf-8")

    # Uninstall cleans lib, bin, and completions completely
    res_un = subprocess.run(
        ["bash", str(INSTALL_SH), "--uninstall", "--prefix", str(prefix)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_un.returncode == 0, res_un.stdout + res_un.stderr
    assert not lib_dir.exists()
    assert not bin_path.exists()
    assert not zsh_comp.exists()
    assert not bash_comp.exists()

    # Invalid flag combinations and unwritable prefix fail cleanly with exit 2
    bad_combo = subprocess.run(
        ["bash", str(INSTALL_SH), "--user", "--prefix", str(prefix)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_combo.returncode == 2

    ro_parent = tmp_path / "readonly_parent"
    ro_parent.mkdir()
    ro_parent.chmod(0o555)
    try:
        if not os.access(ro_parent, os.W_OK):
            ro_res = subprocess.run(
                [
                    "bash",
                    str(INSTALL_SH),
                    "--prefix",
                    str(ro_parent / "sub"),
                    "--python",
                    sys.executable,
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            assert ro_res.returncode == 2
            assert "write permission" in ro_res.stderr
    finally:
        ro_parent.chmod(0o755)


def test_solouvm_update_defaults_dry_run_and_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert DEFAULT_UPDATE_REPO == "https://github.com/zhangwwpeng/solouvm"
    assert resolve_update_target(mode="system")["prefix"] == "/usr/local"

    monkeypatch.chdir(tmp_path)
    assert main(["--json", "update", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["command"] == "update"
    assert payload["data"]["repo"] == "https://github.com/zhangwwpeng/solouvm"
    assert payload["data"]["dry_run"] is True
    assert len(payload["data"]["commands"]) == 2

    assert main(["--json", "--role", "triage", "update", "--dry-run"]) == 2
    role_err = json.loads(capsys.readouterr().out)
    assert role_err["error"]["code"] == "ROLE_DENIED"

    for bad_argv in (
        ["--json", "update", "--repo=-evil"],
        ["--json", "update", "--ref=-evil"],
        ["--json", "update", "--user", "--prefix", str(tmp_path)],
    ):
        assert main(bad_argv) == 2
        err = json.loads(capsys.readouterr().out)
        assert err["error"]["code"] == "CFG_INVALID"

    with pytest.raises(ConfigError):
        run_update(prefix=str(tmp_path), mode="user", dry_run=True)


def test_solouvm_update_end_to_end_from_local_git_repo_and_meta_reuse(tmp_path: Path) -> None:
    upstream = _copy_dist_tree(tmp_path / "upstream")
    subprocess.run(["git", "init", str(upstream)], check=True, capture_output=True)
    _git(upstream, "config", "user.email", "ci@example.com")
    _git(upstream, "config", "user.name", "CI")
    _git(upstream, "add", ".")
    _git(upstream, "commit", "-m", "v0.1.0")

    prefix = tmp_path / "installed"
    inst = subprocess.run(
        ["bash", str(upstream / "install.sh"), "--prefix", str(prefix), "--python", sys.executable],
        cwd=upstream,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert inst.returncode == 0, inst.stdout + inst.stderr

    init_py = upstream / "solouvm" / "__init__.py"
    orig_init = init_py.read_text(encoding="utf-8")

    # Commit v0.2.0 and tag it
    init_py.write_text(
        orig_init.replace('__version__ = "0.1.0"', '__version__ = "0.2.0"'),
        encoding="utf-8",
    )
    _git(upstream, "add", "solouvm/__init__.py")
    _git(upstream, "commit", "-m", "v0.2.0")
    _git(upstream, "tag", "v0.2.0")

    # Commit v0.3.0 without a tag to verify commit-SHA --ref fallback as well
    init_py.write_text(
        orig_init.replace('__version__ = "0.1.0"', '__version__ = "0.3.0"'),
        encoding="utf-8",
    )
    _git(upstream, "add", "solouvm/__init__.py")
    _git(upstream, "commit", "-m", "v0.3.0")
    sha_030 = _git(upstream, "rev-parse", "HEAD").strip()

    bin_path = prefix / "bin" / "solouvm"
    zsh_comp = prefix / "share" / "zsh" / "site-functions" / "_solouvm"
    bash_comp = prefix / "share" / "bash-completion" / "completions" / "solouvm"
    assert zsh_comp.is_file()
    assert bash_comp.is_file()
    zsh_comp.write_text("# stale before update\n", encoding="utf-8")
    bash_comp.write_text("# stale before update\n", encoding="utf-8")

    # Update to v0.2.0 via tag WITHOUT repeating --prefix/--python (reuses .install-meta)
    upd1 = subprocess.run(
        [str(bin_path), "--json", "update", "--repo", str(upstream), "--ref", "v0.2.0"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert upd1.returncode == 0, upd1.stdout + upd1.stderr
    d1 = json.loads(upd1.stdout)["data"]
    assert d1["previous_version"] == "0.1.0"
    assert d1["version"] == "0.2.0"
    assert d1["mode"] == "prefix"
    assert d1["prefix"] == str(prefix)
    assert zsh_comp.read_text(encoding="utf-8").splitlines()[0] == "#compdef solouvm"
    assert "complete -F _solouvm_complete solouvm" in bash_comp.read_text(encoding="utf-8")

    v2 = subprocess.run([str(bin_path), "--version"], capture_output=True, text=True, check=True)
    assert v2.stdout.strip() == "solouvm 0.2.0"

    # Update to v0.3.0 via raw commit SHA
    upd2 = subprocess.run(
        [str(bin_path), "--json", "update", "--repo", str(upstream), "--ref", sha_030],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert upd2.returncode == 0, upd2.stdout + upd2.stderr
    d2 = json.loads(upd2.stdout)["data"]
    assert d2["previous_version"] == "0.2.0"
    assert d2["version"] == "0.3.0"
    assert zsh_comp.is_file()
    assert bash_comp.is_file()

    v3 = subprocess.run([str(bin_path), "--version"], capture_output=True, text=True, check=True)
    assert v3.stdout.strip() == "solouvm 0.3.0"
