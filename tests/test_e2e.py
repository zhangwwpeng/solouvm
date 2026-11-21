"""End-to-end CLI and fake simulator regression tests (Q-01, task-25)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from solouvm.collect.cases import collect_cases
from solouvm.config.load import load_config
from solouvm.explain import EXPLAIN_SECTIONS
from solouvm.triage.packet import PACKET_SECTIONS

REPO_ROOT = Path(__file__).resolve().parent.parent

TRIAGE_DOC = (
    "## 现象\nUVM_ERROR from AXI_SCB.\n\n"
    "## 排查过程\nInspected sim.log and scoreboard mismatch.\n\n"
    "## 证据\nLine 4 reports mismatch expect 0x00 got 0x1f.\n\n"
    "## 结论\nConfirmed RTL design bug on write data path.\n\n"
    "## 建议\nFix slave write channel mux.\n"
)


def _cli_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{REPO_ROOT}:{py_path}" if py_path else str(REPO_ROOT)
    if extra:
        env.update(extra)
    return env


def _run_cli(
    cwd: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout: float = 60.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "solouvm", *args],
        cwd=cwd,
        env=_cli_env(env),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _parse_json_envelope(stdout: str) -> dict[str, object]:
    dec = json.JSONDecoder()
    idx = 0
    objs: list[dict[str, object]] = []
    length = len(stdout)
    while idx < length:
        while idx < length and stdout[idx].isspace():
            idx += 1
        if idx >= length:
            break
        obj, idx = dec.raw_decode(stdout, idx)
        objs.append(obj)
    assert objs, f"no JSON object found in output: {stdout!r}"
    for ev in objs[:-1]:
        assert "event" in ev, f"unexpected trailing JSON before final envelope: {objs}"
    return objs[-1]


def _latest_run_dir(proj: Path) -> Path:
    latest = proj / ".solouvm" / "runs" / "latest"
    if latest.exists():
        return latest.resolve()
    runs = sorted((proj / ".solouvm" / "runs").iterdir())
    assert runs, "no runs directory created"
    return runs[-1]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_e2e_full_lifecycle_and_clean_triage(tmp_path: Path) -> None:
    res = _run_cli(tmp_path, "init", "--json")
    assert res.returncode == 0, res.stderr
    init_json = json.loads(res.stdout)
    assert list(init_json.keys()) == ["schema", "command", "ok", "data", "truncated", "next"]
    assert init_json["ok"] is True

    assert _run_cli(tmp_path, "validate").returncode == 0

    listed = _run_cli(tmp_path, "list", "--json")
    assert listed.returncode == 0, listed.stderr
    assert json.loads(listed.stdout)["data"]["total_jobs"] >= 1

    assert _run_cli(tmp_path, "gen").returncode == 0
    rdir = _latest_run_dir(tmp_path)
    assert (rdir / "plan.json").is_file()
    assert (rdir / "env.json").is_file()
    assert (rdir / "run.mk").is_file()
    assert (tmp_path / ".solouvm" / "gen" / "bin" / "run_job.sh").is_file()

    reg = _run_cli(tmp_path, "regress", "--json")
    assert reg.returncode == 0, reg.stderr
    reg_payload = _parse_json_envelope(reg.stdout)
    assert list(reg_payload.keys()) == ["schema", "command", "ok", "data", "truncated", "next"]
    reg_data = reg_payload["data"]
    assert reg_data["summary"]["passed"] == reg_data["summary"]["total"]
    job_id = reg_data["jobs"][0]["job_id"]

    assert _run_cli(tmp_path, "judge").returncode == 0
    first_res = json.loads((_latest_run_dir(tmp_path) / "results.json").read_text())
    assert _run_cli(tmp_path, "judge").returncode == 0
    second_res = json.loads((_latest_run_dir(tmp_path) / "results.json").read_text())
    first_res.pop("judged_at", None)
    second_res.pop("judged_at", None)
    assert first_res == second_res

    exp = _run_cli(tmp_path, "explain", job_id)
    assert exp.returncode == 0, exp.stderr
    for sec in EXPLAIN_SECTIONS:
        assert sec in exp.stdout

    case_py = tmp_path / "case.py"
    case_py.write_text(case_py.read_text().replace('stage="wip"', 'stage="ready"'))
    assert _run_cli(tmp_path, "regress", env={"SOLOUVM_RUN_ID": "r1"}).returncode == 0
    assert _run_cli(tmp_path, "regress", env={"SOLOUVM_RUN_ID": "r2"}).returncode == 0

    tp = _run_cli(tmp_path, "triage", "plan", "--run", "r2", "--json")
    assert tp.returncode == 0, tp.stderr
    assert json.loads(tp.stdout)["data"]["total"] == 0
    assert _run_cli(tmp_path, "triage", "status", "--run", "r2").returncode == 0


@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_rc"),
    [
        ("pass", "pass", 0),
        ("uvm_error", "fail", 1),
        ("uvm_fatal", "fail", 1),
        ("no_summary", "fail", 1),
        ("rc3", "fail", 1),
        ("hang", "timeout", 1),
        ("segv", "killed", 1),
        ("license", "error", 1),
        ("nonuvm_pass", "pass", 0),
    ],
)
def test_e2e_fake_modes(
    tmp_path: Path,
    mode: str,
    expected_status: str,
    expected_rc: int,
) -> None:
    assert _run_cli(tmp_path, "init").returncode == 0
    conf_p = tmp_path / "solouvm.conf.py"
    conf_p.write_text(
        conf_p.read_text() + '\nCFG["run"] = {"timeout": "2s"}\n',
        encoding="utf-8",
    )
    (tmp_path / "case.py").write_text(
        "from solouvm import Case\n"
        "CASES = [\n"
        f'    Case(name="c_{mode}", uvm_test="axi_smoke_test", '
        f'opts=["+SOLOUVM_FAKE_MODE={mode}"], expect=[]),\n'
        "]\n",
        encoding="utf-8",
    )

    res = _run_cli(tmp_path, "regress", "--json", timeout=30.0)
    assert res.returncode == expected_rc, f"mode={mode}: {res.stdout}\n{res.stderr}"
    payload = _parse_json_envelope(res.stdout)
    assert payload["data"]["jobs"][0]["status"] == expected_status, payload


def test_e2e_triage_accept_and_reject(tmp_path: Path) -> None:
    assert _run_cli(tmp_path, "init").returncode == 0
    (tmp_path / "case.py").write_text(
        "from solouvm import Case\n"
        "CASES = [\n"
        '    Case(name="smoke", uvm_test="axi_smoke_test", '
        'opts=["+SOLOUVM_FAKE_MODE=uvm_error"], expect=[]),\n'
        "]\n",
        encoding="utf-8",
    )

    r1 = _run_cli(tmp_path, "regress", "--json", env={"SOLOUVM_RUN_ID": "r_acc"})
    assert r1.returncode == 1
    job_id = _parse_json_envelope(r1.stdout)["data"]["jobs"][0]["job_id"]

    tp = _run_cli(tmp_path, "triage", "plan", "--run", "r_acc", "--json")
    assert tp.returncode == 0
    tp_data = json.loads(tp.stdout)["data"]
    assert tp_data["total"] == 1
    task_id = tp_data["tasks"][0]["id"]

    pkt = _run_cli(tmp_path, "--role", "triage", "triage", "packet", task_id, "--run", "r_acc")
    assert pkt.returncode == 0, pkt.stderr
    for sec in PACKET_SECTIONS:
        assert sec in pkt.stdout

    b1 = _run_cli(
        tmp_path,
        "--role",
        "triage",
        "bug",
        "add",
        "smoke",
        "--run",
        "r_acc",
        "--job",
        job_id,
        "--title",
        "AXI SCB mismatch",
        "--json",
    )
    assert b1.returncode == 0, b1.stderr
    bug_ref_1 = json.loads(b1.stdout)["data"]["ref"]

    tw = _run_cli(
        tmp_path,
        "--role",
        "triage",
        "triage",
        "write",
        task_id,
        "--run",
        "r_acc",
        "--verdict",
        "design_bug",
        "--confidence",
        "high",
        "--summary",
        "RTL write mux dropped upper byte",
        "--evidence",
        "log:4",
        "--bug",
        bug_ref_1,
        "--doc",
        "-",
        input_text=TRIAGE_DOC,
    )
    assert tw.returncode == 0, tw.stderr
    assert _run_cli(tmp_path, "triage", "status", "--run", "r_acc").returncode == 1

    rev = _run_cli(tmp_path, "triage", "review", task_id, "--run", "r_acc", "--accept")
    assert rev.returncode == 0, rev.stderr
    assert _run_cli(tmp_path, "triage", "status", "--run", "r_acc").returncode == 0

    r2 = _run_cli(tmp_path, "regress", "--json", env={"SOLOUVM_RUN_ID": "r_rej"})
    assert r2.returncode == 1
    job_id_2 = _parse_json_envelope(r2.stdout)["data"]["jobs"][0]["job_id"]
    task2 = json.loads(_run_cli(tmp_path, "triage", "plan", "--run", "r_rej", "--json").stdout)[
        "data"
    ]["tasks"][0]["id"]
    b2 = _run_cli(
        tmp_path,
        "--role",
        "triage",
        "bug",
        "add",
        "smoke",
        "--run",
        "r_rej",
        "--job",
        job_id_2,
        "--title",
        "wrong guess",
        "--json",
    )
    bug_ref_2 = json.loads(b2.stdout)["data"]["ref"]
    assert (
        _run_cli(
            tmp_path,
            "--role",
            "triage",
            "triage",
            "write",
            task2,
            "--run",
            "r_rej",
            "--verdict",
            "tb_bug",
            "--confidence",
            "low",
            "--summary",
            "guessed tb issue",
            "--evidence",
            "log:4",
            "--bug",
            bug_ref_2,
            "--doc",
            "-",
            input_text=TRIAGE_DOC,
        ).returncode
        == 0
    )
    assert (
        _run_cli(
            tmp_path,
            "triage",
            "review",
            task2,
            "--run",
            "r_rej",
            "--reject",
            "not caused by tb",
        ).returncode
        == 0
    )
    cases = collect_cases(load_config(tmp_path / "solouvm.conf.py"))
    bug2 = next(r for r in cases[0].notes if getattr(r, "id", None) == 2)
    assert bug2.status == "invalid"


def test_e2e_compile_fail_exit_2_and_json_error(tmp_path: Path) -> None:
    assert _run_cli(tmp_path, "init").returncode == 0
    conf_p = tmp_path / "solouvm.conf.py"
    conf_p.write_text(
        conf_p.read_text()
        + '\nCFG["builds"] = {"default": {"compile_opts": ["+SOLOUVM_FAKE_MODE=badbuild"]}}\n',
        encoding="utf-8",
    )
    res = _run_cli(tmp_path, "regress", "--json")
    assert res.returncode == 2
    payload = _parse_json_envelope(res.stdout)
    assert payload["ok"] is True, payload
    assert payload["data"]["jobs"][0]["status"] == "error"
    assert "build" in payload["data"]["jobs"][0]["reason"].lower()

    conf_p.write_text(conf_p.read_text() + '\nCFG["unknown_flag"] = True\n', encoding="utf-8")
    bad = _run_cli(tmp_path, "--json", "regress")
    assert bad.returncode == 2
    err = _parse_json_envelope(bad.stdout)
    assert err["ok"] is False
    assert list(err["error"].keys()) == ["code", "message", "file", "line", "hint"]
    assert err["error"]["code"] == "CFG_UNKNOWN_KEY"


def test_e2e_concurrent_runs(tmp_path: Path) -> None:
    assert _run_cli(tmp_path, "init").returncode == 0
    p1 = subprocess.Popen(
        [sys.executable, "-m", "solouvm", "regress", "--json"],
        cwd=tmp_path,
        env=_cli_env({"SOLOUVM_RUN_ID": "run_a"}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    p2 = subprocess.Popen(
        [sys.executable, "-m", "solouvm", "regress", "--json"],
        cwd=tmp_path,
        env=_cli_env({"SOLOUVM_RUN_ID": "run_b"}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert p1.wait(timeout=60) == 0
    assert p2.wait(timeout=60) == 0
    ra = json.loads((tmp_path / ".solouvm" / "runs" / "run_a" / "results.json").read_text())
    rb = json.loads((tmp_path / ".solouvm" / "runs" / "run_b" / "results.json").read_text())
    assert ra["run_id"] == "run_a"
    assert rb["run_id"] == "run_b"
    assert ra["summary"]["passed"] == 1
    assert rb["summary"]["passed"] == 1


def test_e2e_sigint_partial_and_no_orphan(tmp_path: Path) -> None:
    assert _run_cli(tmp_path, "init").returncode == 0
    conf_p = tmp_path / "solouvm.conf.py"
    conf_p.write_text(conf_p.read_text() + '\nCFG["run"] = {"timeout": "30s"}\n', encoding="utf-8")
    (tmp_path / "case.py").write_text(
        "from solouvm import Case\n"
        'CASES = [Case(name="hang", opts=["+SOLOUVM_FAKE_MODE=hang"], expect=[])]\n',
        encoding="utf-8",
    )

    proc = subprocess.Popen(
        [sys.executable, "-m", "solouvm", "regress", "--seed", "1"],
        cwd=tmp_path,
        env=_cli_env({"SOLOUVM_RUN_ID": "run_sig"}),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    sim_log = tmp_path / ".solouvm" / "runs" / "run_sig" / "hang__1" / "try1" / "sim.log"
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and not sim_log.exists():
        time.sleep(0.1)
    assert sim_log.exists(), "hang job did not start in time"
    time.sleep(0.2)

    proc.send_signal(signal.SIGINT)
    rc = proc.wait(timeout=30)
    assert rc == 130

    res = json.loads((tmp_path / ".solouvm" / "runs" / "run_sig" / "results.json").read_text())
    assert res["partial"] is True

    def _wait_dead(pid: int) -> bool:
        stop_by = time.monotonic() + 3.0
        while time.monotonic() < stop_by:
            if not _pid_alive(pid):
                return True
            time.sleep(0.05)
        return not _pid_alive(pid)

    pid_file = tmp_path / ".solouvm" / "runs" / "run_sig" / "launch.pid"
    if pid_file.is_file():
        parts = pid_file.read_text().strip().split()
        if len(parts) >= 2 and parts[1].isdigit():
            assert _wait_dead(int(parts[1]))
    facts = sim_log.parent / "job.facts"
    if facts.is_file():
        for line in facts.read_text().splitlines():
            if line.startswith("pid="):
                pid_val = line.split("=", 1)[1].strip()
                if pid_val.isdigit():
                    assert _wait_dead(int(pid_val))
