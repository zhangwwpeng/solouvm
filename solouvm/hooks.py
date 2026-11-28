"""A-side lifecycle hook discovery and execution (W-01, W-02, W-03)."""

from __future__ import annotations

import contextlib
import io
import runpy
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm import output, ui
from solouvm.errors import HookError, SolouvmError

if TYPE_CHECKING:
    from solouvm.collect.files import FileList
    from solouvm.config.schema import Config
    from solouvm.model import Plan, Result
    from solouvm.providers.base import Provider

__all__ = [
    "HOOK_PHASES",
    "HOOK_STAGES",
    "HookContext",
    "HookRunResult",
    "find_hook",
    "is_enabled",
    "reset",
    "run_hook",
    "set_enabled",
]

HOOK_STAGES: tuple[str, ...] = ("main", "gen", "push", "build", "run", "judge")
HOOK_PHASES: tuple[str, ...] = ("pre", "post")


@dataclass
class _HookState:
    enabled: bool = True


_state = _HookState()


def set_enabled(enabled: bool) -> None:
    """Enable or disable A-side hooks for the current invocation."""
    _state.enabled = bool(enabled)


def is_enabled() -> bool:
    """Return True when A-side hooks are enabled."""
    return _state.enabled


def reset() -> None:
    """Restore default enabled state for A-side hooks."""
    _state.enabled = True


@dataclass
class HookContext:
    """Context object passed to ``hook(ctx)`` in A-side hook scripts."""

    stage: str
    phase: str
    root: Path
    command: str = ""
    config: Config | None = None
    plan: Plan | None = None
    provider: Provider | None = None
    run_id: str | None = None
    files: FileList | None = None
    results: Sequence[Result] | None = None
    payload: dict[str, object] | None = None
    exit_code: int | None = None
    extra: dict[str, object] = field(default_factory=dict)
    source: str = ""

    def log(self, message: str) -> None:
        """Emit a stage-scoped log line (collapses on stage success)."""
        text = str(message)
        lines = text.splitlines() or [""]
        for line in lines:
            ui.hook_notice(self.phase, self.stage, "", status="log", detail=line)

    def echo(self, message: str) -> None:
        """Emit a persistent message that remains visible when stages collapse."""
        ui.echo(message, source=self.source or None)

    def info(self, message: str) -> None:
        """Emit a persistent ``[info]`` message tagged with this hook script."""
        ui.info(message, source=self.source or None)

    def warn(self, message: str) -> None:
        """Emit a ``[warn]`` message to stderr tagged with this hook script."""
        ui.warn(message, source=self.source or None)

    def error(self, message: str) -> None:
        """Emit an ``[error]`` message to stderr tagged with this hook script."""
        ui.error(message, source=self.source or None)


@dataclass(frozen=True)
class HookRunResult:
    """Outcome summary returned after a hook script completes."""

    phase: str
    stage: str
    path: Path
    duration_s: float
    output: list[str]


def find_hook(root: str | Path, phase: str, stage: str) -> Path | None:
    """Locate ``pre_hook_<stage>.py`` / ``post_hook_<stage>.py`` under root or ``hooks/``."""
    if phase not in HOOK_PHASES:
        raise ValueError(f"invalid hook phase {phase!r}; expected one of {HOOK_PHASES}")
    if stage not in HOOK_STAGES:
        raise ValueError(f"invalid hook stage {stage!r}; expected one of {HOOK_STAGES}")
    root_path = Path(root)
    filename = f"{phase}_hook_{stage}.py"
    for candidate in (root_path / filename, root_path / "hooks" / filename):
        if candidate.is_file():
            return candidate
    return None


def _rel_hook_path(root_path: Path, hook_path: Path) -> str:
    try:
        return str(hook_path.relative_to(root_path))
    except ValueError:
        return hook_path.name


def _emit_hook_failure(
    *,
    phase: str,
    stage: str,
    rel: str,
    run_id: str | None,
    detail: str,
    started: float,
    emit_json_events: bool,
) -> None:
    if emit_json_events and ui.is_json_mode():
        output.emit_event(
            "hook_done",
            run_id=run_id,
            phase=phase,
            stage=stage,
            path=rel,
            ok=False,
            error=detail,
            duration_s=round(time.monotonic() - started, 4),
        )
    else:
        ui.hook_notice(phase, stage, rel, status="failed", detail=detail)


def run_hook(
    *,
    phase: str,
    stage: str,
    root: str | Path,
    command: str = "",
    config: Config | None = None,
    plan: Plan | None = None,
    provider: Provider | None = None,
    run_id: str | None = None,
    files: FileList | None = None,
    results: Sequence[Result] | None = None,
    payload: dict[str, object] | None = None,
    exit_code: int | None = None,
    extra: dict[str, object] | None = None,
    no_hooks: bool | None = None,
    emit_json_events: bool = False,
) -> HookRunResult | None:
    """Discover and execute the hook for ``(phase, stage)`` if present and enabled."""
    disabled = (not is_enabled()) if no_hooks is None else bool(no_hooks)
    if disabled:
        return None

    root_path = Path(root)
    hook_path = find_hook(root_path, phase, stage)
    if hook_path is None:
        return None

    rel = _rel_hook_path(root_path, hook_path)
    started = time.monotonic()

    if emit_json_events and ui.is_json_mode():
        output.emit_event(
            "hook_start",
            run_id=run_id,
            phase=phase,
            stage=stage,
            path=rel,
        )
    else:
        ui.hook_notice(phase, stage, rel, status="start")

    buf = io.StringIO()
    orig_stdout = sys.stdout
    try:
        with ui.echo_stream_scope(orig_stdout, source=rel), contextlib.redirect_stdout(buf):
            ns = runpy.run_path(str(hook_path))
    except Exception as exc:
        captured_load = buf.getvalue().splitlines()
        for line in captured_load:
            ui.hook_notice(phase, stage, rel, status="log", detail=line)
        _emit_hook_failure(
            phase=phase,
            stage=stage,
            rel=rel,
            run_id=run_id,
            detail=str(exc),
            started=started,
            emit_json_events=emit_json_events,
        )
        raise HookError(
            f"failed to load hook {rel}: {exc}",
            code="HOOK_FAILED",
            file=hook_path,
            hint="fix the hook script or pass --no-hooks to skip",
        ) from None

    fn = ns.get("hook")
    if not callable(fn):
        detail = f"hook file {rel} must define callable hook(ctx)"
        _emit_hook_failure(
            phase=phase,
            stage=stage,
            rel=rel,
            run_id=run_id,
            detail=detail,
            started=started,
            emit_json_events=emit_json_events,
        )
        raise HookError(
            detail,
            code="HOOK_FAILED",
            file=hook_path,
            hint="define 'def hook(ctx): ...' or pass --no-hooks",
        )

    ctx = HookContext(
        stage=stage,
        phase=phase,
        root=root_path,
        command=command,
        config=config,
        plan=plan,
        provider=provider,
        run_id=run_id,
        files=files,
        results=results,
        payload=payload,
        exit_code=exit_code,
        extra=dict(extra) if extra else {},
        source=rel,
    )

    try:
        with ui.echo_stream_scope(orig_stdout, source=rel), contextlib.redirect_stdout(buf):
            fn(ctx)
    except KeyboardInterrupt:
        raise
    except SolouvmError as exc:
        captured = buf.getvalue().splitlines()
        for line in captured:
            ui.hook_notice(phase, stage, rel, status="log", detail=line)
        _emit_hook_failure(
            phase=phase,
            stage=stage,
            rel=rel,
            run_id=run_id,
            detail=exc.message,
            started=started,
            emit_json_events=emit_json_events,
        )
        raise HookError(
            f"{phase}_hook_{stage} failed: {exc.message}",
            code="HOOK_FAILED",
            file=hook_path,
            hint=exc.hint or "pass --no-hooks to skip A-side hooks",
        ) from None
    except Exception as exc:
        captured = buf.getvalue().splitlines()
        for line in captured:
            ui.hook_notice(phase, stage, rel, status="log", detail=line)
        detail = f"{type(exc).__name__}: {exc}"
        _emit_hook_failure(
            phase=phase,
            stage=stage,
            rel=rel,
            run_id=run_id,
            detail=detail,
            started=started,
            emit_json_events=emit_json_events,
        )
        raise HookError(
            f"{phase}_hook_{stage} raised {detail}",
            code="HOOK_FAILED",
            file=hook_path,
            hint="pass --no-hooks to skip A-side hooks",
        ) from None

    captured_lines = buf.getvalue().splitlines()
    for line in captured_lines:
        ui.hook_notice(phase, stage, rel, status="log", detail=line)

    duration_s = round(time.monotonic() - started, 4)
    if emit_json_events and ui.is_json_mode():
        output.emit_event(
            "hook_done",
            run_id=run_id,
            phase=phase,
            stage=stage,
            path=rel,
            ok=True,
            duration_s=duration_s,
        )
    else:
        ui.hook_notice(phase, stage, rel, status="done", duration_s=duration_s)

    return HookRunResult(
        phase=phase,
        stage=stage,
        path=hook_path,
        duration_s=duration_s,
        output=captured_lines,
    )
