"""Read-only note/bug/todo queries across case files (M-05a, M-06, M-07, M-16)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from difflib import get_close_matches
from fnmatch import fnmatch
from typing import TYPE_CHECKING

from solouvm.collect.cases import collect_cases
from solouvm.errors import ConfigError
from solouvm.model import Bug, Case, Log, Note, Todo, Triage
from solouvm.ui import format_table

if TYPE_CHECKING:
    from solouvm.config.schema import Config

__all__ = ["format_notes_table", "next_bug_id", "query_notes"]

VALID_NOTE_TYPES: frozenset[str] = frozenset({"bug", "todo", "log", "note", "triage"})
_UNASSIGNED_TOKENS: frozenset[str] = frozenset({"unassigned", "none", "-"})
_UNRESOLVED_BUG_STATUSES: frozenset[str] = frozenset({"open", "fixed"})


def _normalize_str_seq(value: object, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, Sequence) and all(isinstance(v, str) for v in value):
        return [v for v in value if v]
    raise ConfigError(
        f"filter {name!r} must be a string or sequence of strings, got {value!r}",
        code="CFG_INVALID",
    )


def _parse_filter_date(value: object, name: str) -> date | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            raise ConfigError(
                f"filter {name!r} has invalid date {value!r} (expected YYYY-MM-DD)",
                code="CFG_INVALID",
            ) from None
    raise ConfigError(
        f"filter {name!r} must be YYYY-MM-DD date string or None, got {value!r}",
        code="CFG_INVALID",
    )


def _record_date_str(record: object) -> str | None:
    if isinstance(record, Bug):
        return record.found or (
            record.at[:10] if isinstance(record.at, str) and len(record.at) >= 10 else None
        )
    if isinstance(record, Todo):
        return record.due or (
            record.at[:10] if isinstance(record.at, str) and len(record.at) >= 10 else None
        )
    if isinstance(record, Log):
        return record.date
    if isinstance(record, Note):
        return record.date or (
            record.at[:10] if isinstance(record.at, str) and len(record.at) >= 10 else None
        )
    if isinstance(record, Triage):
        return record.at[:10] if isinstance(record.at, str) and len(record.at) >= 10 else None
    return None


def _todo_matches_status(todo: Todo, statuses: set[str], explicit_todo_type: bool) -> bool:
    if "all" in statuses:
        return True
    if todo.done:
        return "done" in statuses
    if "pending" in statuses or "-" in statuses:
        return True
    return explicit_todo_type and "open" in statuses


def next_bug_id(cases: Sequence[Case], case_name: str) -> int:
    """Return the next available bug ID for ``case_name`` (M-05a)."""
    target = next((c for c in cases if c.name == case_name), None)
    if target is None:
        names = [c.name for c in cases]
        close = get_close_matches(case_name, names, n=1)
        hint = f"did you mean {close[0]!r}?" if close else None
        raise ConfigError(f"case {case_name!r} not found", code="CASE_INVALID", hint=hint)
    used_ids = {
        rec.id for rec in target.notes if isinstance(rec, (Bug, Todo)) and rec.id is not None
    }
    candidate = max((rec.id for rec in target.notes if isinstance(rec, Bug)), default=0) + 1
    while candidate in used_ids:
        candidate += 1
    return candidate


def query_notes(
    *,
    config: Config,
    cases: list[Case] | None = None,
    filters: dict[str, object] | None = None,
    today: date | None = None,
) -> dict[str, object]:
    """Query records across cases with optional filters (M-06, M-07, M-16)."""
    case_list = collect_cases(config) if cases is None else list(cases)
    flt = dict(filters or {})
    ref_today = today or date.today()

    raw_types = _normalize_str_seq(flt.get("types"), "types")
    for item in raw_types:
        if item not in VALID_NOTE_TYPES:
            raise ConfigError(
                f"invalid note type {item!r}; allowed: {', '.join(sorted(VALID_NOTE_TYPES))}",
                code="CFG_INVALID",
            )
    types_set = set(raw_types)
    raw_statuses = _normalize_str_seq(flt.get("statuses"), "statuses")
    statuses_set = set(raw_statuses)
    owner_list = _normalize_str_seq(flt.get("owner"), "owner")
    owner_set = set(owner_list)
    since_dt = _parse_filter_date(flt.get("since"), "since")
    until_dt = _parse_filter_date(flt.get("until"), "until")
    overdue_only = bool(flt.get("overdue", False))
    case_pats = _normalize_str_seq(flt.get("case"), "case")
    tag_list = _normalize_str_seq(flt.get("tag"), "tag")
    exclude_list = _normalize_str_seq(flt.get("exclude_tag"), "exclude_tag")

    rows: list[dict[str, object]] = []
    open_bugs = 0
    fixed_bugs = 0
    open_todos = 0

    for case in case_list:
        if case_pats and not any(fnmatch(case.name, pat) for pat in case_pats):
            continue
        if tag_list and not any(t in tag_list for t in case.tags):
            continue
        if exclude_list and any(t in exclude_list for t in case.tags):
            continue

        for record in case.notes:
            if isinstance(record, Bug):
                rec_type = "bug"
            elif isinstance(record, Todo):
                rec_type = "todo"
            elif isinstance(record, Log):
                rec_type = "log"
            elif isinstance(record, Note):
                rec_type = "note"
            elif isinstance(record, Triage):
                rec_type = "triage"
            else:
                continue

            if overdue_only:
                if rec_type != "todo":
                    continue
                if types_set and "todo" not in types_set:
                    continue
            elif types_set:
                if rec_type not in types_set:
                    continue
            elif statuses_set:
                if "all" not in statuses_set and rec_type in ("log", "note", "triage"):
                    continue
            elif rec_type not in ("bug", "todo"):
                continue

            if isinstance(record, Bug):
                if statuses_set:
                    if "all" not in statuses_set and record.status not in statuses_set:
                        continue
                elif record.status not in _UNRESOLVED_BUG_STATUSES:
                    continue
            elif isinstance(record, Todo):
                if overdue_only:
                    if record.done or record.due is None:
                        continue
                    try:
                        due_dt = date.fromisoformat(record.due)
                    except ValueError:
                        continue
                    if due_dt >= ref_today:
                        continue
                if statuses_set:
                    if not _todo_matches_status(
                        record, statuses_set, explicit_todo_type=("todo" in types_set)
                    ):
                        continue
                elif record.done:
                    continue
            elif isinstance(record, Triage) and statuses_set and "all" not in statuses_set:
                rev_state = (
                    str(record.review.get("decision", "pending"))
                    if isinstance(record.review, dict)
                    else "pending"
                )
                if record.verdict not in statuses_set and rev_state not in statuses_set:
                    continue

            eff_owner: str | None
            if isinstance(record, (Bug, Todo)):
                eff_owner = record.owner if record.owner is not None else case.owner
            else:
                eff_owner = case.owner

            if owner_set:
                want_unassigned = bool(owner_set & _UNASSIGNED_TOKENS)
                named_owners = owner_set - _UNASSIGNED_TOKENS
                if eff_owner is None:
                    if not want_unassigned:
                        continue
                elif eff_owner not in named_owners:
                    continue

            rec_date_str = _record_date_str(record)
            if since_dt is not None or until_dt is not None:
                if rec_date_str is None:
                    continue
                try:
                    rec_dt = date.fromisoformat(rec_date_str)
                except ValueError:
                    continue
                if since_dt is not None and rec_dt < since_dt:
                    continue
                if until_dt is not None and rec_dt > until_dt:
                    continue

            if isinstance(record, Bug):
                if record.status == "open":
                    open_bugs += 1
                elif record.status == "fixed":
                    fixed_bugs += 1
                rows.append(
                    {
                        "id": f"{case.name}#{record.id}",
                        "record_id": record.id,
                        "case": case.name,
                        "type": "bug",
                        "status": record.status,
                        "owner": eff_owner,
                        "seed": record.seed,
                        "title": record.title,
                        "date": rec_date_str,
                        "due": None,
                        "done": None,
                        "link": record.link,
                        "signature": record.signature,
                        "author": record.author,
                        "at": record.at,
                    }
                )
            elif isinstance(record, Todo):
                if not record.done:
                    open_todos += 1
                rows.append(
                    {
                        "id": f"{case.name}#{record.id}" if record.id is not None else case.name,
                        "record_id": record.id,
                        "case": case.name,
                        "type": "todo",
                        "status": "done" if record.done else "-",
                        "owner": eff_owner,
                        "seed": None,
                        "title": record.title,
                        "date": rec_date_str,
                        "due": record.due,
                        "done": record.done,
                        "link": None,
                        "signature": None,
                        "author": record.author,
                        "at": record.at,
                    }
                )
            elif isinstance(record, Log):
                rows.append(
                    {
                        "id": case.name,
                        "record_id": None,
                        "case": case.name,
                        "type": "log",
                        "status": "-",
                        "owner": eff_owner,
                        "seed": None,
                        "title": record.text,
                        "date": record.date,
                        "due": None,
                        "done": None,
                        "link": None,
                        "signature": None,
                        "author": record.author,
                        "at": record.at,
                    }
                )
            elif isinstance(record, Note):
                rows.append(
                    {
                        "id": case.name,
                        "record_id": None,
                        "case": case.name,
                        "type": "note",
                        "status": "-",
                        "owner": eff_owner,
                        "seed": None,
                        "title": record.text,
                        "date": rec_date_str,
                        "due": None,
                        "done": None,
                        "link": None,
                        "signature": None,
                        "author": record.author,
                        "at": record.at,
                    }
                )
            elif isinstance(record, Triage):
                rev = (
                    str(record.review.get("decision", "pending"))
                    if isinstance(record.review, dict)
                    else "pending"
                )
                rows.append(
                    {
                        "id": f"{case.name}@{record.task}",
                        "record_id": None,
                        "case": case.name,
                        "type": "triage",
                        "status": f"{record.verdict} ({rev})",
                        "owner": eff_owner,
                        "seed": None,
                        "title": record.summary,
                        "date": rec_date_str,
                        "due": None,
                        "done": None,
                        "link": record.doc,
                        "signature": None,
                        "author": record.author,
                        "at": record.at,
                    }
                )

    return {
        "open_bugs": open_bugs,
        "fixed_bugs": fixed_bugs,
        "open_todos": open_todos,
        "total": len(rows),
        "rows": rows,
    }


def format_notes_table(result: dict[str, object]) -> str:
    """Format ``query_notes`` output as an ASCII table with a summary footer (cli.md §6)."""
    rows = result.get("rows") or []
    table_rows: list[list[str]] = []
    for row in rows:  # type: ignore[attr-defined]
        if not isinstance(row, dict):
            continue
        title = str(row.get("title", "")).splitlines()[0] if row.get("title") else ""
        if row.get("type") == "todo" and row.get("due"):
            title = f"{title} (due {row['due']})"
        elif row.get("type") in ("log", "note") and row.get("date"):
            title = f"[{row['date']}] {title}"
        seed = str(row["seed"]) if row.get("seed") is not None else "-"
        owner = str(row["owner"]) if row.get("owner") else "-"
        table_rows.append(
            [
                str(row.get("id", "")),
                str(row.get("type", "")),
                str(row.get("status", "-")),
                owner,
                seed,
                title,
            ]
        )

    open_bugs = int(result.get("open_bugs", 0) or 0)  # type: ignore[call-overload]
    fixed_bugs = int(result.get("fixed_bugs", 0) or 0)  # type: ignore[call-overload]
    open_todos = int(result.get("open_todos", 0) or 0)  # type: ignore[call-overload]
    bug_word = "open bug" if open_bugs == 1 else "open bugs"
    todo_word = "todo" if open_todos == 1 else "todos"
    summary = (
        f"{open_bugs} {bug_word}, "
        f"{fixed_bugs} fixed awaiting verification, "
        f"{open_todos} {todo_word}"
    )

    if not table_rows:
        return summary
    rendered = format_table(["ID", "TYPE", "STATUS", "OWNER", "SEED", "TITLE"], table_rows)
    return f"{rendered}\n\n{summary}"
