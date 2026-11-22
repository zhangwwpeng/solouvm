"""Simulator backends: built-in registry and lookup (config.md §2.7)."""

from __future__ import annotations

import shlex
from collections.abc import Mapping

from solouvm.backends.base import REQUIRED_FIELDS, Backend, resolve_backend
from solouvm.backends.fake import FAKE, fake_sim_source
from solouvm.backends.questa import QUESTA
from solouvm.backends.vcs import VCS
from solouvm.backends.verilator import VERILATOR
from solouvm.backends.xezim import XEZIM
from solouvm.backends.xrun import XRUN
from solouvm.errors import ConfigError

__all__ = [
    "BACKENDS",
    "EXTRA_SIM_KEYS",
    "REQUIRED_FIELDS",
    "Backend",
    "fake_sim_source",
    "get_backend",
    "resolve_sim_backend",
]

# 加后端就是加一项，不用改任何 if/elif
BACKENDS: dict[str, Backend] = {b.name: b for b in (VCS, XRUN, QUESTA, XEZIM, VERILATOR, FAKE)}

EXTRA_SIM_KEYS: frozenset[str] = frozenset({"cov_merge", "cov_report", "wave_export"})
_REGEX_LIST_FIELDS = ("error_regex", "license_regex")


def get_backend(name: str, overrides: Mapping[str, object] | None = None) -> Backend:
    """Return backend ``name`` with ``sim`` overrides applied (built-in table never changes).

    Raises ``ConfigError(CFG_INVALID)`` for unknown keys, bad value types, or an unknown
    simulator whose overrides lack a required field.
    """
    return resolve_backend(name, BACKENDS, overrides)


def resolve_sim_backend(name: str, sim: Mapping[str, object] | None = None) -> Backend:
    """Resolve ``config.simulator`` + ``config.sim``, validating ``EXTRA_SIM_KEYS``."""
    if not sim:
        return get_backend(name)
    overrides: dict[str, object] = {}
    for key, value in sim.items():
        if key in EXTRA_SIM_KEYS:
            if value is None:
                continue
            if isinstance(value, str):
                if not value.strip():
                    raise ConfigError(f"sim.{key} cannot be empty", code="CFG_INVALID")
                continue
            if (
                isinstance(value, (list, tuple))
                and value
                and all(isinstance(v, str) for v in value)
            ):
                continue
            raise ConfigError(
                f"sim.{key} must be a non-empty string or list of strings, got {value!r}",
                code="CFG_INVALID",
            )
        if (
            isinstance(value, list)
            and key not in _REGEX_LIST_FIELDS
            and all(isinstance(v, str) for v in value)
        ):
            overrides[key] = shlex.join(value)
        else:
            overrides[key] = value
    return get_backend(name, overrides)
