"""Triage conclusion validation, debug doc output, and case notes (AI-07, AI-08, AI-16, AI-19)."""

from __future__ import annotations

import contextlib
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.collect.cases import collect_cases_with_sources
from solouvm.errors import ConfigError, RecordError
from solouvm.history import resolve_run_id, run_dir
from solouvm.model import (
    _CONFIDENCES,
    _EVIDENCE,
    _SUMMARY_MAX,
    _VERDICTS,
    Bug,
    Triage,
)
from solouvm.notes.commands import parse_record_ref, read_stdin_text
from solouvm.records import append_record, record_lock, replace_record
from solouvm.report.json import read_results_json
from solouvm.roles import check_role
from solouvm.triage.plan import (
    TASKS_SCHEMA,
    atomic_write_json,
    atomic_write_text_safe,
    read_tasks_json,
    tasks_json_path,
    tasks_lock,
)

if TYPE_CHECKING:
    from solouvm.config.schema import Config

__all__ = ["ALLOWED_VERDICTS", "REQUIRED_DOC_HEADINGS", "write_result"]

REQUIRED_DOC_HEADINGS: tuple[str, ...] = (
    "## 现象",
    "## 排查过程",
    "## 证据",
    "## 结论",
    "## 建议",
)

ALLOWED_VERDICTS: dict[str, frozenset[str]] = {
    "pass": frozenset({"real_pass", "false_pass"}),
    "fail": frozenset(_VERDICTS) - {"real_pass"},
}

_FENCE_RE = re.compile(r"^\s*```")


def _validate_doc_text(doc_text: str) -> str:
    if not isinstance(doc_text, str) or not doc_text.strip():
        raise ConfigError("debug doc must be a non-empty markdown string", code="TRIAGE_INVALID")

    sections: dict[str, list[str]] = {}
    current: str | None = None
    in_fence = False
    for line in doc_text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            if current is not None:
                sections[current].append(line)
            continue
        stripped = line.strip()
        if not in_fence and stripped.startswith("## "):
            current = stripped
            sections.setdefault(current, [])
            continue
        if current is not None:
            sections[current].append(line)

    for heading in REQUIRED_DOC_HEADINGS:
        body_lines = sections.get(heading)
        if body_lines is None or not "\n".join(body_lines).strip():
            raise ConfigError(
                f"debug doc is missing or has empty required section {heading!r}",
                code="TRIAGE_INVALID",
                hint="required sections: " + ", ".join(REQUIRED_DOC_HEADINGS),
            )
    return doc_text.strip()


def _validate_evidence(evidence: Sequence[str]) -> list[str]:
    if isinstance(evidence, str) or not isinstance(evidence, Sequence) or not evidence:
        raise ConfigError(
            "at least one evidence item is required",
            code="TRIAGE_INVALID",
            hint="expected 'log:<line>', '<file>:<line>', or 'wave:<time> <signal>'",
        )
    out: list[str] = []
    for item in evidence:
        if not isinstance(item, str) or not any(p.fullmatch(item) for p in _EVIDENCE):
            raise ConfigError(
                f"invalid evidence item {item!r} "
                "(expected 'log:<line>', '<file>:<line>', or 'wave:<time> <signal>')",
                code="TRIAGE_INVALID",
            )
        out.append(item)
    return out


def _debug_doc_paths(
    config: Config,
    run_id: str,
    task_id: str,
) -> tuple[Path, str]:
    root = Path(config.root)
    _, _, seq = task_id.rpartition("/")
    if not seq or "/" in seq or "\\" in seq or seq in (".", ".."):
        seq = "1"
    debug_base = Path(config.triage.debug_dir)
    debug_dir = debug_base if debug_base.is_absolute() else (root / debug_base)
    abs_path = debug_dir / run_id / f"{seq}.md"
    try:
        rel_posix = abs_path.relative_to(root).as_posix()
    except ValueError:
        try:
            rel_posix = abs_path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel_posix = abs_path.as_posix()
    return abs_path, rel_posix


def _render_debug_doc(
    *,
    task_id: str,
    run_id: str,
    cases: Sequence[str],
    verdict: str,
    confidence: str,
    summary: str,
    evidence: Sequence[str],
    bugs: Sequence[str],
    author: str,
    at: str,
    doc_body: str,
) -> str:
    bugs_text = ", ".join(bugs) if bugs else "none"
    header = "\n".join(
        [
            f"# Triage: {task_id}",
            "",
            f"- Task: `{task_id}`",
            f"- Run: `{run_id}`",
            f"- Cases: {', '.join(cases)}",
            f"- Verdict: `{verdict}` (confidence: `{confidence}`)",
            f"- Summary: {summary}",
            f"- Bugs: {bugs_text}",
            f"- Evidence: {', '.join(evidence)}",
            f"- Author: `{author}`",
            f"- At: `{at}`",
            "",
        ]
    )
    return f"{header}\n{doc_body}\n"


def _current_judged_map(
    root: Path, run_id: str, task: dict[str, object]
) -> tuple[dict[str, str], list[dict[str, object]]]:
    res_by_id: dict[str, str] = {}
    res_path = run_dir(root, run_id) / "results.json"
    if res_path.is_file():
        with contextlib.suppress(ConfigError):
            res_data = read_results_json(res_path)
            for item in res_data.get("jobs") or ():
                if isinstance(item, dict) and isinstance(item.get("job_id"), str):
                    res_by_id[item["job_id"]] = str(
                        item.get("judged_status") or item.get("status", "")
                    )

    judged_map: dict[str, str] = {}
    updated_jobs: list[dict[str, object]] = []
    for jentry in task.get("jobs") or ():
        if not isinstance(jentry, dict):
            continue
        jid = str(jentry.get("job_id", ""))
        if not jid:
            continue
        jstatus = res_by_id.get(jid, str(jentry.get("judged_status", "")))
        judged_map[jid] = jstatus
        updated_jobs.append({"job_id": jid, "judged_status": jstatus})
    return judged_map, updated_jobs


def write_result(
    *,
    config: Config,
    run_id: str,
    task_id: str,
    verdict: str,
    confidence: str,
    summary: str,
    evidence: Sequence[str],
    bugs: Sequence[str] | None = None,
    doc_text: str | None = None,
    author: str | None = None,
    role: str = "triage",
) -> dict[str, object]:
    """Validate and record a triage verdict, debug doc, and per-case ``Triage`` notes."""
    check_role(role, "triage write")

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
            f"task {task_id!r} not found in run {actual_run!r}", code="TASK_NOT_FOUND"
        )
    if task.get("review") is not None:
        raise ConfigError(
            "task already reviewed; ask main to reject before rewriting",
            code="TRIAGE_INVALID",
        )

    kind = str(task.get("kind", ""))
    if verdict not in _VERDICTS:
        raise ConfigError(
            f"invalid verdict {verdict!r}; allowed: {', '.join(_VERDICTS)}",
            code="TRIAGE_INVALID",
        )
    allowed = ALLOWED_VERDICTS.get(kind, frozenset())
    if verdict not in allowed:
        raise ConfigError(
            f"verdict {verdict!r} is not compatible with task kind {kind!r}; "
            f"allowed: {', '.join(sorted(allowed))}",
            code="TRIAGE_INVALID",
        )
    if confidence not in _CONFIDENCES:
        raise ConfigError(
            f"invalid confidence {confidence!r}; allowed: {', '.join(_CONFIDENCES)}",
            code="TRIAGE_INVALID",
        )
    if not isinstance(summary, str) or not summary.strip():
        raise ConfigError("triage summary must be a non-empty string", code="TRIAGE_INVALID")
    if len(summary) > _SUMMARY_MAX:
        raise ConfigError(
            f"triage summary is {len(summary)} characters (max {_SUMMARY_MAX})",
            code="TRIAGE_INVALID",
        )

    evidence_list = _validate_evidence(evidence)

    if doc_text is None:
        raise ConfigError("debug doc is required (--doc)", code="TRIAGE_INVALID")
    raw_doc = read_stdin_text(doc_text, field="doc")
    clean_doc = _validate_doc_text(raw_doc)

    raw_bugs = list(bugs or ())
    bugs_list = list(dict.fromkeys(raw_bugs))
    parsed_bugs = [parse_record_ref(ref) for ref in bugs_list]

    task_cases = [str(c) for c in (task.get("cases") or []) if isinstance(c, str) and c]
    if not task_cases:
        raise ConfigError(f"task {task_id!r} has no cases", code="TRIAGE_INVALID")

    sources = collect_cases_with_sources(config)
    case_map = {cs.case.name: cs.case for cs in sources}
    for cname in task_cases:
        if cname not in case_map:
            raise RecordError(f"case {cname!r} not found", code="RECORD_INVALID")
    for ref, (b_case, b_id) in zip(bugs_list, parsed_bugs, strict=True):
        target_case = case_map.get(b_case)
        if target_case is None or not any(
            isinstance(rec, Bug) and rec.id == b_id for rec in target_case.notes
        ):
            raise RecordError(f"bug {ref!r} not found", code="RECORD_INVALID")

    actor = author.strip() if isinstance(author, str) and author.strip() else role
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    doc_abs, doc_rel = _debug_doc_paths(config, actual_run, task_id)
    judged_map, updated_jobs = _current_judged_map(root, actual_run, task)

    rendered_doc = _render_debug_doc(
        task_id=task_id,
        run_id=actual_run,
        cases=task_cases,
        verdict=verdict,
        confidence=confidence,
        summary=summary,
        evidence=evidence_list,
        bugs=bugs_list,
        author=actor,
        at=now_iso,
        doc_body=clean_doc,
    )
    atomic_write_text_safe(doc_abs, rendered_doc)

    triage_record = Triage(
        task=task_id,
        verdict=verdict,
        confidence=confidence,
        summary=summary,
        evidence=evidence_list,
        judged_status=judged_map,
        doc=doc_rel,
        bugs=bugs_list,
        review=None,
        author=actor,
        at=now_iso,
    )

    for case_name in task_cases:
        with record_lock(root):
            fresh_sources = collect_cases_with_sources(config)
            target_cs = next((cs for cs in fresh_sources if cs.case.name == case_name), None)
            if target_cs is None:
                raise RecordError(f"case {case_name!r} not found", code="RECORD_INVALID")
            existing = next(
                (r for r in target_cs.case.notes if isinstance(r, Triage) and r.task == task_id),
                None,
            )
            if existing is not None:
                if existing.review is not None:
                    raise ConfigError(
                        "task already reviewed; ask main to reject before rewriting",
                        code="TRIAGE_INVALID",
                    )
                replace_record(target_cs.file, case_name, triage_record)
            else:
                append_record(target_cs.file, case_name, triage_record)

    result_obj: dict[str, object] = {
        "verdict": verdict,
        "confidence": confidence,
        "summary": summary,
        "evidence": evidence_list,
        "bugs": bugs_list,
        "doc": doc_rel,
        "author": actor,
        "at": now_iso,
    }

    with tasks_lock(root, actual_run):
        latest_data = read_tasks_json(tpath)
        tasks_list = [dict(t) for t in latest_data["tasks"] if isinstance(t, dict)]
        target_task = next((t for t in tasks_list if t.get("id") == task_id), None)
        if target_task is None:
            raise ConfigError(
                f"task {task_id!r} not found in run {actual_run!r}", code="TASK_NOT_FOUND"
            )
        if target_task.get("review") is not None:
            raise ConfigError(
                "task already reviewed; ask main to reject before rewriting",
                code="TRIAGE_INVALID",
            )
        target_task["jobs"] = updated_jobs
        target_task["stale"] = False
        target_task["result"] = result_obj
        payload: dict[str, object] = {
            "schema": TASKS_SCHEMA,
            "run_id": latest_data.get("run_id", actual_run),
            "policy": latest_data.get("policy", {}),
            "tasks": tasks_list,
        }
        atomic_write_json(tpath, payload)
        return target_task
