"""Verification progress overview and record inconsistency hints (M-20, M-22)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.collect.cases import collect_cases
from solouvm.errors import ConfigError
from solouvm.history import resolve_run_id, run_dir, runs_dir
from solouvm.model import Bug, Case, Stage, Todo
from solouvm.report.json import PASS_STATUSES, read_results_json
from solouvm.ui import format_table

if TYPE_CHECKING:
    from solouvm.config.schema import Config

__all__ = ["compute_progress", "format_progress"]

_UNASSIGNED = "<unassigned>"


def _latest_results_data(root: Path) -> dict[str, object] | None:
    try:
        latest_id = resolve_run_id(root, None)
    except ConfigError:
        latest_id = None

    if latest_id is not None:
        rpath = run_dir(root, latest_id) / "results.json"
        if rpath.is_file():
            try:
                return read_results_json(rpath)
            except ConfigError:
                return None

    base = runs_dir(root)
    if not base.is_dir():
        return None
    candidates: list[tuple[float, str, Path]] = []
    for entry in base.iterdir():
        if not entry.is_dir() or entry.is_symlink() or entry.name in ("latest", ".", ".."):
            continue
        rpath = entry / "results.json"
        if not rpath.is_file():
            continue
        try:
            data = read_results_json(rpath)
        except ConfigError:
            continue
        judged = data.get("judged_at")
        epoch = 0.0
        if isinstance(judged, str) and judged:
            raw = judged[:-1] + "+00:00" if judged.endswith("Z") else judged
            try:
                dt = datetime.fromisoformat(raw)
                epoch = (dt.astimezone() if dt.tzinfo is None else dt).timestamp()
            except ValueError:
                epoch = entry.stat().st_mtime
        else:
            epoch = entry.stat().st_mtime
        candidates.append((epoch, entry.name, rpath))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return read_results_json(candidates[0][2])


def _compute_hints(cases: list[Case], jobs: list[dict[str, object]]) -> list[str]:
    by_case_seed: dict[tuple[str, int], list[dict[str, object]]] = {}
    for job in jobs:
        cname = job.get("case")
        seed = job.get("seed")
        if isinstance(cname, str) and isinstance(seed, int) and not isinstance(seed, bool):
            by_case_seed.setdefault((cname, seed), []).append(job)

    hints: list[str] = []
    seen: set[str] = set()
    for case in cases:
        for rec in case.notes:
            if not isinstance(rec, Bug) or rec.seed is None:
                continue
            matching = by_case_seed.get((case.name, rec.seed), [])
            if not matching:
                continue
            all_passed = all(j.get("status") == "pass" for j in matching)
            failed = next((j for j in matching if j.get("status") not in PASS_STATUSES), None)
            msg: str | None = None
            if rec.status == "open" and all_passed:
                msg = f"{case.name}#{rec.id} may be fixed (seed {rec.seed} passed)"
            elif rec.status == "fixed" and all_passed:
                msg = f"{case.name}#{rec.id} can be marked verified (seed {rec.seed} passed)"
            elif rec.status == "verified" and failed is not None:
                msg = (
                    f"regression? {case.name}#{rec.id} was verified "
                    f"(seed {rec.seed} {failed.get('status')})"
                )
            if msg is not None and msg not in seen:
                seen.add(msg)
                hints.append(msg)
    return hints


def compute_progress(
    *,
    config: Config,
    cases: list[Case] | None = None,
    results_data: dict[str, object] | None = None,
    today: date | None = None,
) -> dict[str, object]:
    """Compute stage counts, latest run summary, per-owner bugs/todos, and hints (M-20, M-22)."""
    case_list = collect_cases(config) if cases is None else list(cases)
    ref_today = today or date.today()
    root = Path(config.root)

    effective_results = _latest_results_data(root) if results_data is None else results_data

    stages: dict[str, int] = {stage.value: 0 for stage in Stage}
    owner_stats: dict[str, dict[str, int]] = {}

    def _ensure_owner(name: str) -> dict[str, int]:
        if name not in owner_stats:
            owner_stats[name] = {
                "open_bugs": 0,
                "fixed_bugs": 0,
                "open_todos": 0,
                "overdue_todos": 0,
            }
        return owner_stats[name]

    overdue_todos: list[dict[str, object]] = []
    for case in case_list:
        stages[case.stage] = stages.get(case.stage, 0) + 1
        if case.owner:
            _ensure_owner(case.owner)
        for rec in case.notes:
            if isinstance(rec, Bug) and rec.status in ("open", "fixed"):
                owner = rec.owner if rec.owner is not None else (case.owner or _UNASSIGNED)
                bucket = _ensure_owner(owner)
                if rec.status == "open":
                    bucket["open_bugs"] += 1
                else:
                    bucket["fixed_bugs"] += 1
            elif isinstance(rec, Todo) and not rec.done:
                owner = rec.owner if rec.owner is not None else (case.owner or _UNASSIGNED)
                bucket = _ensure_owner(owner)
                bucket["open_todos"] += 1
                if rec.due is not None:
                    try:
                        due_dt = date.fromisoformat(rec.due)
                    except ValueError:
                        due_dt = None
                    if due_dt is not None and due_dt < ref_today:
                        bucket["overdue_todos"] += 1
                        overdue_todos.append(
                            {
                                "id": f"{case.name}#{rec.id}" if rec.id is not None else case.name,
                                "case": case.name,
                                "title": rec.title,
                                "owner": owner,
                                "due": rec.due,
                            }
                        )

    overdue_todos.sort(key=lambda item: (str(item["due"]), str(item["case"]), str(item["title"])))
    ordered_owners = sorted(k for k in owner_stats if k != _UNASSIGNED)
    if _UNASSIGNED in owner_stats:
        ordered_owners.append(_UNASSIGNED)
    by_owner = {k: owner_stats[k] for k in ordered_owners}

    latest_run: dict[str, object] | None = None
    hints: list[str] = []
    if effective_results is not None and effective_results:
        summary = effective_results.get("summary")
        summary_dict = summary if isinstance(summary, dict) else {}
        raw_total = summary_dict.get("total", 0)
        raw_passed = summary_dict.get("passed", 0)
        total = (
            int(raw_total) if isinstance(raw_total, int) and not isinstance(raw_total, bool) else 0
        )
        passed = (
            int(raw_passed)
            if isinstance(raw_passed, int) and not isinstance(raw_passed, bool)
            else 0
        )
        pass_rate = round(100.0 * passed / total, 1) if total > 0 else None
        latest_run = {
            "run_id": effective_results.get("run_id"),
            "passed": passed,
            "total": total,
            "pass_rate": pass_rate,
        }
        jobs = [j for j in (effective_results.get("jobs") or []) if isinstance(j, dict)]
        hints = _compute_hints(case_list, jobs)

    return {
        "project": config.project,
        "total_cases": len(case_list),
        "stages": stages,
        "latest_run": latest_run,
        "totals": {
            "open_bugs": sum(v["open_bugs"] for v in by_owner.values()),
            "fixed_bugs": sum(v["fixed_bugs"] for v in by_owner.values()),
            "open_todos": sum(v["open_todos"] for v in by_owner.values()),
            "overdue_todos": len(overdue_todos),
        },
        "by_owner": by_owner,
        "overdue_todos": overdue_todos,
        "hints": hints,
    }


def format_progress(data: dict[str, object]) -> str:
    """Format ``compute_progress`` output for terminal display (M-20)."""
    stages = data.get("stages")
    stages_dict = stages if isinstance(stages, dict) else {}
    stage_names = [s.value for s in Stage]
    stage_parts = [f"{name}={int(stages_dict.get(name, 0) or 0)}" for name in stage_names]
    lines: list[str] = [
        f"Project: {data.get('project', '')} ({data.get('total_cases', 0)} cases)",
        "Stages: " + "  ".join(stage_parts),
    ]

    latest = data.get("latest_run")
    if isinstance(latest, dict):
        rate = latest.get("pass_rate")
        rate_str = f" ({rate:.1f}%)" if isinstance(rate, (int, float)) else ""
        lines.append(
            f"Latest run: {latest.get('run_id')}  "
            f"{latest.get('passed', 0)}/{latest.get('total', 0)} passed{rate_str}"
        )
    else:
        lines.append("Latest run: none")

    by_owner = data.get("by_owner")
    if isinstance(by_owner, dict) and by_owner:
        owner_rows = [
            [
                str(owner),
                str(stats.get("open_bugs", 0)),
                str(stats.get("fixed_bugs", 0)),
                str(stats.get("open_todos", 0)),
                str(stats.get("overdue_todos", 0)),
            ]
            for owner, stats in by_owner.items()
            if isinstance(stats, dict)
        ]
        lines.append("")
        lines.append(
            format_table(["OWNER", "OPEN_BUGS", "FIXED_BUGS", "OPEN_TODOS", "OVERDUE"], owner_rows)
        )

    overdue = data.get("overdue_todos")
    if isinstance(overdue, list) and overdue:
        lines.append("")
        lines.append(f"Overdue todos ({len(overdue)}):")
        for item in overdue:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"  - {item.get('id')} [{item.get('owner')}] due {item.get('due')}: "
                f"{item.get('title')}"
            )

    hints = data.get("hints")
    if isinstance(hints, list) and hints:
        lines.append("")
        lines.append("Hints:")
        for hint in hints:
            lines.append(f"  - {hint}")

    return "\n".join(lines)
