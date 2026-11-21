"""Record write commands: ``bug add/update``, ``note add``, ``todo add/done`` (M-14, AI-19)."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime
from difflib import get_close_matches
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.collect.cases import CaseSource, collect_cases_with_sources
from solouvm.errors import ConfigError, RecordError, RoleError
from solouvm.history import resolve_run_id, run_dir
from solouvm.model import Bug, Case, Log, Note, Todo, Triage, validate_records
from solouvm.records import append_record, record_lock, replace_record
from solouvm.records import write as records_write
from solouvm.report.json import read_results_json
from solouvm.roles import check_role

if TYPE_CHECKING:
    from solouvm.config.schema import Config

__all__ = [
    "bug_add",
    "bug_update",
    "note_add",
    "parse_record_ref",
    "read_stdin_text",
    "todo_add",
    "todo_done",
]


def read_stdin_text(value: str, *, field: str = "body") -> str:
    """Return ``value`` directly, or read from stdin when ``value == '-'`` (AI-19)."""
    if value != "-":
        return value
    isatty = getattr(sys.stdin, "isatty", None)
    if callable(isatty) and isatty():
        raise ConfigError(
            f"--{field} - cannot read from a TTY; pipe or use heredoc",
            code="CFG_INVALID",
        )
    return sys.stdin.read()


def parse_record_ref(ref: str) -> tuple[str, int]:
    """Parse ``CASE#ID`` into ``(case_name, int_id)`` or raise ``RECORD_INVALID``."""
    if not isinstance(ref, str):
        raise RecordError(
            f"invalid record reference {ref!r} (expected 'CASE#ID')", code="RECORD_INVALID"
        )
    case_name, sep, ident = ref.partition("#")
    if not sep or not case_name or not ident.isascii() or not ident.isdigit():
        raise RecordError(
            f"invalid record reference {ref!r} (expected 'CASE#ID')",
            code="RECORD_INVALID",
        )
    rec_id = int(ident)
    if rec_id <= 0:
        raise RecordError(
            f"invalid record reference {ref!r}: ID must be a positive integer",
            code="RECORD_INVALID",
        )
    return case_name, rec_id


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _resolve_author(author: str | None, role: str) -> str:
    if isinstance(author, str) and author.strip():
        return author.strip()
    return role


def _validate_iso_date(value: str, field_name: str) -> None:
    try:
        date.fromisoformat(value)
    except ValueError:
        raise RecordError(
            f"field {field_name!r} has invalid date {value!r} (expected YYYY-MM-DD)",
            code="RECORD_INVALID",
        ) from None


def _find_case_source(config: Config, case_name: str) -> tuple[CaseSource, list[CaseSource]]:
    sources = collect_cases_with_sources(config)
    for cs in sources:
        if cs.case.name == case_name:
            return cs, sources
    names = [cs.case.name for cs in sources]
    close = get_close_matches(case_name, names, n=1)
    hint = f"did you mean {close[0]!r}?" if close else None
    raise RecordError(f"case {case_name!r} not found", code="RECORD_INVALID", hint=hint)


def _validate_candidate_notes(case_name: str, notes: list[object]) -> None:
    probe = Case(name=case_name)
    probe.notes = list(notes)
    try:
        validate_records(probe)
    except ConfigError as exc:
        raise RecordError(exc.message, code="RECORD_INVALID", hint=exc.hint) from None


def _is_reviewed_in_triage(sources: Sequence[CaseSource], ref: str) -> bool:
    for cs in sources:
        for rec in cs.case.notes:
            if isinstance(rec, Triage) and rec.review is not None and ref in rec.bugs:
                return True
    return False


def _job_info(
    root: Path,
    run_id: str | None,
    job_id: str,
) -> tuple[str, int | None, str | None]:
    actual_run = resolve_run_id(root, run_id)
    results_data = read_results_json(run_dir(root, actual_run) / "results.json")
    jobs = results_data.get("jobs")
    job_entry = (
        next(
            (j for j in jobs if isinstance(j, dict) and j.get("job_id") == job_id),
            None,
        )
        if isinstance(jobs, list)
        else None
    )
    if job_entry is None:
        raise ConfigError(
            f"job {job_id!r} not found in run {actual_run!r}",
            code="JOB_NOT_FOUND",
        )
    raw_seed = job_entry.get("seed")
    seed_val = raw_seed if isinstance(raw_seed, int) and not isinstance(raw_seed, bool) else None
    raw_sig = job_entry.get("signature")
    sig_val: str | None = None
    if isinstance(raw_sig, dict):
        sid = raw_sig.get("id")
        stext = raw_sig.get("text")
        if isinstance(sid, str) and sid:
            sig_val = sid
        elif isinstance(stext, str) and stext:
            sig_val = stext
    elif isinstance(raw_sig, str) and raw_sig:
        sig_val = raw_sig
    return actual_run, seed_val, sig_val


def _replace_legacy_todo(path: Path, case_name: str, todo_index: int, updated: Todo) -> None:
    """Replace the ``todo_index``-th ``Todo`` AST element (when ``id=None``) under lock."""
    path = Path(path)

    def edit(source: str) -> str:
        nl = records_write._newline(source)
        tree = records_write._parse(source, path)
        call = records_write.find_case_node(tree, case_name, path)
        lst = records_write._notes_keyword(call, case_name, path)
        todo_elts = [
            elt
            for elt in (lst.elts if lst is not None else [])
            if records_write._call_type(elt) == "Todo"
        ]
        if todo_index < 0 or todo_index >= len(todo_elts):
            raise RecordError(
                f"case {case_name!r} has no legacy Todo at index {todo_index + 1}",
                code="RECORD_INVALID",
                file=path,
            )
        match = todo_elts[todo_index]
        start = records_write._start(source, match)
        end = records_write._end(source, match)
        if match.lineno != match.end_lineno:
            multiline: bool | None = True
        elif not records_write._starts_line(source, start):
            multiline = False
        else:
            multiline = None
        rendered = records_write._render_record(
            updated,
            records_write._column(source, start),
            multiline=multiline,
        )
        rendered = records_write._indent_tail(
            rendered, records_write._leading_ws(source, start), nl
        )
        new_src = source[:start] + rendered + source[end:]
        return records_write._ensure_imports(new_src, records_write._type_names(updated), path)

    def check(target_path: Path) -> None:
        reloaded = records_write._reload(target_path, case_name)
        todos = [r for r in reloaded.notes if isinstance(r, Todo)]
        if todo_index >= len(todos) or todos[todo_index] != updated:
            raise RecordError(f"todo not updated in case {case_name!r} after the write")

    records_write._rewrite(path, edit, check)


def bug_add(
    *,
    config: Config,
    case_name: str,
    title: str,
    seed: int | None = None,
    run_id: str | None = None,
    job_id: str | None = None,
    owner: str | None = None,
    link: str | None = None,
    body: str = "",
    author: str | None = None,
    bug_id: int | None = None,
    role: str = "main",
    today: date | None = None,
) -> Bug:
    """Create and append a new ``Bug`` record to ``case_name`` under record lock (M-14, M-25)."""
    check_role(role, "bug add")
    if not isinstance(title, str) or not title.strip():
        raise RecordError("bug title must be a non-empty string", code="RECORD_INVALID")
    if bug_id is not None and (
        isinstance(bug_id, bool) or not isinstance(bug_id, int) or bug_id <= 0
    ):
        raise RecordError(f"bug id must be a positive int, got {bug_id!r}", code="RECORD_INVALID")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or seed < 0):
        raise RecordError(f"seed must be a non-negative int, got {seed!r}", code="RECORD_INVALID")

    body_text = read_stdin_text(body, field="body")
    root = Path(config.root)

    resolved_seed = seed
    resolved_sig: str | None = None
    resolved_run: str | None = None
    resolved_job: str | None = job_id
    if job_id is not None:
        actual_run, job_seed, job_sig = _job_info(root, run_id, job_id)
        resolved_run = actual_run
        if resolved_seed is None:
            resolved_seed = job_seed
        resolved_sig = job_sig
    elif run_id is not None:
        resolved_run = resolve_run_id(root, run_id)

    found_str = (today or date.today()).isoformat()
    actor = _resolve_author(author, role)
    at_str = _now_iso()

    with record_lock(root):
        target_cs, _ = _find_case_source(config, case_name)
        used_ids = {
            rec.id
            for rec in target_cs.case.notes
            if isinstance(rec, (Bug, Todo)) and rec.id is not None
        }
        if bug_id is None:
            allocated_id = (
                max((rec.id for rec in target_cs.case.notes if isinstance(rec, Bug)), default=0) + 1
            )
            while allocated_id in used_ids:
                allocated_id += 1
        else:
            if bug_id in used_ids:
                raise RecordError(
                    f"case {case_name!r} already has a record with id={bug_id}",
                    code="RECORD_INVALID",
                )
            allocated_id = bug_id

        record = Bug(
            id=allocated_id,
            title=title,
            status="open",
            found=found_str,
            seed=resolved_seed,
            signature=resolved_sig,
            run=resolved_run,
            job=resolved_job,
            owner=owner,
            link=link,
            body=body_text,
            author=actor,
            at=at_str,
        )
        _validate_candidate_notes(case_name, [*target_cs.case.notes, record])
        append_record(target_cs.file, case_name, record)
    return record


def bug_update(
    *,
    config: Config,
    ref: str,
    status: str | None = None,
    owner: str | None = None,
    link: str | None = None,
    append_body: str | None = None,
    author: str | None = None,
    role: str = "main",
    today: date | None = None,
) -> Bug:
    """Update an existing ``Bug`` referenced as ``CASE#ID`` (M-14, AI-08)."""
    check_role(role, "bug update")
    case_name, bug_id = parse_record_ref(ref)
    extra_body = (
        read_stdin_text(append_body, field="append-body") if append_body is not None else None
    )
    root = Path(config.root)
    today_str = (today or date.today()).isoformat()
    actor = _resolve_author(author, role)

    with record_lock(root):
        target_cs, all_sources = _find_case_source(config, case_name)
        bug = next(
            (rec for rec in target_cs.case.notes if isinstance(rec, Bug) and rec.id == bug_id),
            None,
        )
        if bug is None:
            raise RecordError(f"bug {ref!r} not found", code="RECORD_INVALID")

        if role == "triage":
            if bug.author != actor:
                raise RoleError(
                    f"triage role cannot modify bug {ref} authored by {bug.author!r}",
                    code="ROLE_DENIED",
                )
            if _is_reviewed_in_triage(all_sources, ref):
                raise RoleError(
                    f"bug {ref} has been reviewed in triage; ask the main role",
                    code="ROLE_DENIED",
                )

        new_status = status if status is not None else bug.status
        new_found = bug.found or today_str
        new_fixed = bug.fixed
        new_verified = bug.verified
        if status == "fixed":
            new_fixed = new_fixed or today_str
        elif status == "verified":
            new_fixed = new_fixed or today_str
            new_verified = new_verified or today_str

        new_owner = owner if owner is not None else bug.owner
        new_link = link if link is not None else bug.link
        new_body = bug.body
        if extra_body is not None and extra_body:
            new_body = f"{bug.body.rstrip()}\n{extra_body}" if bug.body.strip() else extra_body

        updated = replace(
            bug,
            status=new_status,
            found=new_found,
            fixed=new_fixed,
            verified=new_verified,
            owner=new_owner,
            link=new_link,
            body=new_body,
        )
        candidate_notes = [updated if rec is bug else rec for rec in target_cs.case.notes]
        _validate_candidate_notes(case_name, candidate_notes)
        replace_record(target_cs.file, case_name, updated)
    return updated


def note_add(
    *,
    config: Config,
    case_name: str,
    text: str,
    kind: str = "log",
    date_str: str | None = None,
    author: str | None = None,
    role: str = "main",
    today: date | None = None,
) -> Log | Note:
    """Append a ``Log`` (default) or ``Note`` record to ``case_name`` (M-14, M-25)."""
    check_role(role, "note add")
    if kind not in ("log", "note"):
        raise RecordError(
            f"invalid note kind {kind!r}; expected 'log' or 'note'",
            code="RECORD_INVALID",
        )
    text_val = read_stdin_text(text, field="text")
    if not isinstance(text_val, str) or not text_val.strip():
        raise RecordError("note text must be a non-empty string", code="RECORD_INVALID")

    eff_date = date_str if date_str is not None else (today or date.today()).isoformat()
    _validate_iso_date(eff_date, "date")
    actor = _resolve_author(author, role)
    at_str = _now_iso()

    record: Log | Note
    if kind == "log":
        record = Log(date=eff_date, text=text_val, author=actor, at=at_str)
    else:
        record = Note(text=text_val, date=eff_date, author=actor, at=at_str)

    root = Path(config.root)
    with record_lock(root):
        target_cs, _ = _find_case_source(config, case_name)
        _validate_candidate_notes(case_name, [*target_cs.case.notes, record])
        append_record(target_cs.file, case_name, record)
    return record


def todo_add(
    *,
    config: Config,
    case_name: str,
    title: str,
    owner: str | None = None,
    due: str | None = None,
    author: str | None = None,
    role: str = "main",
) -> Todo:
    """Append a ``Todo`` record with a monotonically allocated ID to ``case_name`` (M-14, M-19)."""
    check_role(role, "todo add")
    title_val = read_stdin_text(title, field="title")
    if not isinstance(title_val, str) or not title_val.strip():
        raise RecordError("todo title must be a non-empty string", code="RECORD_INVALID")
    if due is not None:
        _validate_iso_date(due, "due")
    actor = _resolve_author(author, role)
    at_str = _now_iso()

    root = Path(config.root)
    with record_lock(root):
        target_cs, _ = _find_case_source(config, case_name)
        used_ids = {
            rec.id
            for rec in target_cs.case.notes
            if isinstance(rec, (Bug, Todo)) and rec.id is not None
        }
        todo_id = (
            max(
                (
                    rec.id
                    for rec in target_cs.case.notes
                    if isinstance(rec, Todo) and rec.id is not None
                ),
                default=0,
            )
            + 1
        )
        while todo_id in used_ids:
            todo_id += 1

        record = Todo(
            title=title_val,
            owner=owner,
            due=due,
            done=False,
            id=todo_id,
            author=actor,
            at=at_str,
        )
        _validate_candidate_notes(case_name, [*target_cs.case.notes, record])
        append_record(target_cs.file, case_name, record)
    return record


def todo_done(
    *,
    config: Config,
    ref: str,
    role: str = "main",
) -> Todo:
    """Mark a ``Todo`` referenced by ``CASE#ID`` as ``done=True`` (main role only)."""
    check_role(role, "todo done")
    case_name, target_id = parse_record_ref(ref)
    root = Path(config.root)

    with record_lock(root):
        target_cs, _ = _find_case_source(config, case_name)
        todos = [rec for rec in target_cs.case.notes if isinstance(rec, Todo)]
        by_id = next((t for t in todos if t.id == target_id), None)
        if by_id is not None:
            updated = replace(by_id, done=True)
            _validate_candidate_notes(
                case_name,
                [updated if rec is by_id else rec for rec in target_cs.case.notes],
            )
            replace_record(target_cs.file, case_name, updated)
            return updated

        if 1 <= target_id <= len(todos) and todos[target_id - 1].id is None:
            legacy = todos[target_id - 1]
            updated = replace(legacy, done=True)
            _validate_candidate_notes(
                case_name,
                [updated if rec is legacy else rec for rec in target_cs.case.notes],
            )
            _replace_legacy_todo(target_cs.file, case_name, target_id - 1, updated)
            return updated

        raise RecordError(f"todo {ref!r} not found", code="RECORD_INVALID")
