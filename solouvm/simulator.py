"""Simulator backends: turning configuration into argv lists.

``solouvm`` never runs a shell. A command given as a string is split with
:func:`shlex.split`; a list is used verbatim, and each element is one argument.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Mapping
from typing import Any

from .config import Config
from .errors import ConfigError

#: Ready-made templates. Any other simulator name requires an explicit
#: ``CFG["sim"] = {"build": ..., "run": ...}``.
BUILTIN: dict[str, dict[str, Any]] = {
    "vcs": {
        "build": "vcs -full64 -sverilog -timescale=1ns/1ps -f {filelist} -o {simv}",
        "run": "./{simv}",
        "simv": "simv",
    },
    "xrun": {
        "build": "xrun -64bit -sv -f {filelist} -elaborate",
        "run": "xrun -64bit -R -xmlibdirpath .",
        "simv": "xrun",
    },
    "iverilog": {
        "build": "iverilog -g2012 -o {simv} -c {filelist}",
        "run": "vvp {simv}",
        "simv": "sim.vvp",
    },
    "echo": {
        # Test backend: prints what it would run. Useful for smoke checks.
        "build": "echo build {filelist}",
        "run": "echo run",
        "simv": "none",
    },
}


def backend(cfg: Config) -> dict[str, Any]:
    """Merge the builtin template for the configured simulator with user overrides."""
    name = cfg.simulator
    sim = cfg.sim or {}
    base = dict(BUILTIN.get(name, {}))
    if "build" not in base and "build" not in sim:
        raise ConfigError(
            f"unknown simulator {name!r} and no CFG['sim']['build'] given",
            hint="known: " + ", ".join(sorted(BUILTIN)),
        )
    if "run" not in base and "run" not in sim:
        raise ConfigError(
            f"unknown simulator {name!r} and no CFG['sim']['run'] given",
            hint="known: " + ", ".join(sorted(BUILTIN)),
        )
    base.update({str(k): v for k, v in sim.items()})
    return base


def simv_path(cfg: Config) -> str:
    sim = backend(cfg)
    return str(sim.get("simv") or "simv")


def build_argv(cfg: Config) -> list[str]:
    """Command that produces the simulator binary."""
    sim = backend(cfg)
    values = {
        "filelist": "filelist.f",
        "simv": simv_path(cfg),
        "root": str(cfg.root),
        "workdir": str(cfg.workdir),
        "top": cfg.top or "",
    }
    return _argv(sim["build"], values)


def run_argv(cfg: Config) -> list[str]:
    """Command template that starts one simulation (plusargs are appended later)."""
    sim = backend(cfg)
    values = {
        "filelist": "filelist.f",
        "simv": simv_path(cfg),
        "root": str(cfg.root),
        "workdir": str(cfg.workdir),
        "top": cfg.top or "",
    }
    return _argv(sim["run"], values)


def plusargs(spec: Mapping[str, Any], *, wave: bool = False) -> list[str]:
    """Build the ``+ARG=value`` tail injected into every run."""
    out = render_plusargs(dict(spec.get("params") or {}))
    out.append(f"+UVM_TESTNAME={spec['uvm_test']}" if spec.get("uvm_test") else None)
    out.extend(
        [
            f"+SOLOUVM_SEED={spec['seed']}",
            f"+SOLOUVM_TEST={spec['case']}",
            f"+SOLOUVM_CASE_ID={spec['case_id']}",
            f"+SOLOUVM_OUTDIR={spec['outdir']}",
        ]
    )
    if wave:
        out.append("+SOLOUVM_WAVE=1")
    return _compact(out)


def render_plusargs(params: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for key, value in params.items():
        if isinstance(value, bool):
            if value:
                out.append(f"+{key}")
            continue
        out.append(f"+{key}={value}")
    return out


def _argv(command: Any, values: Mapping[str, str]) -> list[str]:
    if isinstance(command, str):
        parts: Iterable[str] = shlex.split(command)
    elif isinstance(command, (list, tuple)):
        parts = [str(item) for item in command]
    else:
        raise ConfigError(f"sim command must be a str or list, got {command!r}")
    out: list[str] = []
    for part in parts:
        try:
            out.append(part.format_map(_SafeDict(values)))
        except (KeyError, IndexError, ValueError) as exc:
            raise ConfigError(f"cannot expand {part!r} in sim command: {exc}") from None
    if not out:
        raise ConfigError("sim command must not be empty")
    return out


class _SafeDict(dict):
    def __missing__(self, key: str) -> str:  # keep unknown placeholders literal
        return "{" + key + "}"


def _compact(items: Iterable[str | None]) -> list[str]:
    return [item for item in items if item]
