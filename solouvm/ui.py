"""Terminal UI and output formatting; sole entry point for vendored ``rich`` (N-02, N-22, Z-07)."""

from __future__ import annotations

import contextlib
import math
import shlex
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_VENDOR = Path(__file__).parent / "_vendor"


def _load_vendor() -> tuple[Any, Any, Any, Any, Any, Any, Any]:
    """Load Rich components from ``_vendor/rich`` if present."""
    if not (_VENDOR / "rich").is_dir():
        return (None,) * 7
    vendor_str = str(_VENDOR)
    if vendor_str not in sys.path:
        sys.path.insert(0, vendor_str)
    try:
        from rich.console import Console as rich_console
        from rich.console import Group as rich_group
        from rich.live import Live as rich_live
        from rich.rule import Rule as rich_rule
        from rich.spinner import Spinner as rich_spinner
        from rich.table import Table as rich_table
        from rich.text import Text as rich_text
    except ImportError:
        return (None,) * 7
    return (
        rich_console,
        rich_table,
        rich_group,
        rich_live,
        rich_rule,
        rich_spinner,
        rich_text,
    )


Console, Table, _Group, _Live, _Rule, _Spinner, _Text = _load_vendor()


@dataclass
class _UIState:
    json_mode: bool = False
    quiet: bool = False
    verbose: int = 0
    log_file: Path | None = None
    limit: int = 50
    console: Any = None
    active_live: LiveRunner | None = None
    echo_stream: Any = None
    echo_source: str | None = None


_state = _UIState()
_MAX_STAGE_TAIL_LINES = 25
_STAGE_VIEW_HEIGHT = 8
_STAGE_DISPLAY_NAMES: dict[str, str] = {
    "gen": "Generate",
    "push": "Sync",
    "build": "Compile",
    "run": "Simulate",
    "judge": "Judge",
}
_STAGE_NAME_WIDTH = 8
_STAGE_DETAIL_WIDTH = 34
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

__all__ = [
    "Console",
    "LiveRunner",
    "Table",
    "command_log",
    "echo",
    "error",
    "format_table",
    "get_limit",
    "get_log_file",
    "hook_notice",
    "info",
    "is_json_mode",
    "is_quiet",
    "is_tty",
    "live_runner",
    "print_run_summary",
    "reload_vendor",
    "reset",
    "set_log_file",
    "setup",
    "stage_done",
    "stage_start",
    "status_line",
    "summary",
    "table",
    "use_rich",
    "verbosity",
    "warning",
]


def reload_vendor() -> None:
    """Reload vendored ``rich`` classes or fall back to ``None`` when absent."""
    console_cls, table_cls, group_cls, live_cls, rule_cls, spinner_cls, text_cls = _load_vendor()
    module_ns = globals()
    module_ns["Console"] = console_cls
    module_ns["Table"] = table_cls
    module_ns["_Group"] = group_cls
    module_ns["_Live"] = live_cls
    module_ns["_Rule"] = rule_cls
    module_ns["_Spinner"] = spinner_cls
    module_ns["_Text"] = text_cls
    _state.console = None


class LiveRunner:
    """Context manager pinning an auto-refreshing footer above streaming logs in TTY mode."""

    def __init__(self, initial_status: str = "") -> None:
        self._status = initial_status
        self._stage_tag = ""
        self._dirty = False
        self._lock = threading.Lock()
        self._active = False
        self._fixed_view = False
        self._in_stage = False
        self._stage_header: tuple[str, str, str] | None = None
        self._stage_lines: deque[tuple[str, str, str]] = deque(maxlen=_MAX_STAGE_TAIL_LINES)
        self._stage_total_lines = 0
        self._started_at = time.monotonic()
        self._live: Any = None
        self._prev_live: LiveRunner | None = None
        self._delegate: LiveRunner | None = None

    @property
    def is_live(self) -> bool:
        """Return True when a Rich ``Live`` footer is currently active."""
        if self._delegate is not None:
            return self._delegate.is_live
        return self._live is not None

    @property
    def is_collapsible_stage(self) -> bool:
        """Return True when an interactive stage viewport should collapse on completion."""
        if self._delegate is not None:
            return self._delegate.is_collapsible_stage
        with self._lock:
            return self._live is not None and self._in_stage and _state.verbose == 0

    def set_stage(
        self,
        stage: str,
        *,
        index: int | None = None,
        total: int | None = None,
    ) -> None:
        """Set the active pipeline stage label displayed on the pinned footer."""
        if self._delegate is not None:
            self._delegate.set_stage(stage, index=index, total=total)
            return
        new_tag = _format_stage_tag(stage, index=index, total=total)
        with self._lock:
            if new_tag != self._stage_tag:
                self._stage_tag = new_tag
                self._dirty = True

    def begin_stage(
        self,
        stage: str,
        detail: str = "",
        *,
        index: int | None = None,
        total: int | None = None,
    ) -> bool:
        """Start a stage viewport inside the Live area; return True if collapsible."""
        if self._delegate is not None:
            return self._delegate.begin_stage(stage, detail, index=index, total=total)
        idx_tag, stage_col = _format_stage_cols(stage, index=index, total=total)
        new_tag = _format_stage_tag(stage, index=index, total=total)
        with self._lock:
            self._stage_tag = new_tag
            self._status = detail or stage
            self._dirty = False
            live = self._live
            collapsible = live is not None and _state.verbose == 0
            if collapsible:
                self._fixed_view = True
                self._in_stage = True
                self._stage_header = (idx_tag, stage_col, detail)
                self._stage_lines.clear()
                self._stage_total_lines = 0
            else:
                self._in_stage = False
                self._stage_header = None
                self._stage_lines.clear()
                self._stage_total_lines = 0
        if live is not None:
            live.refresh()
        return collapsible

    def add_stage_entries(self, entries: Sequence[tuple[str, str, str]]) -> None:
        """Append styled/plain log entries to the active collapsible stage viewport."""
        if self._delegate is not None:
            self._delegate.add_stage_entries(entries)
            return
        if not entries:
            return
        with self._lock:
            self._stage_lines.extend(entries)
            self._stage_total_lines += len(entries)
            live = self._live
        if live is not None:
            live.refresh()

    def end_stage(
        self,
        summary_renderable: Any,
        *,
        ok: bool,
        is_final: bool = False,
    ) -> bool:
        """Collapse the live stage viewport into a single permanent summary line."""
        if self._delegate is not None:
            return self._delegate.end_stage(summary_renderable, ok=ok, is_final=is_final)
        with self._lock:
            live = self._live
            if live is None or not self._in_stage:
                return False
            header = self._stage_header
            tail = list(self._stage_lines)
            self._in_stage = False
            self._stage_header = None
            self._stage_lines.clear()
            self._stage_total_lines = 0
            if is_final:
                self._fixed_view = False

        # Refresh first so Rich clears/updates the viewport cleanly at its current height
        # before printing permanent failure tail lines and the collapsed summary above it.
        live.refresh()
        if not ok and (header is not None or tail):
            if header is not None and _Text is not None:
                h_idx, h_col, h_detail = header
                htxt = _Text()
                htxt.append("▾ ", style="bold red")
                if h_idx:
                    htxt.append(f"{h_idx} ", style="bold red")
                if h_col:
                    htxt.append(h_col, style="bold red")
                if h_detail:
                    htxt.append(f"  {h_detail}")
                live.console.print(htxt)
            for p_style, prefix, text in tail:
                if _Text is not None:
                    ltxt = _Text()
                    ltxt.append("  │ ", style="dim")
                    if prefix:
                        ltxt.append(prefix, style=p_style)
                    ltxt.append(text)
                    live.console.print(ltxt)
                else:
                    live.console.print(f"  | {prefix}{text}", markup=False, highlight=False)
        live.console.print(summary_renderable)
        return True

    def _render_footer(self) -> Any:
        with self._lock:
            if not self._active:
                return ""
            fixed_view = self._fixed_view
            in_stage = self._in_stage
            header = self._stage_header
            lines_snap = list(self._stage_lines)
            total_lines = self._stage_total_lines
            stage_tag = self._stage_tag
            status = self._status
            live = self._live
            started_at = self._started_at

        renderables: list[Any] = []
        if fixed_view:
            console_h = getattr(getattr(live, "console", None), "height", 24) or 24
            view_rows = max(3, min(_STAGE_VIEW_HEIGHT, console_h - 8))
            if in_stage and header is not None:
                h_idx, h_col, h_detail = header
                htxt = _Text(no_wrap=True, overflow="ellipsis")
                htxt.append("▾ ", style="bold blue")
                if h_idx:
                    htxt.append(f"{h_idx} ", style="bold cyan")
                if h_col:
                    htxt.append(h_col, style="bold")
                if h_detail:
                    htxt.append(f"  {h_detail}")
                renderables.append(htxt)
            else:
                renderables.append(_Text(""))

            if in_stage:
                if total_lines > view_rows:
                    keep = max(1, view_rows - 1)
                    visible = lines_snap[-keep:]
                    omit_txt = _Text(no_wrap=True, overflow="ellipsis")
                    omit_txt.append(
                        f"  │ … ({total_lines - len(visible)} earlier lines)",
                        style="dim",
                    )
                    renderables.append(omit_txt)
                    used_rows = 1 + len(visible)
                else:
                    visible = lines_snap[-view_rows:]
                    used_rows = len(visible)
                for p_style, prefix, text in visible:
                    ltxt = _Text(no_wrap=True, overflow="ellipsis")
                    ltxt.append("  │ ", style="dim")
                    if prefix:
                        ltxt.append(prefix, style=p_style)
                    ltxt.append(text)
                    renderables.append(ltxt)
                pad_rows = view_rows - used_rows
            else:
                pad_rows = view_rows
            for _ in range(max(0, pad_rows)):
                renderables.append(_Text(""))

        now = time.monotonic()
        elapsed = f"{now - started_at:.1f}s"
        frame = _SPINNER_FRAMES[int((now - started_at) * 10) % len(_SPINNER_FRAMES)]
        body = f"{stage_tag}  {status}".strip() if stage_tag else (status or "running")
        footer_line = _Text(no_wrap=True, overflow="ellipsis")
        footer_line.append(f"{frame} ", style="cyan")
        footer_line.append(f"{body} | {elapsed}", style="bold cyan")
        renderables.append(_Rule(style="dim"))
        renderables.append(footer_line)
        return _Group(*renderables)

    def __enter__(self) -> LiveRunner:  # noqa: PYI034
        if _state.active_live is not None:
            self._delegate = _state.active_live
            if self._status:
                self._delegate.update(self._status)
            return self
        self._started_at = time.monotonic()
        if (
            use_rich()
            and not _state.quiet
            and Console is not None
            and _Live is not None
            and _Text is not None
            and _Rule is not None
            and _Group is not None
        ):
            live_console = Console(file=sys.stdout, highlight=False)
            live = _Live(
                console=live_console,
                get_renderable=self._render_footer,
                refresh_per_second=8,
                transient=True,
                redirect_stdout=False,
                redirect_stderr=False,
            )
            self._active = True
            self._live = live
            self._prev_live = _state.active_live
            _state.active_live = self
            try:
                live.start(refresh=True)
            except BaseException:
                _state.active_live = self._prev_live
                self._prev_live = None
                self._live = None
                self._active = False
                raise
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._delegate is not None:
            self._delegate = None
            return
        with self._lock:
            live = self._live
            prev = self._prev_live
            self._prev_live = None
            self._fixed_view = False
            self._in_stage = False
            self._stage_header = None
            self._stage_lines.clear()
            self._stage_total_lines = 0
        if _state.active_live is self:
            _state.active_live = prev
        try:
            if live is not None:
                live.stop()
        finally:
            with self._lock:
                self._live = None
                self._active = False

    def update(self, status: str) -> None:
        """Update the pinned footer status text and trigger a refresh when live."""
        if self._delegate is not None:
            self._delegate.update(status)
            return
        with self._lock:
            if status == self._status and not self._dirty:
                return
            self._status = status
            self._dirty = False
            live = self._live
        if live is not None:
            live.refresh()

    def log_batch(self, lines: Sequence[str]) -> None:
        """Print multiple log lines into the stage viewport or live console."""
        if self._delegate is not None:
            self._delegate.log_batch(lines)
            return
        if _state.json_mode or _state.quiet or not lines:
            return
        if self.is_collapsible_stage:
            self.add_stage_entries([("", "", line) for line in lines])
            return
        live = self._live
        if live is not None:
            for line in lines:
                live.console.print(line, markup=False, highlight=False)
        else:
            for line in lines:
                print(line)

    def log(self, line: str) -> None:
        """Print one log line above the footer (or to plain stdout outside Rich TTY)."""
        self.log_batch((line,))

    def print_permanent(self, lines: Sequence[str], *, source: str = "") -> None:
        """Print lines permanently above any active stage viewport without folding them."""
        if self._delegate is not None:
            self._delegate.print_permanent(lines, source=source)
            return
        if _state.json_mode or _state.quiet or not lines:
            return
        with self._lock:
            live = self._live
        if live is not None:
            _print_echo_lines(live.console, lines, source=source)
        else:
            stream = _state.echo_stream
            out = stream if stream is not None else sys.stdout
            prefix = f"  [{source}] " if source else ""
            for line in lines:
                print(f"{prefix}{line}", file=out)


def live_runner(initial_status: str = "") -> LiveRunner:
    """Create a :class:`LiveRunner` for live log streaming and bottom status updates."""
    return LiveRunner(initial_status=initial_status)


def _format_stage_cols(
    stage: str,
    *,
    index: int | None = None,
    total: int | None = None,
) -> tuple[str, str]:
    idx_tag = f"[{index}/{total}]" if (index is not None and total is not None) else ""
    label = _STAGE_DISPLAY_NAMES.get(stage, stage)
    stage_col = f"{label:<{_STAGE_NAME_WIDTH}}" if label else ""
    return idx_tag, stage_col


def _format_stage_tag(
    stage: str,
    *,
    index: int | None = None,
    total: int | None = None,
) -> str:
    idx_tag, stage_col = _format_stage_cols(stage, index=index, total=total)
    if idx_tag and stage_col:
        return f"{idx_tag} {stage_col}"
    return idx_tag or stage_col


def _fmt_aligned_dur(duration_s: float | None) -> str:
    if duration_s is None:
        return ""
    return f"{duration_s:6.2f}s"


def _fmt_dur(duration_s: float | None) -> str:
    if duration_s is None:
        return ""
    if duration_s < 1.0:
        return f" ({duration_s:.2f}s)"
    return f" ({duration_s:.1f}s)"


def _print_rich_or_plain(plain_text: str, rich_builder: Callable[[], Any]) -> None:
    active = _state.active_live
    if active is not None and active._live is not None and _Text is not None:
        active._live.console.print(rich_builder())
    elif use_rich() and _Text is not None:
        _get_console().print(rich_builder())
    else:
        stream = _state.echo_stream
        print(plain_text, file=stream if stream is not None else sys.stdout)


@contextlib.contextmanager
def echo_stream_scope(stream: Any, *, source: str | None = None) -> Iterator[None]:
    """Temporarily bind fallback stream and source label for :func:`echo` and hook logs."""
    prev_stream = _state.echo_stream
    prev_source = _state.echo_source
    _state.echo_stream = stream
    _state.echo_source = source
    try:
        yield
    finally:
        _state.echo_stream = prev_stream
        _state.echo_source = prev_source


def _infer_caller_source() -> str:
    skip_files = {
        Path(__file__).resolve(),
        (Path(__file__).parent / "hooks.py").resolve(),
    }
    cwd = Path.cwd().resolve()
    frame: Any = sys._getframe(1)
    for _ in range(8):
        raw_name = frame.f_code.co_filename
        if raw_name and not raw_name.startswith("<"):
            try:
                resolved = Path(raw_name).resolve()
            except OSError:
                resolved = None
            if resolved is not None and resolved not in skip_files:
                try:
                    return str(resolved.relative_to(cwd))
                except ValueError:
                    return resolved.name
        if frame.f_back is None:
            break
        frame = frame.f_back
    return ""


def _print_echo_lines(con: Any, lines: Sequence[str], *, source: str) -> None:
    for line in lines:
        if source and _Text is not None:
            txt = _Text()
            txt.append(f"  [{source}] ", style="cyan")
            txt.append(line)
            con.print(txt)
        else:
            con.print(line, markup=False, highlight=False)


def echo(message: str, *, source: str | None = None) -> None:
    """Print a persistent user message tagged with its source script path."""
    if _state.json_mode or _state.quiet:
        return
    eff_source = (
        source
        if source is not None
        else (_state.echo_source if _state.echo_source is not None else _infer_caller_source())
    )
    text = str(message)
    lines = text.splitlines() or [""]
    active = _state.active_live
    if active is not None and active.is_live:
        active.print_permanent(lines, source=eff_source)
        return
    if use_rich():
        _print_echo_lines(_get_console(), lines, source=eff_source)
    else:
        stream = _state.echo_stream
        out = stream if stream is not None else sys.stdout
        prefix = f"  [{eff_source}] " if eff_source else ""
        for line in lines:
            print(f"{prefix}{line}", file=out)


def stage_start(
    stage: str,
    detail: str = "",
    *,
    index: int | None = None,
    total: int | None = None,
) -> None:
    """Announce transition into a pipeline stage and update the live footer."""
    handled = False
    active = _state.active_live
    if active is not None:
        handled = active.begin_stage(stage, detail, index=index, total=total)
    if _state.json_mode or _state.quiet or handled:
        return
    idx_tag, stage_col = _format_stage_cols(stage, index=index, total=total)
    tag = _format_stage_tag(stage, index=index, total=total)
    plain = f"==> {tag}  {detail}" if detail else f"==> {tag}"

    def _build() -> Any:
        txt = _Text()
        txt.append("==> ", style="bold blue")
        if idx_tag:
            txt.append(f"{idx_tag} ", style="bold cyan")
        if stage_col:
            txt.append(stage_col, style="bold")
        if detail:
            txt.append(f"  {detail}")
        return txt

    _print_rich_or_plain(plain, _build)


def stage_done(
    stage: str,
    detail: str = "",
    *,
    index: int | None = None,
    total: int | None = None,
    ok: bool = True,
    duration_s: float | None = None,
) -> None:
    """Announce completion of a pipeline stage above the live footer."""
    if _state.json_mode or _state.quiet:
        return
    idx_tag, stage_col = _format_stage_cols(stage, index=index, total=total)
    tag = _format_stage_tag(stage, index=index, total=total)
    msg = detail or ("done" if ok else "failed")
    msg_col = f"{msg:<{_STAGE_DETAIL_WIDTH}}" if len(msg) <= _STAGE_DETAIL_WIDTH else f"{msg}  "
    dur_col = _fmt_aligned_dur(duration_s)
    mark = "[ok]  " if ok else "[FAIL]"
    dur_part = f"  {dur_col}" if dur_col else ""
    plain = f"{mark} {tag}  {msg_col}{dur_part}".rstrip()

    def _build() -> Any:
        txt = _Text()
        txt.append("✓ " if ok else "✗ ", style="bold green" if ok else "bold red")
        if idx_tag:
            txt.append(f"{idx_tag} ", style="bold cyan")
        if stage_col:
            txt.append(stage_col, style="bold")
        txt.append(f"  {msg_col}")
        if dur_col:
            txt.append(f"  {dur_col}", style="dim")
        return txt

    is_final = index is not None and total is not None and index >= total
    active = _state.active_live
    if (
        active is not None
        and _Text is not None
        and active.end_stage(_build(), ok=ok, is_final=is_final)
    ):
        return
    _print_rich_or_plain(plain, _build)


def hook_notice(
    phase: str,
    stage: str,
    path: str,
    *,
    status: str = "start",
    detail: str = "",
    duration_s: float | None = None,
) -> None:
    """Emit a hook execution status or log line without disrupting JSON or Rich Live."""
    active = _state.active_live
    if status == "start" and active is not None:
        active.update(f"hook {phase}:{stage}")
    if _state.json_mode or _state.quiet:
        return

    if status == "start":
        prefix_style, prefix, body = "bold yellow", "[hook] ", f"{phase}:{stage} ({path})"
        plain = f"  {prefix}{body}"
    elif status == "log":
        prefix_style, prefix, body = "dim", f"[{phase}_{stage}] ", detail
        plain = f"    {prefix}{body}"
    elif status == "done":
        dur = _fmt_dur(duration_s)
        prefix_style, prefix, body = "green", "[hook] ", f"{phase}:{stage} ok{dur}"
        plain = f"  {prefix}{body}"
    else:
        prefix_style, prefix, body = "bold red", "[hook] ", f"{phase}:{stage} failed: {detail}"
        plain = f"  {prefix}{body}"

    if active is not None and active.is_collapsible_stage:
        active.add_stage_entries([(prefix_style, prefix, body)])
        return

    def _build() -> Any:
        txt = _Text()
        indent = "    " if status == "log" else "  "
        txt.append(f"{indent}{prefix}", style=prefix_style)
        txt.append(body)
        return txt

    _print_rich_or_plain(plain, _build)


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
    _state.active_live = None
    _state.echo_stream = None
    _state.echo_source = None


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
    active = _state.active_live
    if active is not None and active.is_collapsible_stage:
        active.log(message)
    elif active is not None and active._live is not None:
        active._live.console.print(message, markup=False, highlight=False)
    elif use_rich():
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


_STATUS_ORDER: tuple[str, ...] = (
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
_PASS_STATUSES: frozenset[str] = frozenset({"pass", "xfail", "skip"})
_CATEGORY_ORDER: tuple[str, ...] = ("design", "execution", "environment")


def _status_style(status: str) -> str:
    if status in ("pass", "xfail"):
        return "bold green"
    if status == "skip":
        return "dim"
    if status == "incomplete":
        return "bold yellow"
    return "bold red"


def _job_duration(job: dict[str, object]) -> float:
    attempts = job.get("attempts")
    if isinstance(attempts, list) and attempts:
        total = 0.0
        found = False
        for att in attempts:
            if isinstance(att, dict) and not att.get("wave"):
                dur = att.get("duration_s")
                if (
                    isinstance(dur, (int, float))
                    and not isinstance(dur, bool)
                    and math.isfinite(dur)
                ):
                    total += float(dur)
                    found = True
        if found:
            return round(total, 3)
    raw = job.get("duration_s")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(raw):
        return round(float(raw), 3)
    return 0.0


def _job_last_log(job: dict[str, object]) -> str:
    attempts = job.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return "-"
    non_wave = [a for a in attempts if isinstance(a, dict) and not a.get("wave")]
    target = non_wave[-1] if non_wave else attempts[-1]
    if isinstance(target, dict) and isinstance(target.get("log"), str) and target["log"]:
        return target["log"]
    return "-"


def _format_job_detail(job: dict[str, object], status: str) -> Any:
    if status in _PASS_STATUSES:
        weak = job.get("weak_signals")
        if isinstance(weak, list) and weak:
            return _Text("weak: " + "; ".join(str(s) for s in weak), style="yellow")
        return _Text("-", style="dim")

    txt = _Text()
    reason = str(job.get("reason", "") or "")
    flaky_tag = " [flaky]" if job.get("flaky") else ""
    case = str(job.get("case", "") or "")
    seed = job.get("seed", "")
    head = f"{reason}{flaky_tag}".strip() or f"case={case}, seed={seed}"
    txt.append(head)

    notes: list[str] = []
    first_err = job.get("first_error")
    if isinstance(first_err, dict):
        notes.append(f"L{first_err.get('line')}: {first_err.get('text')}")
    known = job.get("known")
    if isinstance(known, list) and known:
        notes.append("known=" + ", ".join(str(k) for k in known))
    triage = job.get("triage")
    if isinstance(triage, dict):
        stale_tag = " (stale)" if triage.get("stale") else ""
        notes.append(
            f"triage={triage.get('task')} {triage.get('verdict')} "
            f"[{triage.get('review')}]{stale_tag}"
        )
    if notes:
        txt.append(" | " + " | ".join(notes), style="dim")
    return txt


def _build_status_breakdown(by_status: dict[object, object]) -> Any:
    txt = _Text()
    nonzero = [
        (s, int(by_status.get(s, 0) or 0))
        for s in _STATUS_ORDER
        if int(by_status.get(s, 0) or 0) > 0
    ]
    if not nonzero:
        txt.append("-", style="dim")
        return txt
    for i, (sname, cnt) in enumerate(nonzero):
        if i > 0:
            txt.append("  ")
        txt.append(f"{sname}={cnt}", style=_status_style(sname))
    return txt


def _build_run_table(payload: dict[str, object]) -> Any:
    run_id = str(payload.get("run_id", ""))
    partial = bool(payload.get("partial", False))
    summary_raw = payload.get("summary")
    sdict = summary_raw if isinstance(summary_raw, dict) else {}
    total = int(sdict.get("total", 0) or 0)
    passed = int(sdict.get("passed", 0) or 0)
    duration_s = float(sdict.get("duration_s", 0.0) or 0.0)
    by_status = sdict.get("by_status") if isinstance(sdict.get("by_status"), dict) else {}
    by_category = sdict.get("by_category") if isinstance(sdict.get("by_category"), dict) else {}

    jobs = [j for j in (payload.get("jobs") or []) if isinstance(j, dict)]
    ordered = sorted(
        jobs,
        key=lambda j: (
            0 if str(j.get("status", "")) not in _PASS_STATUSES else 1,
            "" if str(j.get("status", "")) in _PASS_STATUSES else str(j.get("status", "")),
            -_job_duration(j),
            str(j.get("job_id", "")),
        ),
    )
    eff_limit = _state.limit
    if eff_limit > 0 and len(ordered) > eff_limit:
        shown = ordered[:eff_limit]
        omitted = len(ordered) - eff_limit
    else:
        shown = ordered
        omitted = 0

    cat_parts = [
        f"{c}={int(by_category.get(c, 0) or 0)}"
        for c in _CATEGORY_ORDER
        if int(by_category.get(c, 0) or 0) > 0
    ]
    has_cat = bool(cat_parts) or any(bool(j.get("category")) for j in shown)
    has_fail = any(str(j.get("status", "")) not in _PASS_STATUSES for j in shown)

    if partial:
        pass_style = "bold yellow"
    elif total > 0 and passed == total:
        pass_style = "bold green"
    else:
        pass_style = "bold red"
    rate_str = f"{100.0 * passed / total:.1f}%" if total else "-"

    title = _Text()
    title.append("Run ", style="dim")
    title.append(run_id, style="bold")
    if partial:
        title.append(" [PARTIAL]", style="bold yellow")

    caption = (
        _Text(f"... omitted {omitted} job(s) (pass --limit 0 to show all)", style="dim")
        if omitted
        else None
    )
    tbl = Table(
        title=title,
        title_justify="left",
        title_style="none",
        caption=caption,
        caption_justify="left",
        caption_style="dim",
        show_header=True,
        show_footer=True,
        footer_style="none",
    )
    tbl.add_column("JOB", footer=_Text(f"Total ({passed}/{total})", style=pass_style))
    tbl.add_column("STATUS", footer=_Text(rate_str, style=pass_style))
    tbl.add_column("TIME", justify="right", footer=_Text(f"{duration_s:.3f}s", style="bold"))
    if has_cat:
        cat_footer = (
            _Text("  ".join(cat_parts), style="dim") if cat_parts else _Text("-", style="dim")
        )
        tbl.add_column("CAT", footer=cat_footer)
    tbl.add_column("DETAIL", footer=_build_status_breakdown(by_status))
    if has_fail:
        tbl.add_column("LOG", footer="")

    if not shown:
        empty_row: list[Any] = [
            _Text("(no jobs)", style="dim"),
            _Text("-", style="dim"),
            _Text("-", style="dim"),
        ]
        if has_cat:
            empty_row.append(_Text("-", style="dim"))
        empty_row.append(_Text("-", style="dim"))
        if has_fail:
            empty_row.append(_Text("-", style="dim"))
        tbl.add_row(*empty_row)
    else:
        for job in shown:
            status = str(job.get("status", ""))
            row: list[Any] = [
                _Text(str(job.get("job_id", ""))),
                _Text(status, style=_status_style(status)),
                f"{_job_duration(job):.3f}s",
            ]
            if has_cat:
                row.append(str(job.get("category") or "-"))
            row.append(_format_job_detail(job, status))
            if has_fail:
                log_cell = "-" if status in _PASS_STATUSES else _job_last_log(job)
                row.append(_Text(log_cell, style="dim"))
            tbl.add_row(*row)

    return tbl


def _print_signatures(con: Any, signatures: list[dict[str, object]]) -> None:
    hdr = _Text()
    hdr.append(f"Signatures ({len(signatures)}):", style="bold")
    con.print(hdr)
    for sig in signatures:
        cases = ", ".join(str(c) for c in (sig.get("cases") or []))
        cases_part = f", {cases}" if cases else ""
        line = _Text("  ")
        line.append(str(sig.get("id", "")), style="bold red")
        line.append(f" (x{sig.get('count', 0)}{cases_part})", style="dim")
        line.append(f": {sig.get('text', '')}")
        con.print(line)


def _print_waivers_and_hints(con: Any, payload: dict[str, object]) -> None:
    waivers = payload.get("waivers")
    if isinstance(waivers, dict):
        used = waivers.get("used") or []
        unused = waivers.get("unused") or []
        expired = waivers.get("expired") or []
        if used or unused or expired:
            hdr = _Text("Waivers:", style="bold")
            con.print(hdr)
            if used:
                used_str = ", ".join(
                    f"{w.get('id')} ({w.get('lines')} lines)" for w in used if isinstance(w, dict)
                )
                line = _Text("  used: ", style="green")
                line.append(used_str)
                con.print(line)
            if unused:
                line = _Text("  unused: ", style="dim")
                line.append(", ".join(str(u) for u in unused))
                con.print(line)
            if expired:
                line = _Text("  expired: ", style="yellow")
                line.append(", ".join(str(e) for e in expired))
                con.print(line)

    hints = payload.get("hints")
    if isinstance(hints, list) and hints:
        for hint in hints:
            htxt = _Text()
            htxt.append("hint: ", style="bold cyan")
            htxt.append(str(hint))
            con.print(htxt)


def _print_compare(con: Any, compare: dict[str, object]) -> None:
    baseline = str(compare.get("baseline_run_id", ""))
    hdr = _Text()
    hdr.append(f"Compare vs {baseline}:", style="bold")
    con.print(hdr)
    emitted = False
    for key, label, style in (
        ("new_failures", "new failure", "bold red"),
        ("newly_fixed", "newly fixed", "bold green"),
        ("still_failing", "still failing", "yellow"),
    ):
        for item in compare.get(key) or []:
            if not isinstance(item, dict):
                continue
            emitted = True
            prev = item.get("prev_status")
            cur = item.get("status")
            trans = f"{prev} -> {cur}" if prev else str(cur)
            reason = str(item.get("reason", "") or "")
            line = _Text("  ")
            line.append(f"{label:<13} ", style=style)
            line.append(str(item.get("job_id", "")))
            line.append(f"  {trans}", style="dim")
            if reason:
                line.append(f"  ({reason})")
            con.print(line)
    if not emitted:
        con.print(_Text("  (no status changes)", style="dim"))


def print_run_summary(
    payload: dict[str, object],
    *,
    compare: dict[str, object] | None = None,
    fallback_text: str = "",
) -> None:
    """Render regression results as a single Rich table on TTY or fallback plain text."""
    if _state.json_mode:
        return
    if not (use_rich() and Table is not None and _Text is not None):
        if fallback_text:
            summary(fallback_text)
        return

    con = _get_console()
    con.print(_build_run_table(payload), markup=False)

    sigs = [s for s in (payload.get("signatures") or []) if isinstance(s, dict)]
    if sigs:
        _print_signatures(con, sigs)

    _print_waivers_and_hints(con, payload)

    if compare is not None:
        _print_compare(con, compare)
