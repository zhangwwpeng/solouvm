"""The plan: one JSON file that describes exactly what this run will execute."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from . import simulator
from .config import Config
from .errors import InfraError
from .model import Case, CaseSpec, sort_specs
from .util import (
    new_run_id,
    now_iso,
    parse_duration,
    read_json,
    relpath,
    write_json_atomic,
)

SCHEMA = 1


def plan_path(cfg: Config) -> Path:
    return cfg.workdir / "plan.json"


def build(
    cfg: Config,
    cases: list[Case] | None = None,
    *,
    seeds: list[int] | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Expand ``case × seed`` into the immutable work list."""
    cases = cases or []
    seeds = cfg.seeds if seeds is None else seeds
    if not cases:
        raise InfraError("no cases to plan: case.py returned an empty list")
    if not seeds:
        raise InfraError("no seeds to plan: regress.seeds resolved to an empty list")

    defaults = cfg.case_defaults()
    specs: list[CaseSpec] = []
    for case in cases:
        case = case.with_defaults(defaults)
        timeout_s = parse_duration(case.timeout if case.timeout is not None else cfg.timeout)
        for seed in seeds:
            rel_out = Path(relpath(cfg.workdir, cfg.root)) / "out"
            outdir = f"{rel_out}/{_case_id(case.name, seed)}"
            specs.append(CaseSpec.create(case, seed, timeout_s=timeout_s, outdir=outdir))

    return {
        "schema": SCHEMA,
        "run_id": run_id or new_run_id(),
        "created_at": now_iso(),
        "project": cfg.project,
        "root": str(cfg.root),
        "simulator": cfg.simulator,
        "python": sys.executable,
        "workdir": relpath(cfg.workdir, cfg.root),
        "checks": dict(cfg.checks),
        "exec": {
            "sim_cmd": _exec_sim_cmd(cfg),
            "env": {},
            "default_timeout_s": parse_duration(cfg.timeout),
        },
        "cases": [spec.to_json() for spec in sort_specs(specs)],
    }


def write(cfg: Config, plan: dict[str, Any]) -> Path:
    path = plan_path(cfg)
    write_json_atomic(path, plan)
    return path


def load(path: Path) -> dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict) or "cases" not in payload:
        raise InfraError(f"{path} is not a solouvm plan (missing 'cases')")
    if payload.get("schema") != SCHEMA:
        raise InfraError(
            f"{path} has plan schema {payload.get('schema')!r}, expected {SCHEMA}",
            hint="regenerate it with `solouvm gen`",
        )
    return payload


def specs(plan: dict[str, Any]) -> list[CaseSpec]:
    return [CaseSpec.from_json(item) for item in plan["cases"]]


def select(plan: dict[str, Any], cases: list[str], *, seeds: list[int] | None = None) -> dict[str, Any]:
    """Return a shallow copy of ``plan`` restricted to the requested cases/seeds."""
    wanted = set(cases)
    seed_filter = set(seeds) if seeds else None
    kept = [
        item
        for item in plan["cases"]
        if item["case"] in wanted and (seed_filter is None or item["seed"] in seed_filter)
    ]
    if not kept:
        raise InfraError(f"no planned case matches {sorted(wanted)}")
    out = dict(plan)
    out["cases"] = kept
    return out


def _exec_sim_cmd(cfg: Config) -> list[str]:
    """Resolve the run command so it works from any ``outdir``."""
    cmd = simulator.run_argv(cfg)
    simv = simulator.simv_path(cfg)
    absolute = cfg.root / simv
    if absolute.exists():
        cmd = [str(absolute), *cmd[1:]]
    elif cmd[0].startswith("./"):
        cmd = [str(cfg.root / cmd[0][2:]), *cmd[1:]]
    return cmd


def _case_id(name: str, seed: int) -> str:
    from .util import case_id

    return case_id(name, seed)
