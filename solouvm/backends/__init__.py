"""Simulator backends: built-in registry and lookup (config.md §2.7)."""

from __future__ import annotations

from collections.abc import Mapping

from solouvm.backends.base import REQUIRED_FIELDS, Backend, resolve_backend
from solouvm.backends.fake import FAKE, fake_sim_source
from solouvm.backends.iverilog import IVERILOG
from solouvm.backends.questa import QUESTA
from solouvm.backends.vcs import VCS
from solouvm.backends.verilator import VERILATOR
from solouvm.backends.xrun import XRUN

__all__ = ["BACKENDS", "REQUIRED_FIELDS", "Backend", "fake_sim_source", "get_backend"]

# 加后端就是加一项，不用改任何 if/elif
BACKENDS: dict[str, Backend] = {b.name: b for b in (VCS, XRUN, QUESTA, IVERILOG, VERILATOR, FAKE)}


def get_backend(name: str, overrides: Mapping[str, object] | None = None) -> Backend:
    """Return backend ``name`` with ``sim`` overrides applied (built-in table never changes).

    Raises ``ConfigError(CFG_INVALID)`` for unknown keys, bad value types, or an unknown
    simulator whose overrides lack a required field.
    """
    return resolve_backend(name, BACKENDS, overrides)
