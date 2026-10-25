"""``status.json`` (per case) and ``results.json`` (aggregate) handling."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .errors import SolouvmError
from .model import BAD_STATUSES, CaseSpec
from .util import now_iso, read_json, write_json_atomic

SCHEMA = 1
STATUS_FILE = "status.json"


def status_path(outdir: Path) -> Path:
    return outdir / STATUS_FILE


def pending(plan: Mapping[str, Any], spec: CaseSpec) -> dict[str, Any]:
    """Placeholder written *before* the simulation starts.

    It protects against a crashed ``_exec`` leaving the previous run's ``pass``
    behind: ``end`` will read ``pending`` and classify it as an error.
    """
    return {
        "schema": SCHEMA,
        "run_id": plan["run_id"],
        "case_id": spec.case_id,
        "case": spec.case,
        "seed": spec.seed,
        "status": "pending",
        "raw_status": "pending",
        "verdict": None,
        "rc": None,
        "signal": None,
        "timeout": False,
        "attempts": [],
        "duration_s": 0.0,
        "started_at": now_iso(),
        "finished_at": None,
        "cmd": [],
        "cwd": spec.outdir,
        "log": "sim.log",
        "uvm": {"error": 0, "fatal": 0, "warning": 0, "info": 0},
        "checks_fired": [],
    }


def error_status(
    plan: Mapping[str, Any], spec: CaseSpec, message: str, *, status: str = "error"
) -> dict[str, Any]:
    payload = pending(plan, spec)
    payload.update(
        {
            "status": status,
            "raw_status": status,
            "reason": message,
            "finished_at": now_iso(),
            "checks_fired": [{"kind": "infra", "message": message}],
        }
    )
    return payload


def save(path: Path, payload: Mapping[str, Any]) -> None:
    write_json_atomic(path, payload)


def load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = read_json(path)
    except (OSError, ValueError, SolouvmError):
        return None
    return payload if isinstance(payload, dict) else None


def skip_status(plan: Mapping[str, Any], spec: CaseSpec, message: str) -> dict[str, Any]:
    payload = error_status(plan, spec, message, status="skip")
    payload["verdict"] = None
    return payload


def collect(
    plan: Mapping[str, Any],
    specs: Iterable[CaseSpec],
    root: Path,
    *,
    allow_missing: bool = False,
) -> list[dict[str, Any]]:
    """Read every planned status; missing or stale entries become ``error``.

    ``allow_missing=True`` downgrades *never-run* cases to ``skip`` (partial
    runs / sharded farms). Stale and pending statuses stay errors: they mean a
    run started and died, which must never be reported as a pass.
    """
    out: list[dict[str, Any]] = []
    for spec in specs:
        path = status_path(root / spec.outdir)
        payload = load(path)
        if payload is None:
            message = f"missing {STATUS_FILE}"
            out.append(
                skip_status(plan, spec, message)
                if allow_missing
                else error_status(plan, spec, message)
            )
            continue
        if payload.get("run_id") != plan["run_id"]:
            out.append(
                error_status(
                    plan,
                    spec,
                    f"stale status from run {payload.get('run_id')}",
                )
            )
            continue
        if payload.get("status") == "pending":
            out.append(error_status(plan, spec, "run did not finish (pending status)"))
            continue
        payload.setdefault("case_id", spec.case_id)
        out.append(payload)
    return out


def aggregate(plan: Mapping[str, Any], statuses: list[Mapping[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in statuses:
        key = str(item.get("status") or "error")
        counts[key] = counts.get(key, 0) + 1
    for key in ("pass", "xfail", "fail", "timeout", "error", "killed", "xpass", "skip"):
        counts.setdefault(key, 0)
    return {
        "schema": SCHEMA,
        "run_id": plan["run_id"],
        "project": plan.get("project"),
        "simulator": plan.get("simulator"),
        "created_at": plan.get("created_at"),
        "finished_at": now_iso(),
        "counts": counts,
        "cases": list(statuses),
    }


def is_bad(status: str | None) -> bool:
    return (status or "error") in BAD_STATUSES


def failing(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [item for item in payload["cases"] if is_bad(item.get("status"))]


def exit_code(payload: Mapping[str, Any]) -> int:
    from .errors import EXIT_OK, EXIT_TEST_FAIL

    return EXIT_OK if not failing(payload) else EXIT_TEST_FAIL
