"""Tests for run_job.sh, launch.sh and fake_sim.sh +SOLOUVM_FAKE_SCB (task 14)."""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from solouvm.backends import BACKENDS, fake_sim_source
from solouvm.gen.scripts import render_launch, render_run_job
from solouvm.judge.facts import read_facts
from solouvm.judge.scan import scan
from solouvm.model import Checks, Expect

FAKE_SIM = Path(__file__).resolve().parent.parent / "solouvm" / "backends" / "fake_sim.sh"

_MOCK_TIMEOUT_PY = f"""#!{sys.executable}
import os, signal, subprocess, sys, time

args = sys.argv[1:]
kill_after = 10.0
while args and args[0].startswith("--"):
    opt = args.pop(0)
    if opt.startswith("--kill-after="):
        val = opt.split("=", 1)[1].rstrip("s")
        kill_after = float(val)
secs = float(args.pop(0))
proc = subprocess.Popen(args, start_new_session=True)

def forward(signum, _frame):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=1.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    sys.exit(128 + signum)

signal.signal(signal.SIGTERM, forward)
signal.signal(signal.SIGINT, forward)

try:
    rc = proc.wait(timeout=secs)
except subprocess.TimeoutExpired:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=kill_after)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        sys.exit(137)
    sys.exit(124)
sys.exit(128 - rc if rc < 0 else rc)
"""


def _install_scripts(root: Path, *, version: str = "0.1.0") -> tuple[Path, Path]:
    bin_dir = root / ".solouvm" / "gen" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    run_job = bin_dir / "run_job.sh"
    launch = bin_dir / "launch.sh"
    fake = bin_dir / "fake_sim.sh"
    run_job.write_text(render_run_job(version), encoding="utf-8")
    launch.write_text(render_launch(version), encoding="utf-8")
    fake.write_text(fake_sim_source(), encoding="utf-8")
    run_job.chmod(0o755)
    launch.chmod(0o755)
    fake.chmod(0o755)
    return run_job, launch


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for(predicate, *, timeout: float = 5.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_scripts_bash_syntax_valid(tmp_path: Path) -> None:
    run_job, launch = _install_scripts(tmp_path, version="9.9.9")
    for script in (run_job, launch, FAKE_SIM):
        res = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert res.returncode == 0, res.stderr


def test_run_job_pass_and_generator_version(tmp_path: Path) -> None:
    run_job, _ = _install_scripts(tmp_path, version="1.2.3")
    job_dir = tmp_path / ".solouvm" / "runs" / "r1" / "smoke__1" / "try1"
    res = subprocess.run(
        [
            "bash",
            str(run_job),
            "--kind",
            "job",
            "--dir",
            str(job_dir),
            "--run-id",
            "r1",
            "--job-id",
            "smoke__1",
            "--try",
            "1",
            "--",
            "true",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res.returncode == 0
    assert (job_dir / "sim.log").is_file()
    facts = read_facts(job_dir / "job.facts")
    assert facts is not None
    assert facts.schema == 1
    assert facts.generator == "solouvm 1.2.3"
    assert facts.kind == "job"
    assert facts.run_id == "r1"
    assert facts.job_id == "smoke__1"
    assert facts.try_ == 1
    assert facts.state == "done"
    assert facts.rc == 0
    assert facts.signal is None
    assert facts.timed_out is False
    assert facts.hung is False
    assert facts.duration_s is not None and facts.duration_s >= 0.0


def test_run_job_nonzero_still_exits_zero_for_job_and_nonzero_for_build(tmp_path: Path) -> None:
    run_job, _ = _install_scripts(tmp_path)

    job_dir = tmp_path / "job_fail"
    res_job = subprocess.run(
        [
            "bash",
            str(run_job),
            "--kind",
            "job",
            "--dir",
            str(job_dir),
            "--",
            "bash",
            "-c",
            "exit 3",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_job.returncode == 0
    facts_job = read_facts(job_dir / "job.facts")
    assert facts_job is not None and facts_job.rc == 3

    build_dir = tmp_path / "build_fail"
    res_build = subprocess.run(
        [
            "bash",
            str(run_job),
            "--kind",
            "build",
            "--dir",
            str(build_dir),
            "--",
            "bash",
            "-c",
            "exit 3",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res_build.returncode == 3
    assert not (build_dir / ".ok").exists()
    assert (build_dir / "build.log").is_file()
    facts_build = read_facts(build_dir / "job.facts")
    assert facts_build is not None and facts_build.rc == 3


def test_run_job_build_ok_and_lock_reuse(tmp_path: Path) -> None:
    run_job, _ = _install_scripts(tmp_path)
    build_dir = tmp_path / ".solouvm" / "build" / "default" / "abc123"
    marker = tmp_path / "compile_count.txt"
    cmd = [
        "bash",
        str(run_job),
        "--kind",
        "build",
        "--dir",
        str(build_dir),
        "--lock",
        "--",
        "bash",
        "-c",
        f'echo ran >> "{marker}"',
    ]

    r1 = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, timeout=15, check=False)
    assert r1.returncode == 0
    assert (build_dir / ".ok").is_file()
    assert len(marker.read_text(encoding="utf-8").splitlines()) == 1

    # Second run with --lock sees .ok and skips command
    r2 = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, timeout=15, check=False)
    assert r2.returncode == 0
    assert len(marker.read_text(encoding="utf-8").splitlines()) == 1


def test_run_job_plusargs_and_log_names(tmp_path: Path) -> None:
    run_job, _ = _install_scripts(tmp_path)

    job_dir = tmp_path / "out_job"
    args_file = tmp_path / "job_args.txt"
    r_job = subprocess.run(
        [
            "bash",
            str(run_job),
            "--kind",
            "job",
            "--dir",
            str(job_dir),
            "--",
            "bash",
            "-c",
            f'printf "%s\\n" "$@" > "{args_file}"',
            "_",
            "+USER_ARG=1",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_job.returncode == 0
    args = args_file.read_text(encoding="utf-8").splitlines()
    assert f"+SOLOUVM_ROOT={tmp_path.resolve()}" in args
    assert f"+SOLOUVM_OUTDIR={job_dir.resolve()}" in args
    assert (job_dir / "sim.log").is_file()

    # kind=build and custom --log must NOT append +SOLOUVM_ROOT / +SOLOUVM_OUTDIR
    build_dir = tmp_path / "out_build"
    b_args_file = tmp_path / "build_args.txt"
    r_build = subprocess.run(
        [
            "bash",
            str(run_job),
            "--kind",
            "build",
            "--dir",
            str(build_dir),
            "--log",
            "custom.log",
            "--",
            "bash",
            "-c",
            f'printf "%s\\n" "$@" > "{b_args_file}"',
            "_",
            "-top",
            "tb_top",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert r_build.returncode == 0
    b_args = b_args_file.read_text(encoding="utf-8").splitlines()
    assert not any(a.startswith("+SOLOUVM_ROOT=") for a in b_args)
    assert not any(a.startswith("+SOLOUVM_OUTDIR=") for a in b_args)
    assert (build_dir / "custom.log").is_file()


def test_run_job_sigsegv(tmp_path: Path) -> None:
    run_job, _ = _install_scripts(tmp_path)
    job_dir = tmp_path / "segv_job"
    res = subprocess.run(
        [
            "bash",
            str(run_job),
            "--kind",
            "job",
            "--dir",
            str(job_dir),
            "--",
            "bash",
            "-c",
            "kill -SEGV $$",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert res.returncode == 0
    facts = read_facts(job_dir / "job.facts")
    assert facts is not None
    assert facts.rc == 139
    assert facts.signal == "SIGSEGV"
    assert facts.timed_out is False
    assert facts.hung is False


@pytest.mark.parametrize("impl", ["builtin", "timeout", "gtimeout"])
def test_run_job_timeout_all_three_implementations(tmp_path: Path, impl: str) -> None:
    run_job, _ = _install_scripts(tmp_path)
    mock_bin = tmp_path / f"bin_{impl}"
    mock_bin.mkdir()
    if impl in ("timeout", "gtimeout"):
        shim = mock_bin / impl
        shim.write_text(_MOCK_TIMEOUT_PY, encoding="utf-8")
        shim.chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{mock_bin}:/usr/bin:/bin"
    env["SOLOUVM_TIMEOUT_IMPL"] = impl

    job_dir = tmp_path / f"to_{impl}"
    pid_marker = tmp_path / f"child_{impl}.pid"
    started = time.monotonic()
    res = subprocess.run(
        [
            "bash",
            str(run_job),
            "--kind",
            "job",
            "--dir",
            str(job_dir),
            "--timeout",
            "1",
            "--",
            "bash",
            "-c",
            f'echo $$ > "{pid_marker}"; sleep 10',
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    elapsed = time.monotonic() - started
    assert res.returncode == 0
    assert elapsed < 5.0

    facts = read_facts(job_dir / "job.facts")
    assert facts is not None
    assert facts.timeout_impl == impl
    assert facts.timed_out is True
    assert facts.hung is False
    assert facts.signal is None
    assert facts.rc in (124, 143)

    child_pid = int(pid_marker.read_text(encoding="utf-8").strip())
    assert not _pid_alive(child_pid)


def test_run_job_hang_detection(tmp_path: Path) -> None:
    run_job, _ = _install_scripts(tmp_path)

    # Silent command triggers --hang
    hang_dir = tmp_path / "hang_job"
    hang_marker = tmp_path / "hang_child.pid"
    started = time.monotonic()
    res = subprocess.run(
        [
            "bash",
            str(run_job),
            "--kind",
            "job",
            "--dir",
            str(hang_dir),
            "--hang",
            "1",
            "--",
            "bash",
            "-c",
            f'echo $$ > "{hang_marker}"; sleep 10',
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    elapsed = time.monotonic() - started
    assert res.returncode == 0
    assert 0.8 <= elapsed < 5.0
    facts = read_facts(hang_dir / "job.facts")
    assert facts is not None
    assert facts.hung is True
    assert facts.timed_out is False
    assert facts.signal is None
    hang_pid = int(hang_marker.read_text(encoding="utf-8").strip())
    assert not _pid_alive(hang_pid)

    # Active output resets hang timer and finishes normally
    active_dir = tmp_path / "active_job"
    res2 = subprocess.run(
        [
            "bash",
            str(run_job),
            "--kind",
            "job",
            "--dir",
            str(active_dir),
            "--hang",
            "1",
            "--",
            "bash",
            "-c",
            "for i in 1 2 3 4; do echo tick $i; sleep 0.3; done",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res2.returncode == 0
    facts2 = read_facts(active_dir / "job.facts")
    assert facts2 is not None
    assert facts2.hung is False
    assert facts2.rc == 0


def test_run_job_sigterm_writes_interrupted_and_kills_child(tmp_path: Path) -> None:
    run_job, _ = _install_scripts(tmp_path)
    job_dir = tmp_path / "term_job"
    pid_marker = tmp_path / "sleep_child.pid"

    proc = subprocess.Popen(
        [
            "bash",
            str(run_job),
            "--kind",
            "job",
            "--dir",
            str(job_dir),
            "--",
            "bash",
            "-c",
            f'echo $$ > "{pid_marker}"; sleep 20',
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _wait_for(pid_marker.is_file, timeout=5.0)
        child_pid = int(pid_marker.read_text(encoding="utf-8").strip())
        assert _pid_alive(child_pid)

        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=10)
        assert rc == 130
        assert _wait_for(lambda: not _pid_alive(child_pid), timeout=3.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    facts = read_facts(job_dir / "job.facts")
    assert facts is not None
    assert facts.state == "interrupted"
    assert facts.rc == 130


def test_run_job_lock_concurrency_and_stale_cleanup(tmp_path: Path) -> None:
    run_job, _ = _install_scripts(tmp_path)
    build_dir = tmp_path / "shared_build"
    counter = tmp_path / "runs.txt"

    cmd = [
        "bash",
        str(run_job),
        "--kind",
        "build",
        "--dir",
        str(build_dir),
        "--lock",
        "--",
        "bash",
        "-c",
        f'sleep 0.5; echo build >> "{counter}"',
    ]
    p1 = subprocess.Popen(cmd, cwd=tmp_path)
    try:
        assert _wait_for((build_dir.parent / f"{build_dir.name}.lock").exists, timeout=3.0)
        p2 = subprocess.Popen(cmd, cwd=tmp_path)
        try:
            assert p1.wait(timeout=10) == 0
            assert p2.wait(timeout=10) == 0
        finally:
            if p2.poll() is None:
                p2.kill()
                p2.wait(timeout=5)
    finally:
        if p1.poll() is None:
            p1.kill()
            p1.wait(timeout=5)

    assert len(counter.read_text(encoding="utf-8").splitlines()) == 1

    # Stale lock left by dead PID on same host is automatically cleaned (D23)
    stale_dir = tmp_path / "stale_build"
    lock_dir = tmp_path / "stale_build.lock"
    lock_dir.mkdir(parents=True)
    host = socket.gethostname().split(".")[0]
    (lock_dir / "owner").write_text(f"{host} 999999 old_run\n", encoding="utf-8")

    res = subprocess.run(
        ["bash", str(run_job), "--kind", "build", "--dir", str(stale_dir), "--lock", "--", "true"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res.returncode == 0
    assert (stale_dir / ".ok").is_file()
    assert not lock_dir.exists()


def test_run_job_hooks_and_env_expansion(tmp_path: Path) -> None:
    run_job, _ = _install_scripts(tmp_path)
    job_dir = tmp_path / "hook_job"
    res = subprocess.run(
        [
            "bash",
            str(run_job),
            "--kind",
            "job",
            "--dir",
            str(job_dir),
            "--setup",
            "export BASE_DIR=/opt/vip",
            "--pre",
            "echo pre_ran",
            "--post",
            "echo post_ran",
            "--env",
            "DATA=${BASE_DIR}/axi",
            "--env",
            "SAFE=$(echo SHOULD_NOT_RUN)",
            "--",
            "bash",
            "-c",
            'echo "DATA=$DATA"; echo "SAFE=$SAFE"',
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res.returncode == 0
    log = (job_dir / "sim.log").read_text(encoding="utf-8")
    assert "pre_ran" in log
    assert "DATA=/opt/vip/axi" in log
    assert "SAFE=$(echo SHOULD_NOT_RUN)" in log
    assert "post_ran" in log


def test_run_job_argument_and_dir_errors(tmp_path: Path) -> None:
    run_job, _ = _install_scripts(tmp_path)
    blocking_file = tmp_path / "not_a_dir"
    blocking_file.write_text("x", encoding="utf-8")

    bad_dir = subprocess.run(
        ["bash", str(run_job), "--kind", "job", "--dir", str(blocking_file / "sub"), "--", "true"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert bad_dir.returncode == 2
    assert "cannot create" in bad_dir.stderr

    bad_kind = subprocess.run(
        ["bash", str(run_job), "--kind", "weird", "--dir", str(tmp_path / "d"), "--", "true"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert bad_kind.returncode == 2

    no_cmd = subprocess.run(
        ["bash", str(run_job), "--kind", "job", "--dir", str(tmp_path / "d"), "--"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert no_cmd.returncode == 2


def test_launch_pid_detach_and_sigterm(tmp_path: Path) -> None:
    _, launch = _install_scripts(tmp_path)

    # 1) Normal run writes launch.pid (<host> <pgid>)
    res = subprocess.run(
        ["bash", str(launch), "--run-id", "run1", "--", "true"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res.returncode == 0
    pid_text = (tmp_path / ".solouvm" / "runs" / "run1" / "launch.pid").read_text(encoding="utf-8")
    parts = pid_text.strip().split()
    assert len(parts) == 2 and parts[1].isdigit()

    # 2) --detach returns immediately (< 2s) and writes launch.pid
    bg_marker = tmp_path / "detach_child.pid"
    t0 = time.monotonic()
    res_detach = subprocess.run(
        [
            "bash",
            str(launch),
            "--run-id",
            "run_detach",
            "--detach",
            "--",
            "bash",
            "-c",
            f'echo $$ > "{bg_marker}"; sleep 10',
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    elapsed = time.monotonic() - t0
    assert res_detach.returncode == 0
    assert elapsed < 2.0
    detach_pid_text = (tmp_path / ".solouvm" / "runs" / "run_detach" / "launch.pid").read_text(
        encoding="utf-8"
    )
    pgid = int(detach_pid_text.strip().split()[1])
    try:
        assert _wait_for(bg_marker.is_file, timeout=3.0)
        assert _pid_alive(pgid)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, signal.SIGTERM)
        _wait_for(lambda: not _pid_alive(pgid), timeout=3.0)

    # 3) SIGTERM to launch.sh kills make/child process group
    child_marker = tmp_path / "launch_term_child.pid"
    proc = subprocess.Popen(
        [
            "bash",
            str(launch),
            "--run-id",
            "run_term",
            "--",
            "bash",
            "-c",
            f'echo $$ > "{child_marker}"; sleep 20',
        ],
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _wait_for(child_marker.is_file, timeout=5.0)
        child_pid = int(child_marker.read_text(encoding="utf-8").strip())
        assert _pid_alive(child_pid)
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=10)
        assert rc == 130
        assert _wait_for(lambda: not _pid_alive(child_pid), timeout=3.0)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_fake_sim_scoreboard_plusarg() -> None:
    res = subprocess.run(
        [
            "bash",
            str(FAKE_SIM),
            "+SOLOUVM_FAKE_MODE=pass",
            "+SOLOUVM_FAKE_SCB=AXI_SCB:120",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert res.returncode == 0
    assert "[AXI_SCB] compared=120" in res.stdout
    scanned = scan(
        res.stdout.splitlines(),
        checks=Checks(),
        backend=BACKENDS["fake"],
        expects=[Expect(scoreboard="AXI_SCB", min=100)],
    )
    assert scanned.scoreboard["AXI_SCB"] == 120
