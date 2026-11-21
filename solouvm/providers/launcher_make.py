"""Make launcher running jobs directly under ``make -j`` without batch submission."""

from __future__ import annotations

from typing import TYPE_CHECKING

from solouvm.providers.base import Transport

if TYPE_CHECKING:
    from solouvm.model import Plan

__all__ = ["MakeLauncher"]


class MakeLauncher:
    """Default launcher: ``SUBMIT`` is empty so ``make`` runs jobs directly."""

    kind: str = "make"

    def make_vars(self, plan: Plan) -> dict[str, str]:
        """Return empty Make overrides; ``run.mk`` runs jobs directly."""
        _ = plan
        return {}

    def jobs(self, requested: int) -> int:
        """Return requested ``make -j`` concurrency unchanged."""
        return requested

    def stop(self, transport: Transport, run_id: str) -> None:
        """No-op for MakeLauncher; ``launch.sh``/caller terminates the process group."""
        _ = transport, run_id
