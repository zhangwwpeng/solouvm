"""Structured ``results.json`` serialization, reading and run comparison (formats.md §6)."""

from __future__ import annotations

import contextlib
import json
import math
import os
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.errors import ConfigError, InfraError

if TYPE_CHECKING:
    from solouvm.model import Result

__all__ = [
    "CATEGORY_ORDER",
    "PASS_STATUSES",
    "RESULTS_SCHEMA",
    "STATUS_ORDER",
    "atomic_write_text",
    "compare_results",
    "job_duration",
    "read_results_json",
    "render_json",
    "result_to_dict",
    "results_to_payload",
    "validate_results_payload",
    "write_results_json",
]

RESULTS_SCHEMA = 1

PASS_STATUSES: frozenset[str] = frozenset({"pass", "xfail", "skip"})

STATUS_ORDER: tuple[str, ...] = (
    "pass",
    "xfail",
    "skip",
    "fail",
    "timeout",
    "killed",
    "incomplete",
    "error",
    "xpass",
    "false_pass",
)

CATEGORY_ORDER: tuple[str, ...] = ("design", "execution", "environment")


def job_duration(item: Result | dict[str, object]) -> float:
    """Return total non-wave attempt duration in seconds for a ``Result`` or job dict."""
    attempts = item.get("attempts") if isinstance(item, dict) else item.attempts
    if not isinstance(attempts, list):
        return 0.0
    total = 0.0
    for att in attempts:
        if isinstance(att, dict) and not att.get("wave"):
            dur = att.get("duration_s")
            if (
                isinstance(dur, (int, float))
                and not isinstance(dur, bool)
                and math.isfinite(dur)
            ):
                total += float(dur)
    return round(total, 3)


def result_to_dict(result: Result) -> dict[str, object]:
    """Convert a :class:`Result` into the canonical ``results.json`` job object."""
    return {
        "job_id": result.job_id,
        "case": result.case,
        "seed": result.seed,
        "status": result.status,
        "judged_status": result.judged_status,
        "raw_status": result.raw_status,
        "category": result.category,
        "reason": result.reason,
        "first_error": dict(result.first_error) if result.first_error is not None else None,
        "signature": dict(result.signature) if result.signature is not None else None,
        "uvm": dict(result.uvm),
        "waivers": [dict(w) for w in result.waivers],
        "expect": [dict(e) for e in result.expect],
        "weak_signals": list(result.weak_signals),
        "known": list(result.known),
        "triage": dict(result.triage) if result.triage is not None else None,
        "flaky": bool(result.flaky),
        "attempts": [dict(a) for a in result.attempts],
        "repro": dict(result.repro),
    }


def _aggregate_waivers(
    results: Sequence[Result],
    waivers_summary: dict[str, object] | None,
) -> dict[str, object]:
    if waivers_summary is not None:
        return {
            "used": [dict(item) for item in waivers_summary.get("used", [])],  # type: ignore[union-attr]
            "unused": list(waivers_summary.get("unused", [])),  # type: ignore[arg-type]
            "expired": list(waivers_summary.get("expired", [])),  # type: ignore[arg-type]
        }
    totals: dict[str, int] = {}
    order: list[str] = []
    for res in results:
        for entry in res.waivers:
            wid = str(entry.get("id", ""))
            lines = entry.get("lines", 0)
            if not wid or not isinstance(lines, int) or isinstance(lines, bool) or lines <= 0:
                continue
            if wid not in totals:
                order.append(wid)
                totals[wid] = 0
            totals[wid] += lines
    return {
        "used": [{"id": wid, "lines": totals[wid]} for wid in order],
        "unused": [],
        "expired": [],
    }


def _group_signatures(results: Sequence[Result]) -> list[dict[str, object]]:
    groups: dict[str, dict[str, object]] = {}
    cases_by_sig: dict[str, set[str]] = {}
    for res in results:
        if res.status in PASS_STATUSES or res.signature is None:
            continue
        sig_id = str(res.signature.get("id", ""))
        sig_text = str(res.signature.get("text", ""))
        if not sig_id:
            continue
        if sig_id not in groups:
            groups[sig_id] = {
                "id": sig_id,
                "text": sig_text,
                "count": 0,
                "cases": [],
                "example": res.job_id,
            }
            cases_by_sig[sig_id] = set()
        groups[sig_id]["count"] = int(groups[sig_id]["count"]) + 1  # type: ignore[arg-type]
        cases_by_sig[sig_id].add(res.case)
    out: list[dict[str, object]] = []
    for sig_id, entry in groups.items():
        entry["cases"] = sorted(cases_by_sig[sig_id])
        out.append(entry)
    out.sort(key=lambda item: (-int(item["count"]), str(item["id"])))  # type: ignore[arg-type]
    return out


def results_to_payload(
    results: Sequence[Result],
    *,
    run_id: str,
    partial: bool,
    judged_at: datetime,
    waivers_summary: dict[str, object] | None = None,
    hints: Sequence[str] = (),
) -> dict[str, object]:
    """Assemble the canonical ``results.json`` payload with deterministic key order."""
    stamp = judged_at.astimezone() if judged_at.tzinfo is None else judged_at
    status_counts = Counter(res.status for res in results)
    cat_counts = Counter(res.category for res in results if res.category in CATEGORY_ORDER)
    summary: dict[str, object] = {
        "total": len(results),
        "passed": sum(1 for res in results if res.status in PASS_STATUSES),
        "by_status": {status: status_counts.get(status, 0) for status in STATUS_ORDER},
        "by_category": {cat: cat_counts.get(cat, 0) for cat in CATEGORY_ORDER},
        "duration_s": round(sum(job_duration(res) for res in results), 3),
    }
    return {
        "schema": RESULTS_SCHEMA,
        "run_id": run_id,
        "judged_at": stamp.isoformat(timespec="seconds"),
        "partial": bool(partial),
        "summary": summary,
        "jobs": [result_to_dict(res) for res in results],
        "signatures": _group_signatures(results),
        "waivers": _aggregate_waivers(results, waivers_summary),
        "hints": list(hints),
    }


def render_json(payload: dict[str, object]) -> str:
    """Serialize ``payload`` deterministically (2-space indent, UTF-8, trailing newline)."""
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` atomically via a same-directory temp file and ``os.replace``."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InfraError(
            f"failed to create directory {path.parent}: {exc}", code="PROVIDER_FAILED"
        ) from exc
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise InfraError(f"failed to write {path}: {exc}", code="PROVIDER_FAILED") from exc
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def write_results_json(path: Path, payload: dict[str, object]) -> None:
    """Write ``payload`` to ``results.json`` atomically."""
    atomic_write_text(path, render_json(payload))


def _is_non_bool_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _invalid_results(message: str, path: Path | None) -> ConfigError:
    return ConfigError(
        message,
        code="CFG_INVALID",
        file=path,
        hint="results.json is malformed; re-run 'solouvm judge' to regenerate it",
    )


def validate_results_payload(payload: object, *, path: Path | None = None) -> dict[str, object]:
    """Validate structure and numeric types of a ``results.json`` payload."""
    if not isinstance(payload, dict):
        raise _invalid_results("invalid results payload: expected a JSON object", path)
    schema = payload.get("schema")
    if type(schema) is not int:
        raise _invalid_results("invalid results payload: missing integer schema", path)
    if schema != RESULTS_SCHEMA:
        raise ConfigError(
            f"unsupported results schema {schema!r} (expected {RESULTS_SCHEMA}); re-judge",
            code="CFG_INVALID",
            file=path,
            hint="re-run 'solouvm judge' to regenerate results.json",
        )

    summary = payload.get("summary")
    if not isinstance(summary, dict):
        raise _invalid_results("invalid results payload: 'summary' must be an object", path)
    for key in ("total", "passed"):
        if key in summary and summary[key] is not None:
            val = summary[key]
            if not _is_non_bool_int(val) or val < 0:
                raise _invalid_results(
                    f"invalid results payload: summary.{key} must be a non-negative int",
                    path,
                )
    if "duration_s" in summary and summary["duration_s"] is not None:
        dur = summary["duration_s"]
        if not _is_finite_number(dur) or float(dur) < 0:
            raise _invalid_results(
                "invalid results payload: summary.duration_s must be a finite non-negative number",
                path,
            )
    for map_key in ("by_status", "by_category"):
        if map_key in summary and summary[map_key] is not None:
            mapping = summary[map_key]
            if not isinstance(mapping, dict) or not all(
                v is None or _is_non_bool_int(v) for v in mapping.values()
            ):
                raise _invalid_results(
                    f"invalid results payload: summary.{map_key} must map keys to ints",
                    path,
                )

    jobs = payload.get("jobs")
    if not isinstance(jobs, list) or not all(isinstance(j, dict) for j in jobs):
        raise _invalid_results("invalid results payload: 'jobs' must be a list of objects", path)
    for job in jobs:
        if "attempts" in job and job["attempts"] is not None:
            attempts = job["attempts"]
            if not isinstance(attempts, list) or not all(isinstance(a, dict) for a in attempts):
                raise _invalid_results(
                    "invalid results payload: job.attempts must be a list of objects",
                    path,
                )

    if "signatures" in payload and payload["signatures"] is not None:
        sigs = payload["signatures"]
        if not isinstance(sigs, list) or not all(isinstance(s, dict) for s in sigs):
            raise _invalid_results(
                "invalid results payload: 'signatures' must be a list of objects",
                path,
            )
        for sig in sigs:
            if (
                "count" in sig
                and sig["count"] is not None
                and not _is_non_bool_int(sig["count"])
            ):
                raise _invalid_results(
                    "invalid results payload: signature.count must be an int",
                    path,
                )

    if "waivers" in payload and payload["waivers"] is not None:
        waivers = payload["waivers"]
        if not isinstance(waivers, dict):
            raise _invalid_results("invalid results payload: 'waivers' must be an object", path)
        if "used" in waivers and waivers["used"] is not None:
            used = waivers["used"]
            if not isinstance(used, list) or not all(isinstance(w, dict) for w in used):
                raise _invalid_results(
                    "invalid results payload: waivers.used must be a list of objects",
                    path,
                )
            for item in used:
                if (
                    "lines" in item
                    and item["lines"] is not None
                    and not _is_non_bool_int(item["lines"])
                ):
                    raise _invalid_results(
                        "invalid results payload: waivers.used[].lines must be an int",
                        path,
                    )

    return payload


def read_results_json(path: Path) -> dict[str, object]:
    """Read and validate a ``results.json`` file (N-08)."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"results file {path} not found",
            code="RUN_NOT_FOUND",
            file=path,
            hint="run 'solouvm judge' first",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"cannot load results file {path}: {exc}",
            code="CFG_LOAD_FAILED",
            file=path,
        ) from exc
    return validate_results_payload(data, path=path)


def _status_for_compare(item: dict[str, object], mode: str) -> str:
    if mode == "judged":
        judged = item.get("judged_status")
        if isinstance(judged, str) and judged:
            return judged
    return str(item.get("status", ""))


def compare_results(
    current: dict[str, object],
    baseline: dict[str, object],
    *,
    mode: str = "status",
) -> dict[str, object]:
    """Compare ``current`` against ``baseline`` for new failures, newly fixed, and still failing."""
    if mode not in ("status", "judged"):
        raise ConfigError(
            f"invalid compare mode {mode!r}; expected 'status' or 'judged'",
            code="CFG_INVALID",
        )

    base_jobs: dict[str, dict[str, object]] = {}
    for item in baseline.get("jobs", []) or ():
        if isinstance(item, dict) and isinstance(item.get("job_id"), str):
            base_jobs[item["job_id"]] = item

    new_failures: list[dict[str, object]] = []
    newly_fixed: list[dict[str, object]] = []
    still_failing: list[dict[str, object]] = []

    for item in current.get("jobs", []) or ():
        if not isinstance(item, dict) or not isinstance(item.get("job_id"), str):
            continue
        jid = item["job_id"]
        cur_status = _status_for_compare(item, mode)
        cur_pass = cur_status in PASS_STATUSES
        prev = base_jobs.get(jid)
        prev_status = _status_for_compare(prev, mode) if prev is not None else None
        prev_pass = prev_status in PASS_STATUSES if prev_status is not None else None

        entry = {
            "job_id": jid,
            "case": item.get("case", ""),
            "seed": item.get("seed"),
            "status": cur_status,
            "prev_status": prev_status,
            "reason": item.get("reason", ""),
        }
        if not cur_pass and (prev_pass is None or prev_pass):
            new_failures.append(entry)
        elif cur_pass and prev_pass is False:
            newly_fixed.append(entry)
        elif not cur_pass and prev_pass is False:
            still_failing.append(entry)

    return {
        "baseline_run_id": baseline.get("run_id"),
        "new_failures": new_failures,
        "newly_fixed": newly_fixed,
        "still_failing": still_failing,
    }
