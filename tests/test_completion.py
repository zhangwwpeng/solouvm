"""Tests for static shell completion generation, CLI wiring, and real shell behavior."""

from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

from solouvm.backends import BACKENDS
from solouvm.cli import main
from solouvm.cli_parser import build_parser
from solouvm.completion import SUPPORTED_SHELLS, generate_completion
from solouvm.errors import ConfigError
from solouvm.ops import HELP_TOPICS, SCHEMAS


def _bash_complete(script_path: Path, words: list[str], cwd: Path | None = None) -> list[str]:
    """Run ``_solouvm_complete`` under macOS ``/bin/bash`` (3.2) and return sorted replies."""
    bash_bin = "/bin/bash" if Path("/bin/bash").is_file() else (shutil.which("bash") or "bash")
    words_lit = " ".join(shlex.quote(w) for w in words)
    cword = len(words) - 1
    runner = "\n".join(
        [
            "set -e",
            f"source {shlex.quote(str(script_path))}",
            f"COMP_WORDS=({words_lit})",
            f"COMP_CWORD={cword}",
            "COMPREPLY=()",
            "_solouvm_complete",
            'printf "%s\\n" "${COMPREPLY[@]}"',
        ]
    )
    proc = subprocess.run(
        [bash_bin, "-c", runner],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line != ""]


def test_generate_completion_covers_commands_choices_and_errors() -> None:
    assert SUPPORTED_SHELLS == ("bash", "zsh")

    with pytest.raises(ConfigError) as exc_info:
        generate_completion("fish")
    assert exc_info.value.code == "CFG_INVALID"

    bash_script = generate_completion("bash")
    zsh_script = generate_completion("zsh")

    assert bash_script.startswith("# bash completion for solouvm")
    assert bash_script.rstrip().endswith("complete -F _solouvm_complete solouvm")
    assert zsh_script.splitlines()[0] == "#compdef solouvm"

    parser = build_parser(defaultdict(lambda: lambda _a: 0))
    sub_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    for top_name, sub_parser in sub_action.choices.items():
        assert top_name in bash_script
        assert top_name in zsh_script
        nested = next(
            (a for a in sub_parser._actions if isinstance(a, argparse._SubParsersAction)),
            None,
        )
        if nested is not None:
            for sub_name in nested.choices:
                assert sub_name in bash_script
                assert sub_name in zsh_script

    for token in (
        *BACKENDS.keys(),
        *HELP_TOPICS.keys(),
        *SCHEMAS.keys(),
        "agent",
        "env",
        "test",
        "seq",
        "vseq",
        "bash",
        "zsh",
        "text",
        "json",
        "junit",
        "html",
        "local",
        "remote",
        "zh",
        "en",
        "name",
        "duration",
        "main",
        "triage",
        "real_pass",
        "false_pass",
    ):
        assert token in bash_script
        assert token in zsh_script


def test_cli_completion_subcommand_json_and_role(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["completion", "bash"]) == 0
    bash_out = capsys.readouterr().out
    assert "complete -F _solouvm_complete solouvm" in bash_out
    assert bash_out.endswith("\n")

    assert main(["completion", "zsh"]) == 0
    zsh_out = capsys.readouterr().out
    assert zsh_out.splitlines()[0] == "#compdef solouvm"
    assert zsh_out.endswith("\n")

    assert main(["--json", "completion", "zsh"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == 1
    assert payload["ok"] is True
    assert payload["command"] == "completion"
    assert payload["data"]["shell"] == "zsh"
    assert "#compdef solouvm" in payload["data"]["script"]

    # Read-only under --role triage
    assert main(["--role", "triage", "completion", "bash"]) == 0
    assert "complete -F _solouvm_complete solouvm" in capsys.readouterr().out


def test_completion_scripts_pass_real_shell_syntax(tmp_path: Path) -> None:
    bash_file = tmp_path / "solouvm.bash"
    zsh_file = tmp_path / "_solouvm"
    bash_file.write_text(generate_completion("bash"), encoding="utf-8")
    zsh_file.write_text(generate_completion("zsh"), encoding="utf-8")

    sys_bash = Path("/bin/bash")
    assert sys_bash.is_file() or shutil.which("bash") is not None
    if sys_bash.is_file():
        subprocess.run([str(sys_bash), "-n", str(bash_file)], check=True)
    if path_bash := shutil.which("bash"):
        subprocess.run([path_bash, "-n", str(bash_file)], check=True)

    zsh_bin = shutil.which("zsh")
    assert zsh_bin is not None
    subprocess.run([zsh_bin, "-n", str(zsh_file)], check=True)


def test_bash_completion_live_in_bash_32(tmp_path: Path) -> None:
    script = tmp_path / "solouvm.bash"
    script.write_text(generate_completion("bash"), encoding="utf-8")

    # Root subcommands
    root_cmds = _bash_complete(script, ["solouvm", ""])
    assert {"init", "regress", "uvm", "triage", "update", "completion"} <= set(root_cmds)

    # Nested subcommands
    assert set(_bash_complete(script, ["solouvm", "uvm", ""])) == {"sync", "diff"}
    assert set(_bash_complete(script, ["solouvm", "pkg", ""])) == {"list", "sync", "update"}
    assert set(_bash_complete(script, ["solouvm", "triage", ""])) == {
        "plan",
        "packet",
        "write",
        "review",
        "status",
    }

    # Global option consuming value before subcommand group
    assert set(_bash_complete(script, ["solouvm", "-c", "foo.py", "uvm", ""])) == {
        "sync",
        "diff",
    }

    # Option value choices (space-separated and --opt=val forms)
    assert set(_bash_complete(script, ["solouvm", "report", "--format", ""])) == {
        "text",
        "json",
        "junit",
        "html",
    }
    assert set(_bash_complete(script, ["solouvm", "report", "--format=j"])) == {
        "--format=json",
        "--format=junit",
    }
    assert set(_bash_complete(script, ["solouvm", "init", "--simulator", "v"])) == {
        "vcs",
        "verilator",
    }

    # Positional choices
    assert set(_bash_complete(script, ["solouvm", "new", ""])) == {
        "agent",
        "env",
        "test",
        "seq",
        "vseq",
    }
    assert set(HELP_TOPICS.keys()) <= set(_bash_complete(script, ["solouvm", "help", ""]))
    assert set(_bash_complete(script, ["solouvm", "schema", ""])) == set(SCHEMAS.keys())
    assert set(_bash_complete(script, ["solouvm", "completion", ""])) == {"bash", "zsh"}

    # Subcommand option flags
    upd_opts = _bash_complete(script, ["solouvm", "update", "--"])
    assert {"--repo", "--ref", "--user", "--system", "--prefix", "--python", "--dry-run"} <= set(
        upd_opts
    )

    # Value starting with '-' after a value-taking option must not trigger flags or -c completion
    assert _bash_complete(script, ["solouvm", "bug", "add", "smoke", "--title", "-"]) == []
    assert _bash_complete(script, ["solouvm", "bug", "add", "smoke", "--title", "-c", ""]) == []

    # Directory path completion for --prefix
    work = tmp_path / "work"
    sub_dir = work / "opt_dir"
    sub_dir.mkdir(parents=True)
    (work / "plain.txt").write_text("hi\n", encoding="utf-8")
    dir_replies = _bash_complete(script, ["solouvm", "update", "--prefix", ""], cwd=work)
    assert "opt_dir" in dir_replies
    assert "plain.txt" not in dir_replies


def test_zsh_completion_live_sourcing(tmp_path: Path) -> None:
    zsh_file = tmp_path / "_solouvm"
    zsh_file.write_text(generate_completion("zsh"), encoding="utf-8")

    zsh_bin = shutil.which("zsh")
    assert zsh_bin is not None

    # 1. Source when compdef is not defined yet: succeeds cleanly and defines _solouvm
    proc1 = subprocess.run(
        [zsh_bin, "-fc", f"source {shlex.quote(str(zsh_file))} && (( $+functions[_solouvm] ))"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc1.returncode == 0, proc1.stdout + proc1.stderr

    # 2. Source when compdef exists: registers `_solouvm` for `solouvm`
    proc2 = subprocess.run(
        [
            zsh_bin,
            "-fc",
            (
                'registered=""; '
                'compdef() { registered="$*"; }; '
                f"source {shlex.quote(str(zsh_file))} && "
                'print -r -- "$registered"'
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc2.returncode == 0, proc2.stdout + proc2.stderr
    assert proc2.stdout.strip() == "_solouvm solouvm"
