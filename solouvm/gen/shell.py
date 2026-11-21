"""POSIX shell, GNU Make escaping and relative path helpers (architecture.md §5.1, N-05, N-12)."""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from solouvm.errors import ConfigError

__all__ = ["check_no_newlines", "make_escape", "relpath", "sh_quote"]

_BRACED_PREFIX_RE = re.compile(r"^\{[A-Za-z_]\w*\}(?:$|/)")


def check_no_newlines(value: str, *, field: str = "value") -> str:
    """Reject ``\\n`` or ``\\r`` in generated shell/Makefile/filelist values."""
    if "\n" in value or "\r" in value:
        raise ConfigError(
            f"{field} cannot contain newlines (got {value!r})",
            code="CFG_INVALID",
        )
    return value


def sh_quote(value: str, *, field: str = "value") -> str:
    """Quote ``value`` as a single POSIX shell word; reject newlines."""
    check_no_newlines(value, field=field)
    return shlex.quote(value)


def make_escape(value: str, *, field: str = "value") -> str:
    """Shell-quote ``value`` and double every ``$`` for GNU Make recipes."""
    return sh_quote(value, field=field).replace("$", "$$")


def relpath(target: str | Path, start: str | Path) -> str:
    """Return a POSIX path to ``target`` relative to ``start``, keeping ``$``/``{...}`` prefixes."""
    if isinstance(target, str):
        check_no_newlines(target, field="path")
        if target.startswith("$") or _BRACED_PREFIX_RE.match(target):
            return target
    target_str = check_no_newlines(os.fspath(target), field="path")
    start_str = check_no_newlines(os.fspath(start), field="path")
    try:
        rel = os.path.relpath(target_str, start_str)
    except ValueError as exc:
        raise ConfigError(
            f"cannot compute relative path from {start_str!r} to {target_str!r}: {exc}",
            code="CFG_INVALID",
        ) from None
    rel = rel.replace(os.sep, "/")
    if os.altsep:
        rel = rel.replace(os.altsep, "/")
    return rel
