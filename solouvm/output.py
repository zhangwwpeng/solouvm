"""Structured JSON / JSONL output envelopes, truncation, and error handling (formats.md §9)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TypeVar

from solouvm import ui
from solouvm.errors import SolouvmError, format_error

SCHEMA_VERSION = 1

T = TypeVar("T")

__all__ = [
    "SCHEMA_VERSION",
    "emit_event",
    "emit_json",
    "emit_next",
    "fail",
    "make_error_envelope",
    "make_json_envelope",
    "truncate_items",
]


def truncate_items(
    items: Sequence[T],
    limit: int,
    *,
    more: str,
) -> tuple[list[T], dict[str, object] | None]:
    """Slice ``items`` to ``limit`` (``0`` disables truncation) and build ``truncated`` metadata."""
    if limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")
    if limit > 0 and len(items) > limit:
        return list(items[:limit]), {"omitted": len(items) - limit, "more": more}
    return list(items), None


def make_json_envelope(
    command: str,
    data: object,
    *,
    truncated: dict[str, object] | None = None,
    next_: Sequence[str] | None = None,
) -> dict[str, object]:
    """Build the standard success JSON envelope with fixed key order (formats.md §9)."""
    return {
        "schema": SCHEMA_VERSION,
        "command": command,
        "ok": True,
        "data": data,
        "truncated": truncated,
        "next": [] if next_ is None else list(next_),
    }


def make_error_envelope(command: str, err: SolouvmError) -> dict[str, object]:
    """Build the standard failure JSON envelope with fixed key order (formats.md §9)."""
    return {
        "schema": SCHEMA_VERSION,
        "command": command,
        "ok": False,
        "error": err.to_dict(),
    }


def emit_json(
    command: str,
    data: object,
    *,
    truncated: dict[str, object] | None = None,
    next_: Sequence[str] | None = None,
) -> None:
    """Write a single formatted JSON envelope (UTF-8, 2-space indent, newline) to stdout."""
    payload = make_json_envelope(command, data, truncated=truncated, next_=next_)
    print(json.dumps(payload, indent=2, ensure_ascii=False), flush=True)


def emit_event(event: str, **fields: object) -> None:
    """Write a single JSON Lines streaming event object to stdout and flush immediately."""
    if "schema" in fields:
        raise ValueError("event fields cannot override reserved key 'schema'")
    payload: dict[str, object] = {"schema": SCHEMA_VERSION, "event": event, **fields}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), flush=True)


def emit_next(commands: Sequence[str]) -> None:
    """Print suggested follow-up commands in human mode (suppressed in ``--json`` and ``-q``)."""
    if ui.is_json_mode() or ui.is_quiet():
        return
    for cmd in commands:
        ui.info(f"next: {cmd}")


def fail(err: SolouvmError, *, command: str = "") -> int:
    """Report ``err`` in JSON or human format and return its mapped exit code."""
    if ui.is_json_mode():
        payload = make_error_envelope(command, err)
        print(json.dumps(payload, indent=2, ensure_ascii=False), flush=True)
    else:
        ui.error(format_error(err))
    return err.exit_code
