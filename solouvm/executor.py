"""``_exec``: run exactly one planned case and write its ``status.json``.

This is the only place that sees exit codes, signals and timeouts, which is why
the pass/fail decision lives here and not in ``end``.
"""

from __future__ import annotations

import os
import signal as signal_module
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import logcheck, simulator
from . import plan as plan_mod
from . import status as status_mod
from .errors import EXIT_INFRA, EXIT_OK, InfraError
from .model import CaseSpec
from .util import now_iso

LOG_NAME = "sim.log"


def run(plan_path: Path, case_id: str, *, verbose: bool = False) -> int:
    """Entry point used by the generated Makefile; always returns 0 on test failures."""
    plan = plan_mod.load(plan_path)
    root = Path(plan["root"])
    spec = _find(plan, case_id)
    outdir = root / spec.outdir
    outdir.mkdir(parents=True, exist_ok=True)

    status_file = status_mod.status_path(outdir)
    base = status_mod.pending(plan, spec)
    try:
        status_mod.save(status_file, base)
    except OSError as exc:
        print(f"[solouvm] cannot write {status_file}: {exc}", file=sys.stderr)
        return EXIT_INFRA

    attempts: list[dict[str, Any]] = []
    final: dict[str, Any] = {}
    for attempt in range(1, spec.retry + 2):
        result = _run_once(plan, spec, outdir, attempt=attempt, verbose=verbose)
        attempts.append(
            {
                "n": attempt,
                "status": result["status"],
                "raw_status": result["raw_status"],
                "rc": result["rc"],
                "signal": result["signal"],
                "timeout": result["timeout"],
                "duration_s": result["duration_s"],
                "reason": result.get("reason", ""),
            }
        )
        final = result
        if result["status"] == "pass":
            break

    payload = {**base, **final}
    payload["attempts"] = attempts
    payload["duration_s"] = round(sum(item["duration_s"] for item in attempts), 3)
    payload["finished_at"] = now_iso()
    payload["status"], payload["verdict"] = _apply_expect(
        payload["raw_status"], spec.expect_fail
    )
    status_mod.save(status_file, payload)

    flag = "" if payload["status"] == "pass" else "  <-- " + payload["status"].upper()
    print(
        f"[solouvm] {spec.case_id}: {payload['status']}"
        f" ({payload.get('reason') or 'ok'}){flag}"
    )
    return EXIT_OK


def _run_once(
    plan: Mapping[str, Any],
    spec: CaseSpec,
    outdir: Path,
    *,
    attempt: int,
    verbose: bool,
) -> dict[str, Any]:
    cmd = [str(part) for part in plan["exec"]["sim_cmd"]] + simulator.plusargs(spec.to_json())
    env = _build_env(plan, spec)
    log_path = outdir / LOG_NAME
    started = time.monotonic()

    timed_out = False
    signal_name: str | None = None
    rc: int | None = None
    launch_error: str | None = None

    try:
        with log_path.open("w", encoding="utf-8", errors="replace") as sink:
            proc = subprocess.Popen(
                cmd,
                cwd=str(outdir),
                stdout=subprocess.PIPE if verbose else sink,
                stderr=subprocess.STDOUT,
                env=env,
                start_new_session=True,  # own group: timeout can kill forks too
            )
            if verbose:
                _tee(proc, sink)
            try:
                rc = proc.wait(timeout=spec.timeout_s)
            except subprocess.TimeoutExpired:
                timed_out = True
                _kill_group(proc)
                rc = proc.returncode
    except FileNotFoundError as exc:
        launch_error = f"cannot execute {cmd[0]!r}: {exc}"
    except OSError as exc:
        launch_error = f"failed to start simulation: {exc}"

    duration = round(time.monotonic() - started, 3)

    if launch_error:
        verdict: dict[str, Any] = {
            "status": "error",
            "raw_status": "error",
            "reason": launch_error,
            "uvm": {"error": 0, "fatal": 0, "warning": 0, "info": 0},
            "checks_fired": [{"kind": "infra", "message": launch_error}],
        }
    else:
        signal_name = None if timed_out else _signal_name(rc)
        verdict = logcheck.judge_file(
            log_path,
            checks=plan.get("checks") or {},
            rc=rc,
            signal=signal_name,
            timed_out=timed_out,
        )

    return {
        "status": verdict["status"],
        "raw_status": verdict["raw_status"],
        "verdict": None,
        "reason": verdict.get("reason", ""),
        "rc": rc,
        "signal": signal_name,
        "timeout": timed_out,
        "cmd": cmd,
        "cwd": str(outdir),
        "log": LOG_NAME,
        "uvm": verdict.get("uvm", {}),
        "checks_fired": verdict.get("checks_fired", []),
        "duration_s": duration,
        "started_at": now_iso(),
    }


def _apply_expect(raw: str, expect_fail: bool) -> tuple[str, str | None]:
    """Apply ``expect_fail`` *after* judging; logcheck stays expect-agnostic."""
    if not expect_fail:
        return raw, None
    if raw == "pass":
        return "xpass", "XPASS"
    if raw == "fail":
        return "xfail", "XFAIL"
    return raw, None


def _build_env(plan: Mapping[str, Any], spec: CaseSpec) -> dict[str, str]:
    env = dict(os.environ)
    env.update({str(k): str(v) for k, v in (plan["exec"].get("env") or {}).items()})
    env.update({str(k): str(v) for k, v in spec.env.items()})
    env.setdefault("LC_ALL", "C.UTF-8")
    return env


def _kill_group(proc: subprocess.Popen[Any]) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal_module.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal_module.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - unkillable process
        pass


def _signal_name(rc: int | None) -> str | None:
    if rc is None or rc >= 0:
        return None
    try:
        return signal_module.Signals(-rc).name
    except ValueError:  # pragma: no cover - unknown signal number
        return f"SIG{-rc}"


def _tee(proc: subprocess.Popen[Any], sink: Any) -> None:
    assert proc.stdout is not None
    for raw in proc.stdout:
        text = raw.decode("utf-8", errors="replace")
        sink.write(text)
        sys.stdout.write(text)
        sys.stdout.flush()


def _find(plan: Mapping[str, Any], case_id: str) -> CaseSpec:
    for item in plan_mod.specs(plan):
        if item.case_id == case_id:
            return item
    raise InfraError(
        f"case_id {case_id!r} is not in the plan",
        hint="regenerate the plan with `solouvm gen`",
    )
