"""Tests for solouvm.providers (task 16 + M2/M4 ssh, lsf, rsync, git, plugins, prefilter)."""

from __future__ import annotations

import inspect
import json
import os
import shlex
import shutil
import subprocess
import textwrap
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from solouvm import ui
from solouvm.backends import get_backend
from solouvm.config import load_config
from solouvm.errors import ConfigError, InfraError
from solouvm.judge.prefilter import extract_keywords, extract_literal, keywords_for
from solouvm.judge.rules import Verdict, decide
from solouvm.judge.scan import scan
from solouvm.model import Case, Checks, Expect, Facts, Job, Plan, Waiver
from solouvm.providers import (
    GitSync,
    LocalTransport,
    LsfLauncher,
    MakeLauncher,
    NoneSync,
    Provider,
    RsyncSync,
    SshTransport,
    get_provider,
    known_kinds,
    load_plugins,
    register_launcher,
    register_sync,
    register_transport,
)
from solouvm.providers.base import ProviderRegistry


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def _make_plan(run_id: str = "20261118-120000-abcd") -> Plan:
    return Plan(
        run_id=run_id,
        created_at=datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc),
        parent_run=None,
        provider="local",
        selection={"case": [], "tag": []},
        builds={"default": "a1b2c3d4"},
        jobs=[
            Job(
                job_id="smoke__1",
                case="smoke",
                seed=1,
                build="default",
                uvm_test="smoke_test",
                params={},
                opts=[],
                env={},
                timeout_s=60.0,
                hang_s=None,
                retry=0,
                expect_fail=False,
                tags=(),
                dir=f".solouvm/runs/{run_id}/smoke__1",
            )
        ],
    )


def _install_stub(bin_dir: Path, name: str, script: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / name
    header = "#!/usr/bin/env bash\nset -euo pipefail\n"
    target.write_text(header + textwrap.dedent(script), encoding="utf-8")
    target.chmod(0o755)
    return target


# ---------------------------------------------------------------- get_provider / local


def test_get_provider_default_local(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    _write(proj / "solouvm.conf.py", 'CFG = {"simulator": "fake", "top": "tb_top"}\n')
    cfg = load_config(proj)
    prov = get_provider(cfg)
    assert isinstance(prov, Provider)
    assert prov.name == "local"
    assert isinstance(prov.transport, LocalTransport)
    assert isinstance(prov.launcher, MakeLauncher)
    assert isinstance(prov.sync, NoneSync)
    assert prov.setup == ""


def test_get_provider_cli_override(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    _write(
        proj / "solouvm.conf.py",
        """
        CFG = {
            "simulator": "fake",
            "top": "tb_top",
            "providers": {
                "farm": {
                    "launcher": {"kind": "lsf", "queue": "normal"},
                    "setup": "module load vcs",
                }
            },
        }
        """,
    )
    cfg = load_config(proj)
    assert get_provider(cfg).name == "local"
    farm = get_provider(cfg, "farm")
    assert farm.name == "farm"
    assert isinstance(farm.launcher, LsfLauncher)
    assert farm.launcher.queue == "normal"
    assert farm.setup == "module load vcs"

    cli_cfg = load_config(proj, cli={"provider": "farm"})
    assert get_provider(cli_cfg).name == "farm"


def test_get_provider_unknown_raises_cfg_invalid(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    _write(proj / "solouvm.conf.py", 'CFG = {"simulator": "fake", "top": "tb_top"}\n')
    cfg = load_config(proj)
    with pytest.raises(ConfigError) as exc_info:
        get_provider(cfg, "missing_box")
    assert exc_info.value.code == "CFG_INVALID"
    assert "local" in exc_info.value.message


# ---------------------------------------------------------------- LocalTransport


def test_local_exec_success_and_failure(tmp_path: Path) -> None:
    t = LocalTransport()
    assert t.exec(["true"], cwd=str(tmp_path)) == 0

    with pytest.raises(InfraError) as exc_info:
        t.exec(["false"], cwd=str(tmp_path))
    assert exc_info.value.code == "PROVIDER_FAILED"
    assert "false" in exc_info.value.message
    assert "1" in exc_info.value.message

    assert t.exec(["false"], cwd=str(tmp_path), check=False) == 1

    with pytest.raises(InfraError) as missing_exc:
        t.exec(["definitely-no-such-solouvm-cmd"], cwd=str(tmp_path))
    assert missing_exc.value.code == "PROVIDER_FAILED"
    assert "definitely-no-such-solouvm-cmd" in missing_exc.value.message


def test_local_exec_output_and_capture(tmp_path: Path) -> None:
    t = LocalTransport(root=tmp_path)
    rc, out = t.exec_output(["sh", "-c", "echo hi; echo err >&2"], cwd=".")
    assert rc == 0
    assert "hi" in out and "err" in out

    rc2, stdout, stderr = t.exec_capture(["sh", "-c", "echo out; echo bad >&2"], cwd=".")
    assert rc2 == 0
    assert stdout.strip() == "out"
    assert stderr.strip() == "bad"


def test_local_spawn_terminate(tmp_path: Path) -> None:
    t = LocalTransport()
    proc = t.spawn(["sleep", "10"], cwd=str(tmp_path))
    assert proc.pid > 0
    assert proc.poll() is None
    proc.terminate()
    rc = proc.wait(timeout=5)
    assert rc != 0


def test_local_read_lines_streaming_and_filters(tmp_path: Path) -> None:
    log = tmp_path / "sim.log"
    log.write_bytes(b"header\n\nUVM_INFO a.sv(1): ok\n\nUVM_ERROR b.sv(9): bad \xff\nfooter\n")
    t = LocalTransport()

    assert "yield" in inspect.getsource(LocalTransport.read_lines)
    gen = t.read_lines(str(log))
    assert inspect.isgenerator(gen)

    all_lines = list(gen)
    assert all_lines == [
        (1, "header"),
        (2, ""),
        (3, "UVM_INFO a.sv(1): ok"),
        (4, ""),
        (5, "UVM_ERROR b.sv(9): bad \ufffd"),
        (6, "footer"),
    ]
    assert list(t.read_lines(str(log), keywords=["UVM_ERROR"])) == [
        (5, "UVM_ERROR b.sv(9): bad \ufffd")
    ]
    assert list(t.read_lines(str(log), lines=(3, 5))) == [
        (3, "UVM_INFO a.sv(1): ok"),
        (4, ""),
        (5, "UVM_ERROR b.sv(9): bad \ufffd"),
    ]
    assert t.line_count(str(log)) == 6
    assert t.line_count(str(tmp_path / "absent.log")) is None


def test_local_read_many_put_exists_rm_tree(tmp_path: Path) -> None:
    t = LocalTransport()
    target = tmp_path / "nested" / "dir" / "a.facts"
    assert t.exists(str(target)) is False
    t.put(str(target), b"state=done\n")
    assert t.exists(str(target)) is True

    other = tmp_path / "nested" / "dir" / "b.facts"
    t.put(str(other), b"state=running\n")
    (tmp_path / "nested" / "dir" / "sub.facts").mkdir()  # directory should be skipped

    many = t.read_many(str(tmp_path / "nested" / "dir" / "*.facts"))
    assert many == {
        str(target): "state=done\n",
        str(other): "state=running\n",
    }

    t.rm_tree(str(tmp_path / "nested"))
    assert t.exists(str(tmp_path / "nested")) is False


def test_ui_command_log_integration(tmp_path: Path) -> None:
    log_file = tmp_path / "solouvm.log"
    ui.reset()
    ui.setup(verbose=2, log_file=log_file)
    try:
        t = LocalTransport()
        t.exec(["true"], cwd=str(tmp_path))
        content = log_file.read_text(encoding="utf-8")
        assert "true" in content
    finally:
        ui.reset()


# ---------------------------------------------------------------- MakeLauncher & NoneSync


def test_make_launcher_and_none_sync(tmp_path: Path) -> None:
    plan = _make_plan()
    launcher = MakeLauncher()
    assert launcher.make_vars(plan) == {}
    assert launcher.jobs(8) == 8
    launcher.stop(LocalTransport(), plan.run_id)

    before = set(tmp_path.iterdir())
    NoneSync().push(LocalTransport(), str(tmp_path), ["a.txt"])
    assert set(tmp_path.iterdir()) == before


# ---------------------------------------------------------------- LsfLauncher


def test_lsf_launcher_vars_jobs_and_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan = _make_plan("run_42")
    launcher = LsfLauncher(queue="normal", resources="rusage[mem=8000]", extra=["-P", "asic"])
    vars_dict = launcher.make_vars(plan)
    assert set(vars_dict) == {"SUBMIT"}
    submit = vars_dict["SUBMIT"]
    assert submit.startswith("bsub -K ")
    assert "-q normal" in submit
    assert "-R 'rusage[mem=8000]'" in submit
    assert "-P asic" in submit
    assert "-J 'solouvm_run_42_$(notdir $@)'" in submit
    assert launcher.jobs(16) == 16

    calls_file = tmp_path / "bkill.log"
    bin_dir = tmp_path / "bin"
    _install_stub(
        bin_dir,
        "bkill",
        f'printf "%s\\n" "$*" >> {shlex.quote(str(calls_file))}\n',
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    launcher.stop(LocalTransport(), "run_42")
    assert calls_file.read_text(encoding="utf-8").strip() == "-J solouvm_run_42_*"

    if shutil.which("make") is not None:
        mk = tmp_path / "run.mk"
        mk.write_text("SUBMIT ?=\nrun/smoke__1:\n\t@printf '%s\\n' $(SUBMIT)\n", encoding="utf-8")
        # Verify Make expands $(notdir $@) cleanly to smoke__1
        proc = subprocess.run(
            ["make", "-C", str(tmp_path), "-f", "run.mk", f"SUBMIT={submit}", "run/smoke__1"],
            capture_output=True,
            text=True,
            check=False,
        )
        # Note: bsub isn't executed here since recipe prints args or use echo wrapper:
        mk2 = tmp_path / "check.mk"
        mk2.write_text("SUBMIT ?=\nrun/smoke__1:\n\techo $(SUBMIT)\n", encoding="utf-8")
        sub_echo = submit.replace("bsub ", "", 1)
        proc2 = subprocess.run(
            ["make", "-C", str(tmp_path), "-f", "check.mk", f"SUBMIT={sub_echo}", "run/smoke__1"],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "solouvm_run_42_smoke__1" in proc2.stdout
        _ = proc


# ---------------------------------------------------------------- SshTransport


def _setup_ssh_stub(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Create an executable ``ssh`` stub that records calls and runs remote_cmd locally."""
    bin_dir = tmp_path / "bin"
    log_file = tmp_path / "ssh_calls.jsonl"
    remote_root = tmp_path / "remote_b"
    remote_root.mkdir(parents=True, exist_ok=True)
    helper = tmp_path / "ssh_stub.py"
    helper.write_text(
        textwrap.dedent(
            """
            import json
            import subprocess
            import sys

            log_path = sys.argv[1]
            args = sys.argv[2:]
            opts = []
            i = 0
            while i < len(args) and args[i].startswith("-"):
                opts.append(args[i])
                if args[i] in ("-p", "-o", "-i", "-l", "-F") and i + 1 < len(args):
                    opts.append(args[i + 1])
                    i += 2
                else:
                    i += 1
            host = args[i] if i < len(args) else ""
            cmd = args[i + 1] if i + 1 < len(args) else ""
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"opts": opts, "host": host, "cmd": cmd}) + "\\n")
            proc = subprocess.run(["sh", "-c", cmd])
            sys.exit(proc.returncode)
            """
        ),
        encoding="utf-8",
    )
    _install_stub(
        bin_dir,
        "ssh",
        f'exec python3 {shlex.quote(str(helper))} {shlex.quote(str(log_file))} "$@"\n',
    )
    return bin_dir, log_file, remote_root


def test_ssh_validation() -> None:
    with pytest.raises(ConfigError) as e1:
        SshTransport(host="", workdir="/w")
    assert e1.value.code == "CFG_INVALID"
    with pytest.raises(ConfigError) as e2:
        SshTransport(host="simbox", workdir="  ")
    assert e2.value.code == "CFG_INVALID"


def test_ssh_transport_operations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir, calls_log, remote_root = _setup_ssh_stub(tmp_path)
    local_root = tmp_path / "local_a"
    local_root.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    ssh = SshTransport(
        host="simbox",
        workdir=str(remote_root),
        ssh_opts=["-p", "2222"],
        root=local_root,
    )

    # put + exists
    assert ssh.exists("sub/hello.txt") is False
    ssh.put(str(local_root / "sub" / "hello.txt"), b"alpha\nbeta\nUVM_ERROR hit\ngamma")
    assert ssh.exists("sub/hello.txt") is True
    assert (remote_root / "sub" / "hello.txt").read_text(encoding="utf-8") == (
        "alpha\nbeta\nUVM_ERROR hit\ngamma"
    )

    # exec
    assert ssh.exec(["test", "-f", "hello.txt"], cwd="sub") == 0
    assert ssh.exec(["false"], cwd=".", check=False) == 1
    with pytest.raises(InfraError) as exc_exec:
        ssh.exec(["false"], cwd=".")
    assert exc_exec.value.code == "PROVIDER_FAILED"

    # spawn
    proc = ssh.spawn(["sleep", "5"], cwd=".")
    assert proc.poll() is None
    proc.terminate()
    assert proc.wait(timeout=5) != 0

    # read_lines: keywords (with grep -n -F and -C), bounded range, full stream
    assert inspect.isgenerator(ssh.read_lines("sub/hello.txt"))
    assert list(ssh.read_lines("sub/hello.txt", keywords=["UVM_"])) == [(3, "UVM_ERROR hit")]
    # grep rc == 1 (no matches) -> empty, no error!
    assert list(ssh.read_lines("sub/hello.txt", keywords=["NO_SUCH_TOKEN"])) == []
    # grep rc >= 2 (missing file) -> InfraError
    with pytest.raises(InfraError):
        list(ssh.read_lines("sub/missing.txt", keywords=["UVM_"]))

    assert list(ssh.read_lines("sub/hello.txt", lines=(2, 3))) == [
        (2, "beta"),
        (3, "UVM_ERROR hit"),
    ]
    assert list(ssh.read_lines("sub/hello.txt")) == [
        (1, "alpha"),
        (2, "beta"),
        (3, "UVM_ERROR hit"),
        (4, "gamma"),
    ]

    # line_count
    assert ssh.line_count("sub/hello.txt") == 3  # 3 newlines (wc -l semantics)
    assert ssh.line_count("sub/missing.txt") is None

    # read_many (multiple files, including no trailing newline, and zero matches)
    ssh.put("runs/r1/j1/try1/job.facts", b"state=done\nrc=0")
    ssh.put("runs/r1/j2/try1/job.facts", b"state=running\n")
    facts = ssh.read_many("runs/r1/*/try*/job.facts")
    assert facts == {
        str(remote_root / "runs/r1/j1/try1/job.facts"): "state=done\nrc=0",
        str(remote_root / "runs/r1/j2/try1/job.facts"): "state=running\n",
    }
    assert ssh.read_many("runs/r1/*/try*/nope.facts") == {}

    # Inspect recorded ssh calls to confirm compression -C and grep -n -F
    recorded = [json.loads(line) for line in calls_log.read_text(encoding="utf-8").splitlines()]
    grep_calls = [c for c in recorded if "grep -n -F" in c["cmd"]]
    assert grep_calls
    assert "-C" in grep_calls[0]["opts"]
    assert "-p" in grep_calls[0]["opts"]
    assert "2222" in grep_calls[0]["opts"]

    # kill_pgid / stop_pgid
    ssh.stop_pgid(999999)
    recorded_after = [
        json.loads(line) for line in calls_log.read_text(encoding="utf-8").splitlines()
    ]
    assert "kill -TERM -999999" in recorded_after[-1]["cmd"]

    # rm_tree
    ssh.rm_tree("sub")
    assert ssh.exists("sub") is False


def test_ssh_exists_255_raises_infra_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    _install_stub(bin_dir, "ssh", "exit 255\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    ssh = SshTransport(host="simbox", workdir="/work")
    with pytest.raises(InfraError) as exc_info:
        ssh.exists("foo")
    assert exc_info.value.code == "PROVIDER_FAILED"


def test_ssh_default_opts_order_nul_read_many_and_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir, calls_log, remote_root = _setup_ssh_stub(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    ssh = SshTransport(
        host="simbox",
        workdir=str(remote_root),
        ssh_opts=["-p", "2222", "-o", "ConnectTimeout=3"],
    )
    assert ssh.exists("missing") is False
    recorded = [json.loads(line) for line in calls_log.read_text(encoding="utf-8").splitlines()]
    opts = recorded[-1]["opts"]
    # User ssh_opts appear before defaults so OpenSSH first-match-wins honors overrides.
    assert opts.index("ConnectTimeout=3") < opts.index("ConnectTimeout=10")
    assert "BatchMode=yes" in opts
    assert "ServerAliveInterval=15" in opts
    assert "ServerAliveCountMax=3" in opts

    # NUL-delimited read_many handles filenames with newlines and 0-byte files cleanly.
    nl_dir = remote_root / "nl"
    nl_dir.mkdir(parents=True, exist_ok=True)
    (nl_dir / "line1\nline2.facts").write_bytes(b"state=done\nrc=0\n")
    (nl_dir / "empty.facts").write_bytes(b"")
    many = ssh.read_many("nl/*.facts")
    assert many == {
        str(remote_root / "nl/empty.facts"): "",
        str(remote_root / "nl/line1\nline2.facts"): "state=done\nrc=0\n",
    }

    # Operation timeout raises InfraError(PROVIDER_FAILED).
    ssh_fast = SshTransport(host="simbox", workdir=str(remote_root), op_timeout=0.05)
    with pytest.raises(InfraError) as exc_to:
        ssh_fast.exec(["sleep", "2"], cwd=".", timeout=0.05)
    assert exc_to.value.code == "PROVIDER_FAILED"
    assert "timed out" in exc_to.value.message

    slow_bin = tmp_path / "slow_bin"
    _install_stub(slow_bin, "ssh", "sleep 2\n")
    monkeypatch.setenv("PATH", f"{slow_bin}:{os.environ['PATH']}")
    with pytest.raises(InfraError) as exc_op:
        ssh_fast.exists("anything")
    assert exc_op.value.code == "PROVIDER_FAILED"
    assert "timed out" in exc_op.value.message


# ---------------------------------------------------------------- RsyncSync


def test_rsync_sync_two_step_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    record_file = tmp_path / "rsync_calls.jsonl"
    helper = tmp_path / "rsync_stub.py"
    helper.write_text(
        textwrap.dedent(
            """
            import json
            import sys

            out_path = sys.argv[1]
            argv = sys.argv[2:]
            stdin_data = sys.stdin.read() if "--files-from=-" in argv else ""
            with open(out_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"argv": argv, "stdin": stdin_data}) + "\\n")
            """
        ),
        encoding="utf-8",
    )
    _install_stub(
        bin_dir,
        "rsync",
        f'exec python3 {shlex.quote(str(helper))} {shlex.quote(str(record_file))} "$@"\n',
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    tree = tmp_path / "proj"
    tree.mkdir()
    ssh = SshTransport(host="simbox", workdir="/work/proj", ssh_opts=["-p", "2200"])
    sync = RsyncSync(exclude=("waves/",), delete=True)
    sync.push(
        ssh,
        str(tree),
        [
            ".solouvm/gen/bin/run_job.sh",
            str(tree / ".solouvm/runs/R1/run.mk"),
        ],
    )

    calls = [json.loads(line) for line in record_file.read_text(encoding="utf-8").splitlines()]
    assert len(calls) == 2

    step1, step2 = calls
    assert "--exclude=.git/" in step1["argv"]
    assert "--exclude=.solouvm/" in step1["argv"]
    assert "--exclude=waves/" in step1["argv"]
    assert "--delete" in step1["argv"]
    assert "-e" in step1["argv"]
    assert step1["argv"][-2:] == [f"{tree}/", "simbox:/work/proj/"]

    assert "--files-from=-" in step2["argv"]
    assert step2["argv"][-2:] == [f"{tree}/", "simbox:/work/proj/"]
    assert step2["stdin"].splitlines() == [
        ".solouvm/gen/bin/run_job.sh",
        ".solouvm/runs/R1/run.mk",
    ]


def test_rsync_sync_failure_and_empty_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    _install_stub(bin_dir, "rsync", "echo 'rsync error' >&2\nexit 12\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    sync = RsyncSync(host="box", remote_workdir="/w")
    with pytest.raises(InfraError) as exc_info:
        sync.push(LocalTransport(), str(tmp_path), [])
    assert exc_info.value.code == "PROVIDER_FAILED"
    assert "12" in exc_info.value.message

    sync_missing = RsyncSync()
    with pytest.raises(InfraError):
        sync_missing.push(LocalTransport(), str(tmp_path), [])


# ---------------------------------------------------------------- GitSync


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def test_git_sync_dirty_and_clean_push(tmp_path: Path) -> None:
    remote_bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_bare)], check=True, capture_output=True)

    repo_a = tmp_path / "repo_a"
    subprocess.run(["git", "init", str(repo_a)], check=True, capture_output=True)
    _git(repo_a, "config", "user.email", "ci@example.com")
    _git(repo_a, "config", "user.name", "CI")
    _git(repo_a, "remote", "add", "origin", str(remote_bare))

    _write(repo_a / "rtl" / "dut.sv", "module dut; endmodule\n")
    _git(repo_a, "add", "rtl/dut.sv")
    _git(repo_a, "commit", "-m", "initial")
    branch = _git(repo_a, "symbolic-ref", "--short", "HEAD").strip()
    _git(repo_a, "push", "-u", "origin", branch)

    # Clone B from bare remote
    repo_b = tmp_path / "repo_b"
    subprocess.run(["git", "clone", str(remote_bare), str(repo_b)], check=True, capture_output=True)

    # Make a new commit on A + create generated file under .solouvm/ (gitignored)
    _write(repo_a / ".gitignore", ".solouvm/\n")
    _write(repo_a / "rtl" / "dut.sv", "module dut; logic a; endmodule\n")
    _git(repo_a, "add", ".gitignore", "rtl/dut.sv")
    _git(repo_a, "commit", "-m", "add signal")

    # Dirty tree must raise GIT_DIRTY with rsync hint
    _write(repo_a / "rtl" / "dirty.sv", "// uncommitted\n")
    with pytest.raises(InfraError) as dirty_exc:
        GitSync(remote="origin", branch=branch).push(LocalTransport(root=repo_b), str(repo_a), [])
    assert dirty_exc.value.code == "GIT_DIRTY"
    assert "rsync" in (dirty_exc.value.hint or "")

    # Remove dirty file, generate artifact under .solouvm/ (gitignored)
    (repo_a / "rtl" / "dirty.sv").unlink()
    gen_mk = repo_a / ".solouvm" / "runs" / "R1" / "run.mk"
    _write(gen_mk, "# generated run.mk\n")

    GitSync(remote="origin", branch=branch).push(
        LocalTransport(root=repo_b),
        str(repo_a),
        [".solouvm/runs/R1/run.mk"],
    )
    assert "logic a;" in (repo_b / "rtl" / "dut.sv").read_text(encoding="utf-8")
    assert (repo_b / ".solouvm" / "runs" / "R1" / "run.mk").read_text(encoding="utf-8") == (
        "# generated run.mk\n"
    )

    # Also test branch=None pushing current branch + commit checkout
    _write(repo_a / "rtl" / "dut.sv", "module dut; logic b; endmodule\n")
    _git(repo_a, "add", "rtl/dut.sv")
    _git(repo_a, "commit", "-m", "second signal")
    GitSync(remote="origin", branch=None).push(
        LocalTransport(root=repo_b),
        str(repo_a),
        [".solouvm/runs/R1/run.mk"],
    )
    assert "logic b;" in (repo_b / "rtl" / "dut.sv").read_text(encoding="utf-8")


def test_git_sync_rejects_branch_head_mismatch(tmp_path: Path) -> None:
    remote_bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_bare)], check=True, capture_output=True)

    repo_a = tmp_path / "repo_a"
    subprocess.run(["git", "init", str(repo_a)], check=True, capture_output=True)
    _git(repo_a, "config", "user.email", "ci@example.com")
    _git(repo_a, "config", "user.name", "CI")
    _git(repo_a, "remote", "add", "origin", str(remote_bare))

    _write(repo_a / "rtl" / "dut.sv", "module dut; endmodule\n")
    _git(repo_a, "add", "rtl/dut.sv")
    _git(repo_a, "commit", "-m", "base")
    base_branch = _git(repo_a, "symbolic-ref", "--short", "HEAD").strip()
    _git(repo_a, "push", "-u", "origin", base_branch)

    repo_b = tmp_path / "repo_b"
    subprocess.run(["git", "clone", str(remote_bare), str(repo_b)], check=True, capture_output=True)

    _git(repo_a, "checkout", "-b", "feature")
    _write(repo_a / "rtl" / "dut.sv", "module dut; logic feat; endmodule\n")
    _git(repo_a, "add", "rtl/dut.sv")
    _git(repo_a, "commit", "-m", "feature commit")

    with pytest.raises(InfraError) as exc:
        GitSync(remote="origin", branch=base_branch).push(
            LocalTransport(root=repo_b),
            str(repo_a),
            [],
        )
    assert exc.value.code == "PROVIDER_FAILED"
    assert "does not match" in str(exc.value)
    assert "feat" not in (repo_b / "rtl" / "dut.sv").read_text(encoding="utf-8")


# ---------------------------------------------------------------- Plugins (H-15)


def test_plugin_registration_via_cfg_and_isolation(tmp_path: Path) -> None:
    proj = tmp_path / "proj_plug"
    _write(
        proj / "myflow.py",
        """
        class SlurmLauncher:
            kind = "slurm"

            def __init__(self, cfg):
                self.cfg = cfg

            def make_vars(self, plan):
                return {"SUBMIT": f"srun -p {self.cfg.get('partition', 'default')}"}

            def jobs(self, requested):
                return requested

            def stop(self, transport, run_id):
                pass


        class ZeroArgSync:
            kind = "zero_sync"

            def __init__(self):
                self.pushed = []

            def push(self, transport, tree, files):
                self.pushed.append(list(files))
        """,
    )
    _write(
        proj / "solouvm.conf.py",
        """
        CFG = {
            "simulator": "fake",
            "top": "tb_top",
            "plugins": ["myflow.SlurmLauncher", "myflow.ZeroArgSync"],
            "provider": "hpc",
            "providers": {
                "hpc": {
                    "launcher": {"kind": "slurm", "partition": "gpu", "nodes": 2},
                    "sync": {"kind": "zero_sync", "custom_flag": True},
                }
            },
        }
        """,
    )
    cfg = load_config(proj)
    assert cfg.providers["hpc"].launcher.kind == "slurm"
    assert cfg.providers["hpc"].launcher.options == {"partition": "gpu", "nodes": 2}
    assert cfg.providers["hpc"].sync.kind == "zero_sync"
    assert cfg.providers["hpc"].sync.options == {"custom_flag": True}

    prov = get_provider(cfg)
    assert prov.launcher.make_vars(_make_plan()) == {"SUBMIT": "srun -p gpu"}
    prov.sync.push(prov.transport, str(proj), ["a.txt"])
    assert prov.sync.pushed == [["a.txt"]]  # type: ignore[attr-defined]

    # Built-in kinds still reject unknown keys strictly even when plugins are loaded
    _write(
        proj / "solouvm.local.py",
        'LOCAL = {"providers": {"bad_builtin": {"launcher": {"kind": "lsf", "partition": "x"}}}}\n',
    )
    with pytest.raises(ConfigError) as err_builtin:
        load_config(proj)
    assert err_builtin.value.code == "CFG_UNKNOWN_KEY"

    # Other projects without CFG["plugins"] do NOT inherit "slurm"
    other = tmp_path / "proj_other"
    _write(
        other / "solouvm.conf.py",
        """
        CFG = {
            "simulator": "fake",
            "top": "t",
            "providers": {"x": {"launcher": {"kind": "slurm"}}},
        }
        """,
    )
    with pytest.raises(ConfigError) as err_other:
        load_config(other)
    assert err_other.value.code == "CFG_INVALID"


def test_plugin_direct_registry_and_rejection(tmp_path: Path) -> None:
    reg = ProviderRegistry()
    assert "local" in known_kinds()["transport"]

    class DummySync:
        kind = "dummy_s"

        def push(self, transport, tree, files):
            pass

    reg.register_sync("dummy_s", DummySync)
    assert "dummy_s" in reg.known_kinds()["sync"]

    # Cannot overwrite built-in kind
    with pytest.raises(ConfigError) as err_over:
        reg.register_transport("ssh", DummySync)  # type: ignore[arg-type]
    assert err_over.value.code == "CFG_INVALID"

    # Invalid plugin specs / incomplete classes
    _write(
        tmp_path / "badplug.py",
        """
        NOT_A_CLASS = 123

        class IncompleteLauncher:
            kind = "inc"
            def make_vars(self, plan):
                return {}
        """,
    )
    for bad_spec in (
        "bad",
        "no_such_module.Cls",
        "badplug.MissingAttr",
        "badplug.NOT_A_CLASS",
        "badplug.IncompleteLauncher",
    ):
        with pytest.raises(ConfigError) as exc_bad:
            reg.load_plugins([bad_spec], root=tmp_path)
        assert exc_bad.value.code == "CFG_INVALID"

    # Verify top-level helpers exist & work on isolated registry behavior
    assert callable(register_transport)
    assert callable(register_launcher)
    assert callable(register_sync)
    assert callable(load_plugins)


# ---------------------------------------------------------------- Prefilter (H-16)


def test_extract_literal_cases() -> None:
    assert extract_literal(r"^Error-\[") == "Error-["
    assert extract_literal(r"^Fatal:") == "Fatal:"
    assert extract_literal(r"Offending '") == "Offending '"
    assert extract_literal(r"^xmsim: \*E,") == "xmsim: *E,"
    assert extract_literal(r"\$finish.* at time (\d+)") == " at time "
    assert extract_literal(r"sim time: (\d+)ns") == "sim time: "
    assert extract_literal(r"foobar?") == "fooba"
    assert extract_literal(r"fo+bar") == "bar"
    assert extract_literal(r"(foo)+") == "foo"
    assert extract_literal(r"a{3}") == "aaa"
    assert extract_literal(r"[a|b]ERROR") == "ERROR"
    assert extract_literal(r"UVM_ERROR|UVM_FATAL") == "UVM_"
    assert extract_literal(r"(?:ERROR|FAIL): bad") == ": bad"
    assert extract_literal(r"(foo|bar)_TAIL") == "_TAIL"

    # Cases returning None (must fall back to full log transfer)
    assert extract_literal("") is None
    assert extract_literal("ab") is None
    assert extract_literal("foo|bar") is None
    assert extract_literal("(foo)*") is None
    assert extract_literal("a{2,5}") is None
    assert extract_literal("(?i)error") is None
    assert extract_literal(r"ab\s+cd") is None


def test_extract_keywords_collection_and_fallback() -> None:
    vcs = get_backend("vcs")
    checks = Checks(
        fail_regex=("TEST FAILED",),
        pass_regex=("PASS_OK",),
        env_fail_regex=("LICENSE_DENIED",),
        waivers=(
            Waiver(id="OLD", reason="expired", regex=r"foo|bar", until="2020-01-01"),
            Waiver(regex=r"known warning msg", reason="active", until="2099-12-31"),
        ),
    )
    expects = [
        Expect(scoreboard="AXI_SCB", min=10),
        Expect(regex=r"\[VSEQ\] done", count_min=1),
    ]
    kws = extract_keywords(
        backend=vcs,
        checks=checks,
        expects=expects,
        today=date(2026, 11, 18),
    )
    assert kws is not None
    assert keywords_for is extract_keywords
    assert {"UVM_", "SOLOUVM_DONE", "TEST_DONE"}.issubset(kws)
    assert "TEST FAILED" in kws
    assert "PASS_OK" in kws
    assert "LICENSE_DENIED" in kws
    assert "Failed to obtain license" not in kws  # overridden by checks.env_fail_regex
    assert "known warning msg" in kws
    assert "compared=" in kws
    assert "[VSEQ] done" in kws
    assert "Error-[" in kws

    # Active alternation without common literal forces fallback -> None
    bad_checks = Checks(fail_regex=(r"foo|bar",))
    assert extract_keywords(backend=vcs, checks=bad_checks) is None


# ---------------------------------------------------------------- Filtered vs Unfiltered Parity


_SUMMARY_PASS = (
    "--- UVM Report Summary ---\n"
    "** Report counts by severity\n"
    "UVM_INFO :    3\nUVM_WARNING :    0\nUVM_ERROR :    0\nUVM_FATAL :    0\n"
)
_SUMMARY_ERR1 = (
    "--- UVM Report Summary ---\n"
    "** Report counts by severity\n"
    "UVM_INFO :    3\nUVM_WARNING :    0\nUVM_ERROR :    1\nUVM_FATAL :    0\n"
)
_SUMMARY_FATAL1 = (
    "--- UVM Report Summary ---\n"
    "** Report counts by severity\n"
    "UVM_INFO :    3\nUVM_WARNING :    0\nUVM_ERROR :    0\nUVM_FATAL :    1\n"
)


def _sample_facts() -> Facts:
    t0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    return Facts(
        schema=1,
        generator="solouvm 0.1.0",
        kind="job",
        run_id="R1",
        job_id="case__1",
        try_=1,
        host="simbox",
        pid=100,
        timeout_impl="timeout",
        started_at=t0,
        state="done",
        rc=0,
        finished_at=t0,
        duration_s=1.5,
    )


def _sparse_lines(filtered: list[tuple[int, str]], total_lines: int) -> list[str]:
    buf = [""] * total_lines
    for lineno, text in filtered:
        buf[lineno - 1] = text
    return buf


@pytest.mark.parametrize(
    ("name", "log_text", "checks", "case"),
    [
        (
            "uvm_pass",
            "noise 1\nnoise 2\n"
            "UVM_INFO a.sv(1) @ 0ns: uvm_test_top [RNTST] start\n"
            "noise 3\n"
            "[AXI_SCB] compared=128 passed=128\n"
            "UVM_INFO a.sv(9) @ 60000ns: reporter [TEST_DONE] done\n" + _SUMMARY_PASS,
            Checks(),
            Case(
                name="uvm_pass",
                expect=[
                    Expect(scoreboard="AXI_SCB", min=100),
                    Expect(sim_time_min="50us"),
                    Expect(uvm_info_min=1, id="RNTST"),
                ],
            ),
        ),
        (
            "uvm_error_fail",
            "boot\nUVM_ERROR scb.sv(12) @ 100ns: uvm_test_top.scb [AXI_SCB] mismatch 0x10\n"
            + _SUMMARY_ERR1,
            Checks(),
            Case(name="uvm_error_fail"),
        ),
        (
            "uvm_fatal_fail",
            "boot\nUVM_FATAL env.sv(3) @ 10ns: uvm_test_top [FATAL_ID] dead\n" + _SUMMARY_FATAL1,
            Checks(),
            Case(name="uvm_fatal_fail"),
        ),
        (
            "waived_uvm_error",
            "noise\nUVM_ERROR scb.sv(5) @ 50ns: uvm_test_top [KNOWN_ERR] harmless\n"
            + _SUMMARY_ERR1,
            Checks(waivers=(Waiver(id="KNOWN_ERR", reason="known"),)),
            Case(name="waived_uvm_error"),
        ),
        (
            "sim_error",
            "running\nError-[NOA] Null object access\ndone\n",
            Checks(),
            Case(name="sim_error"),
        ),
        (
            "fail_regex_hit",
            "running\nnoise\nCUSTOM_BAD_TAG happened\n" + _SUMMARY_PASS,
            Checks(fail_regex=("CUSTOM_BAD_TAG",)),
            Case(name="fail_regex_hit"),
        ),
        (
            "pass_regex_rescue_no_summary",
            "UVM_INFO a.sv(1) @ 0ns: uvm_test_top [RNTST] running\n*** PASS OVERRIDE ***\n",
            Checks(pass_regex=("PASS OVERRIDE",)),
            Case(name="pass_regex_rescue_no_summary"),
        ),
        (
            "env_license_fail",
            "starting\nFailed to obtain license for VCS\n",
            Checks(),
            Case(name="env_license_fail"),
        ),
        (
            "non_uvm_pass",
            "plain sim\n$finish at time 50000\nSOLOUVM_DONE status=pass\n",
            Checks(),
            Case(name="non_uvm_pass", expect=[Expect(regex=r"plain sim", count_min=1)]),
        ),
    ],
)
def test_filtered_scan_parity_with_ssh_and_local(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    log_text: str,
    checks: Checks,
    case: Case,
) -> None:
    _ = name
    bin_dir, _, remote_root = _setup_ssh_stub(tmp_path)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    backend = get_backend("vcs")
    kws = extract_keywords(backend=backend, checks=checks, expects=case.expect)
    assert kws is not None

    log_path = remote_root / "sim.log"
    log_path.write_text(log_text, encoding="utf-8")

    local_t = LocalTransport(root=remote_root)
    ssh_t = SshTransport(host="simbox", workdir=str(remote_root))

    facts = _sample_facts()
    today = date(2026, 11, 18)

    raw_lines = log_text.splitlines()
    unfiltered_scan = scan(raw_lines, checks=checks, backend=backend, expects=case.expect)
    v_unfiltered: Verdict = decide(
        facts=facts,
        scan=unfiltered_scan,
        build_ok=True,
        build_facts=None,
        build_key="default/k",
        checks=checks,
        case=case,
        allow_missing=False,
        today=today,
    )

    for transport in (local_t, ssh_t):
        filtered = list(transport.read_lines("sim.log", keywords=kws))
        compact_scan = scan(
            (text for _, text in filtered),
            checks=checks,
            backend=backend,
            expects=case.expect,
        )
        v_compact = decide(
            facts=facts,
            scan=compact_scan,
            build_ok=True,
            build_facts=None,
            build_key="default/k",
            checks=checks,
            case=case,
            allow_missing=False,
            today=today,
        )
        assert v_compact.status == v_unfiltered.status
        assert v_compact.category == v_unfiltered.category
        assert v_compact.signature == v_unfiltered.signature
        if v_unfiltered.first_error is None:
            assert v_compact.first_error is None
        else:
            assert v_compact.first_error is not None
            assert v_compact.first_error[1] == v_unfiltered.first_error[1]

        sparse_scan = scan(
            _sparse_lines(filtered, len(raw_lines)),
            checks=checks,
            backend=backend,
            expects=case.expect,
        )
        v_sparse = decide(
            facts=facts,
            scan=sparse_scan,
            build_ok=True,
            build_facts=None,
            build_key="default/k",
            checks=checks,
            case=case,
            allow_missing=False,
            today=today,
        )
        assert v_sparse == v_unfiltered
