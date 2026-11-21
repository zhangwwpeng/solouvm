"""Triage review workflow, status reporting, and staleness tracking (AI-09, AI-11, AI-20)."""

from __future__ import annotations

import contextlib
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.collect.cases import collect_cases, collect_cases_with_sources
from solouvm.errors import ConfigError, RecordError, RunActiveError
from solouvm.history import resolve_run_id, run_dir
from solouvm.judge import judge_run, write_results
from solouvm.model import Bug, Result, Triage
from solouvm.notes.commands import parse_record_ref
from solouvm.plan import read_plan
from solouvm.providers import get_provider
from solouvm.records import append_record, record_lock, replace_record
from solouvm.report.json import read_results_json
from solouvm.roles import check_role
from solouvm.triage.plan import (
    TASKS_SCHEMA,
    atomic_write_json,
    read_tasks_json,
    tasks_json_path,
    tasks_lock,
)

if TYPE_CHECKING:
    from solouvm.config.schema import Config

__all__ = ["review_task", "triage_status"]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _invalidate_rejected_bugs(
    *,
    config: Config,
    task_id: str,
    bug_refs: list[str],
    reason_text: str,
) -> None:
    root = Path(config.root)
    today_str = date.today().isoformat()
    for ref in bug_refs:
        case_name, bug_id = parse_record_ref(ref)
        with record_lock(root):
            sources = collect_cases_with_sources(config)
            target_cs = next((cs for cs in sources if cs.case.name == case_name), None)
            if target_cs is None:
                raise RecordError(f"case {case_name!r} not found", code="RECORD_INVALID")
            bug = next(
                (r for r in target_cs.case.notes if isinstance(r, Bug) and r.id == bug_id),
                None,
            )
            if bug is None:
                raise RecordError(f"bug {ref!r} not found", code="RECORD_INVALID")
            note_line = f"Triage rejected ({task_id}): {reason_text}"
            new_body = f"{bug.body.rstrip()}\n{note_line}" if bug.body.strip() else note_line
            updated = replace(
                bug,
                status="invalid",
                found=bug.found or today_str,
                body=new_body,
            )
            replace_record(target_cs.file, case_name, updated)


def _update_case_triage_reviews(
    *,
    config: Config,
    task: dict[str, object],
    review_obj: dict[str, object],
) -> None:
    root = Path(config.root)
    task_id = str(task.get("id", ""))
    res_data = task.get("result")
    assert isinstance(res_data, dict)
    task_cases = [str(c) for c in (task.get("cases") or []) if isinstance(c, str) and c]

    for case_name in task_cases:
        with record_lock(root):
            sources = collect_cases_with_sources(config)
            target_cs = next((cs for cs in sources if cs.case.name == case_name), None)
            if target_cs is None:
                raise RecordError(f"case {case_name!r} not found", code="RECORD_INVALID")
            existing = next(
                (r for r in target_cs.case.notes if isinstance(r, Triage) and r.task == task_id),
                None,
            )
            if existing is not None:
                updated = replace(existing, review=dict(review_obj))
                replace_record(target_cs.file, case_name, updated)
            else:
                judged_map = {
                    str(j["job_id"]): str(j["judged_status"])
                    for j in (task.get("jobs") or [])
                    if isinstance(j, dict) and j.get("job_id")
                }
                triage_rec = Triage(
                    task=task_id,
                    verdict=str(res_data.get("verdict", "")),
                    confidence=str(res_data.get("confidence", "")),
                    summary=str(res_data.get("summary", "")),
                    evidence=[str(e) for e in (res_data.get("evidence") or [])],
                    judged_status=judged_map,
                    doc=str(res_data["doc"]) if isinstance(res_data.get("doc"), str) else None,
                    bugs=[str(b) for b in (res_data.get("bugs") or [])],
                    review=dict(review_obj),
                    author=(
                        str(res_data["author"]) if isinstance(res_data.get("author"), str) else None
                    ),
                    at=str(res_data["at"]) if isinstance(res_data.get("at"), str) else None,
                )
                append_record(target_cs.file, case_name, triage_rec)


def _rejudge_run_after_review(config: Config, run_id: str) -> None:
    root = Path(config.root)
    rdir = run_dir(root, run_id)
    plan_path = rdir / "plan.json"
    results_path = rdir / "results.json"
    if not plan_path.is_file() or not results_path.is_file():
        return

    plan = read_plan(plan_path)
    results_data = read_results_json(results_path)
    is_partial = bool(results_data.get("partial", False))
    allow_missing = any(
        isinstance(j, dict) and j.get("judged_status") == "skip"
        for j in (results_data.get("jobs") or ())
    )

    try:
        provider = get_provider(config, plan.provider)
    except ConfigError:
        provider = get_provider(config, "local")

    fresh_cases = collect_cases(config)
    try:
        rejudged = judge_run(
            config=config,
            plan=plan,
            provider=provider,
            cases=fresh_cases,
            allow_missing=allow_missing,
            partial=is_partial,
        )
    except RunActiveError:
        if is_partial:
            return
        try:
            rejudged = judge_run(
                config=config,
                plan=plan,
                provider=provider,
                cases=fresh_cases,
                allow_missing=allow_missing,
                partial=True,
            )
            is_partial = True
        except RunActiveError:
            return

    write_results(
        rdir,
        rejudged,
        run_id=run_id,
        partial=is_partial,
        judged_at=datetime.now().astimezone().replace(microsecond=0),
        config=config,
        cases=fresh_cases,
    )


def review_task(
    *,
    config: Config,
    run_id: str,
    task_id: str,
    accept: bool,
    reason: str | None = None,
    author: str = "main",
    role: str = "main",
) -> dict[str, object]:
    """Accept or reject a triaged task, update case records, and re-judge results."""
    check_role(role, "triage review")

    if not accept and (not isinstance(reason, str) or not reason.strip()):
        raise ConfigError(
            "rejecting a triage task requires a non-empty --reject reason",
            code="TRIAGE_INVALID",
        )
    if accept and reason is not None and not isinstance(reason, str):
        raise ConfigError("review reason must be a string or None", code="TRIAGE_INVALID")

    root = Path(config.root)
    actual_run = resolve_run_id(root, run_id)
    tpath = tasks_json_path(root, actual_run)
    tasks_data = read_tasks_json(tpath)

    task = next(
        (t for t in tasks_data["tasks"] if isinstance(t, dict) and t.get("id") == task_id),
        None,
    )
    if task is None:
        raise ConfigError(
            f"task {task_id!r} not found in run {actual_run!r}",
            code="TASK_NOT_FOUND",
        )
    res_data = task.get("result")
    if not isinstance(res_data, dict):
        raise ConfigError(
            f"task {task_id!r} has not been triaged yet (result is missing)",
            code="TRIAGE_INVALID",
        )

    actor = author.strip() if isinstance(author, str) and author.strip() else role
    clean_reason = reason.strip() if isinstance(reason, str) and reason.strip() else None
    review_obj: dict[str, object] = {
        "decision": "accepted" if accept else "rejected",
        "reason": clean_reason,
        "author": actor,
        "at": _now_iso(),
    }

    _update_case_triage_reviews(config=config, task=task, review_obj=review_obj)
    if not accept:
        bug_refs = [str(b) for b in (res_data.get("bugs") or []) if isinstance(b, str) and b]
        assert clean_reason is not None
        _invalidate_rejected_bugs(
            config=config,
            task_id=task_id,
            bug_refs=bug_refs,
            reason_text=clean_reason,
        )

    with tasks_lock(root, actual_run):
        latest_data = read_tasks_json(tpath)
        tasks_list = [dict(t) for t in latest_data["tasks"] if isinstance(t, dict)]
        target_task = next((t for t in tasks_list if t.get("id") == task_id), None)
        if target_task is None:
            raise ConfigError(
                f"task {task_id!r} not found in run {actual_run!r}",
                code="TASK_NOT_FOUND",
            )
        target_task["review"] = review_obj
        payload: dict[str, object] = {
            "schema": TASKS_SCHEMA,
            "run_id": latest_data.get("run_id", actual_run),
            "policy": latest_data.get("policy", {}),
            "tasks": tasks_list,
        }
        atomic_write_json(tpath, payload)

    _rejudge_run_after_review(config, actual_run)
    return target_task


def _task_seq(task: dict[str, object], fallback: int) -> int:
    tid = str(task.get("id", ""))
    _, _, tail = tid.rpartition("/")
    if tail.isascii() and tail.isdigit():
        return int(tail)
    return fallback


def _task_priority_bucket(task: dict[str, object]) -> int:
    if task.get("review") is None:
        return 0
    if bool(task.get("stale", False)):
        return 1
    return 2


def _refresh_stale_flags(
    tasks_list: list[dict[str, object]],
    current_judged: dict[str, str],
) -> bool:
    stale_changed = False
    for task in tasks_list:
        was_stale = bool(task.get("stale", False))
        computed_stale = False
        if current_judged:
            for jentry in task.get("jobs") or ():
                if not isinstance(jentry, dict):
                    continue
                jid = str(jentry.get("job_id", ""))
                if jid in current_judged and current_judged[jid] != jentry.get("judged_status"):
                    computed_stale = True
                    break
        new_stale = was_stale or computed_stale
        if new_stale != was_stale or "stale" not in task:
            stale_changed = True
        task["stale"] = new_stale
    return stale_changed


def triage_status(
    *,
    config: Config,
    run_id: str,
    results: list[Result] | None = None,
) -> tuple[list[dict[str, object]], int]:
    """Return prioritized tasks (unreviewed -> stale -> reviewed) and exit code (1 if pending)."""
    root = Path(config.root)
    actual_run = resolve_run_id(root, run_id)
    tpath = tasks_json_path(root, actual_run)
    tasks_data = read_tasks_json(tpath)
    tasks_list = [dict(t) for t in tasks_data["tasks"] if isinstance(t, dict)]

    current_judged: dict[str, str] = {}
    if results is not None:
        current_judged = {r.job_id: (r.judged_status or r.status) for r in results}
    else:
        res_path = run_dir(root, actual_run) / "results.json"
        if res_path.is_file():
            with contextlib.suppress(ConfigError):
                res_data = read_results_json(res_path)
                for item in res_data.get("jobs") or ():
                    if isinstance(item, dict) and isinstance(item.get("job_id"), str):
                        current_judged[item["job_id"]] = str(
                            item.get("judged_status") or item.get("status", "")
                        )

    if _refresh_stale_flags(tasks_list, current_judged):
        with tasks_lock(root, actual_run):
            latest_data = read_tasks_json(tpath)
            tasks_list = [dict(t) for t in latest_data["tasks"] if isinstance(t, dict)]
            _refresh_stale_flags(tasks_list, current_judged)
            payload: dict[str, object] = {
                "schema": TASKS_SCHEMA,
                "run_id": latest_data.get("run_id", actual_run),
                "policy": latest_data.get("policy", {}),
                "tasks": tasks_list,
            }
            atomic_write_json(tpath, payload)

    indexed = list(enumerate(tasks_list))
    indexed.sort(
        key=lambda pair: (_task_priority_bucket(pair[1]), _task_seq(pair[1], pair[0]), pair[0])
    )
    sorted_tasks = [t for _, t in indexed]
    exit_code = 1 if any(t.get("review") is None for t in sorted_tasks) else 0
    return sorted_tasks, exit_code
