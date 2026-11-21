"""LSF launcher injecting ``bsub -K`` via ``SUBMIT`` and cancelling via ``bkill``."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from solouvm.errors import ConfigError
from solouvm.gen.shell import check_no_newlines, make_escape
from solouvm.providers.base import Transport

if TYPE_CHECKING:
    from solouvm.model import Plan

__all__ = ["LsfLauncher"]


class LsfLauncher:
    """Launcher submitting each Make target synchronously through ``bsub -K``."""

    kind: str = "lsf"

    def __init__(
        self,
        queue: str | None = None,
        resources: str | None = None,
        extra: Sequence[str] = (),
    ) -> None:
        self.queue: str | None = (
            check_no_newlines(queue, field="launcher.queue") if queue is not None else None
        )
        self.resources: str | None = (
            check_no_newlines(resources, field="launcher.resources")
            if resources is not None
            else None
        )
        self.extra: tuple[str, ...] = tuple(
            check_no_newlines(str(arg), field="launcher.extra") for arg in extra
        )

    def make_vars(self, plan: Plan) -> dict[str, str]:
        """Build ``SUBMIT`` variable assigning ``bsub -K`` flags and per-job ``-J`` name."""
        run_id = check_no_newlines(plan.run_id, field="plan.run_id")
        if "'" in run_id:
            raise ConfigError(
                f"plan.run_id cannot contain single quotes: {run_id!r}",
                code="CFG_INVALID",
            )
        run_id_make = run_id.replace("$", "$$")
        parts: list[str] = ["bsub", "-K"]
        if self.queue:
            parts.extend(["-q", make_escape(self.queue, field="launcher.queue")])
        if self.resources:
            parts.extend(["-R", make_escape(self.resources, field="launcher.resources")])
        for opt in self.extra:
            parts.append(make_escape(opt, field="launcher.extra"))
        # Single-quoted for shell, with $(notdir $@) expanded per target by Make.
        parts.extend(["-J", f"'solouvm_{run_id_make}_$(notdir $@)'"])
        return {"SUBMIT": " ".join(parts)}

    def jobs(self, requested: int) -> int:
        """Return requested concurrency; ``make -j`` bounds active ``bsub -K`` jobs."""
        return requested

    def stop(self, transport: Transport, run_id: str) -> None:
        """Cancel all LSF jobs belonging to ``run_id`` via ``bkill -J``."""
        clean_id = check_no_newlines(run_id, field="run_id")
        transport.exec(["bkill", "-J", f"solouvm_{clean_id}_*"], cwd=".", check=False)
