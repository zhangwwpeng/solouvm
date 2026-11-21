"""No-op sync used when A and B share the same filesystem."""

from __future__ import annotations

from collections.abc import Iterable

from solouvm.providers.base import Transport

__all__ = ["NoneSync"]


class NoneSync:
    """Sync implementation for shared filesystems where no file copy is needed."""

    kind: str = "none"

    def push(self, transport: Transport, tree: str, files: Iterable[str]) -> None:
        """No-op: A and B already access the same directory tree."""
        _ = transport, tree, files
