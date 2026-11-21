"""Simulator backend definition (config.md §2.7, verdict.md §4.2).

后端只产出字符串：命令模板、错误模式；不碰 provider，也不执行任何东西。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, fields, replace

from solouvm.errors import ConfigError


@dataclass(frozen=True)
class Backend:
    """Snapshot of one simulator backend; frozen so the built-in table can never be mutated."""

    name: str
    build: str
    run: str
    simv: str
    seed: str
    wave: str
    cov_build: str
    cov_run: str
    compile_only: str
    error_regex: tuple[str, ...]
    license_regex: tuple[str, ...]
    time_regex: str | None
    version: str | None


# 自定义后端（name 不在内置表里）必须给出的字段
REQUIRED_FIELDS: tuple[str, ...] = ("build", "run", "simv", "seed", "wave", "compile_only")

_REGEX_LIST_FIELDS = frozenset({"error_regex", "license_regex"})
_OPTIONAL_FIELDS = frozenset({"time_regex", "version"})
# 自定义后端可省略、取空值的字段
_DEFAULTS: dict[str, object] = {
    "cov_build": "",
    "cov_run": "",
    "error_regex": (),
    "license_regex": (),
    "time_regex": None,
    "version": None,
}


def override_keys() -> frozenset[str]:
    """Field names that ``sim`` overrides may set (everything except ``name``)."""
    return frozenset(f.name for f in fields(Backend)) - {"name"}


def _check_regex(key: str, pattern: str) -> None:
    try:
        re.compile(pattern)
    except re.error as exc:
        raise ConfigError(f"sim.{key}: invalid regular expression {pattern!r}: {exc}") from None


def _normalize_value(key: str, value: object) -> object:
    if key in _REGEX_LIST_FIELDS:
        if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"sim.{key} must be a list of strings, got {value!r}")
        for pattern in value:
            _check_regex(key, pattern)
        return tuple(value)
    if value is None and key in _OPTIONAL_FIELDS:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"sim.{key} must be a string, got {value!r}")
    if key == "time_regex":
        _check_regex(key, value)
    return value


def normalize_overrides(overrides: Mapping[str, object]) -> dict[str, object]:
    """Validate ``sim`` overrides and convert them to ``Backend`` field values."""
    unknown = sorted(set(overrides) - override_keys())
    if unknown:
        raise ConfigError(
            f"unknown sim key(s): {', '.join(map(str, unknown))}",
            hint=f"valid keys: {', '.join(sorted(override_keys()))}",
        )
    return {key: _normalize_value(key, value) for key, value in overrides.items()}


def resolve_backend(
    name: str,
    builtins: Mapping[str, Backend],
    overrides: Mapping[str, object] | None = None,
) -> Backend:
    """Return the built-in backend ``name`` with ``overrides`` applied, or a custom backend."""
    values = normalize_overrides(overrides or {})
    if name in builtins:
        # replace 复制一份再改字段，内置表保持不变
        return replace(builtins[name], **values) if values else builtins[name]
    missing = [key for key in REQUIRED_FIELDS if key not in values]
    if missing:
        raise ConfigError(
            f"unknown simulator {name!r}: sim is missing required key(s): {', '.join(missing)}",
            hint=f"built-in simulators: {', '.join(sorted(builtins))}; "
            "a custom simulator must define all of: " + ", ".join(REQUIRED_FIELDS),
        )
    return Backend(name=name, **{**_DEFAULTS, **values})  # type: ignore[arg-type]
