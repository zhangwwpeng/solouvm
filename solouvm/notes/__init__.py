"""Case notes, bug/todo management, markdown documentation export, and progress overview."""

from solouvm.notes.commands import (
    bug_add,
    bug_update,
    note_add,
    parse_record_ref,
    read_stdin_text,
    todo_add,
    todo_done,
)
from solouvm.notes.export import GENERATED_MARKER, export_docs
from solouvm.notes.progress import compute_progress, format_progress
from solouvm.notes.query import format_notes_table, next_bug_id, query_notes

__all__ = [
    "GENERATED_MARKER",
    "bug_add",
    "bug_update",
    "compute_progress",
    "export_docs",
    "format_notes_table",
    "format_progress",
    "next_bug_id",
    "note_add",
    "parse_record_ref",
    "query_notes",
    "read_stdin_text",
    "todo_add",
    "todo_done",
]
