"""Terminal UI and output formatting; sole entry point for vendored ``rich`` (N-02, N-22, Z-07)."""

from __future__ import annotations

import shlex
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_VENDOR = Path(__file__).parent / "_vendor"


def _load_vendor() -> tuple[Any, Any]:
    """Load ``Console`` and ``Table`` from ``_vendor/rich`` if present."""
    if not (_VENDOR / "rich").is_dir():
        return None, None
    vendor_str = str(_VENDOR)
    if vendor_str not in sys.path:
        sys.path.insert(0, vendor_str)
    try:
        from rich.console import Console as rich_console
        from rich.table import Table as rich_table
    except ImportError:
        return None, None
    return rich_console, rich_table


Console, Table = _load_vendor()


@dataclass
class _UIState:
    json_mode: bool = False
    quiet: bool = False
    verbose: int = 0
    log_file: Path | None = None
    limit: int = 50
    console: Any = None


_state = _UIState()

__all__ = [
    "Console",
    "Table",
    "command_log",
    "error",
    "format_table",
    "get_limit",
    "get_log_file",
    "info",
    "is_json_mode",
    "is_quiet",
    "is_tty",
    "reload_vendor",
    "reset",
    "set_log_file",
    "setup",
    "status_line",
    "summary",
    "table",
    "use_rich",
    "verbosity",
    "warning",
]


def reload_vendor() -> None:
    """Reload vendored ``rich`` classes or fall back to ``None`` when absent."""
    console_cls, table_cls = _load_vendor()
    module_ns = globals()
    module_ns["Console"] = console_cls
    module_ns["Table"] = table_cls
    _state.console = None


def setup(
    *,
    json_mode: bool = False,
    quiet: bool = False,
    verbose: int = 0,
    log_file: str | Path | None = None,
    limit: int = 50,
) -> None:
    """Configure global UI state for the current command invocation."""
    if limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")
    if verbose < 0:
        raise ValueError(f"verbose must be >= 0, got {verbose}")
    _state.json_mode = bool(json_mode)
    _state.quiet = bool(quiet)
    _state.verbose = int(verbose)
    _state.log_file = None if log_file is None else Path(log_file)
    _state.limit = int(limit)
    _state.console = None if (Console is None or _state.json_mode) else Console(highlight=False)


def reset() -> None:
    """Restore default UI state."""
    _state.json_mode = False
    _state.quiet = False
    _state.verbose = 0
    _state.log_file = None
    _state.limit = 50
    _state.console = None


def set_log_file(path: str | Path | None) -> None:
    """Set or clear the debug log path (typically ``.solouvm/solouvm.log``)."""
    _state.log_file = None if path is None else Path(path)


def get_log_file() -> Path | None:
    """Return the configured debug log path, if any."""
    return _state.log_file


def is_tty() -> bool:
    """Return True when ``sys.stdout`` is attached to an interactive terminal."""
    isatty = getattr(sys.stdout, "isatty", None)
    if isatty is None:
        return False
    return bool(isatty())


def is_json_mode() -> bool:
    """Return True when ``--json`` mode is active."""
    return _state.json_mode


def is_quiet() -> bool:
    """Return True when ``--quiet`` mode is active."""
    return _state.quiet


def verbosity() -> int:
    """Return the configured verbosity level (0, 1 for ``-v``, 2+ for ``-vv``)."""
    return _state.verbose


def get_limit() -> int:
    """Return the configured output row limit (``0`` means unlimited)."""
    return _state.limit


def use_rich() -> bool:
    """Return True only when vendored ``rich`` is loaded, non-JSON, and stdout is a TTY."""
    return Console is not None and not _state.json_mode and is_tty()


def _get_console() -> Any:
    if _state.console is None and Console is not None:
        _state.console = Console(highlight=False)
    return _state.console


def _emit_stdout(message: str) -> None:
    if use_rich():
        _get_console().print(message, markup=False, highlight=False)
    else:
        print(message)


def info(message: str) -> None:
    """Print an informational message unless ``--json`` or ``--quiet`` is active."""
    if _state.json_mode or _state.quiet:
        return
    _emit_stdout(message)


def status_line(message: str) -> None:
    """Print a progress/status line unless ``--json`` or ``--quiet`` is active."""
    if _state.json_mode or _state.quiet:
        return
    _emit_stdout(message)


def summary(message: str) -> None:
    """Print final summary text; shown even in ``--quiet``, suppressed in ``--json``."""
    if _state.json_mode:
        return
    _emit_stdout(message)


def warning(message: str) -> None:
    """Write a warning to ``sys.stderr`` so stdout stays clean for tables and JSON."""
    has_prefix = message.startswith(("warning:", "solouvm: warning:"))
    text = message if has_prefix else f"warning: {message}"
    print(text, file=sys.stderr)


def error(message: str) -> None:
    """Write a formatted error message to ``sys.stderr``."""
    has_prefix = message.startswith(("solouvm: error:", "error:"))
    text = message if has_prefix else f"solouvm: error: {message}"
    print(text, file=sys.stderr)


def command_log(cmd: str | Sequence[str]) -> None:
    """Trace an external command when ``-vv`` (``verbose >= 2``) is active (Z-07)."""
    if _state.verbose < 2:
        return
    cmd_str = cmd if isinstance(cmd, str) else shlex.join([str(part) for part in cmd])
    print(f"[cmd] {cmd_str}", file=sys.stderr)
    if _state.log_file is not None:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        _state.log_file.parent.mkdir(parents=True, exist_ok=True)
        with _state.log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"{timestamp} {cmd_str}\n")


def format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    """Format a plain ASCII column-aligned table without any ANSI escape sequences."""
    ncols = max([len(headers), *(len(row) for row in rows)], default=0)
    if ncols == 0:
        return ""
    header_cells = [str(headers[i]) if i < len(headers) else "" for i in range(ncols)]
    body_rows = [[str(row[i]) if i < len(row) else "" for i in range(ncols)] for row in rows]
    all_rows = [header_cells, *body_rows] if headers else body_rows
    widths = [max(len(row[col]) for row in all_rows) for col in range(ncols)]
    lines = [
        "  ".join(row[col].ljust(widths[col]) for col in range(ncols)).rstrip() for row in all_rows
    ]
    return "\n".join(lines)


def table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    limit: int | None = None,
) -> tuple[list[list[str]], int]:
    """Render a table (Rich on TTY, plain ASCII otherwise) with optional row truncation.

    Returns ``(shown_rows, omitted_count)``.
    """
    effective_limit = _state.limit if limit is None else limit
    if effective_limit < 0:
        raise ValueError(f"limit must be >= 0, got {effective_limit}")
    if effective_limit > 0 and len(rows) > effective_limit:
        shown = [[str(cell) for cell in row] for row in rows[:effective_limit]]
        omitted = len(rows) - effective_limit
    else:
        shown = [[str(cell) for cell in row] for row in rows]
        omitted = 0

    if _state.json_mode or _state.quiet:
        return shown, omitted

    ncols = max([len(headers), *(len(row) for row in shown)], default=0)
    if ncols == 0:
        return shown, omitted

    if use_rich() and Table is not None:
        tbl = Table(show_header=bool(headers))
        for col in range(ncols):
            tbl.add_column(str(headers[col]) if col < len(headers) else "")
        for row in shown:
            padded = [row[col] if col < len(row) else "" for col in range(ncols)]
            tbl.add_row(*padded)
        _get_console().print(tbl, markup=False)
    else:
        rendered = format_table(headers, shown)
        if rendered:
            print(rendered)

    return shown, omitted
