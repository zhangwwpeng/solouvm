"""Regression planning: selection, expansion, sharding and ``plan.json`` (formats.md §4)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import random
import tempfile
from collections.abc import Callable, Sequence
from datetime import datetime
from difflib import get_close_matches
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

from solouvm.config.schema import Config
from solouvm.errors import ConfigError
from solouvm.model import Case, Job, Plan, Stage
from solouvm.seeds import parse_seeds
from solouvm.units import parse_duration

DEFAULT_STAGES = ("wip", "ready", "signoff")
SUPPORTED_PLAN_SCHEMA = 1

_SELECTION_ORDER = ("case", "tag", "exclude_tag", "build", "stage", "shard")

__all__ = [
    "DEFAULT_STAGES",
    "SUPPORTED_PLAN_SCHEMA",
    "build_plan",
    "job_id",
    "parse_shard",
    "read_plan",
    "select_cases",
    "write_plan",
]


def job_id(case_name: str, seed: int) -> str:
    """Return the canonical job identifier ``<case>__<seed>``."""
    return f"{case_name}__{seed}"


def parse_shard(shard: str) -> tuple[int, int]:
    """Parse ``I/N`` (1-based ``1 <= I <= N``); raise ``CFG_INVALID`` on malformed input."""
    if not isinstance(shard, str):
        raise ConfigError(f"invalid shard {shard!r}: expected 'I/N'", code="CFG_INVALID")
    left, sep, right = shard.strip().partition("/")
    if not sep or "/" in right:
        raise ConfigError(f"invalid shard {shard!r}: expected 'I/N'", code="CFG_INVALID")
    left, right = left.strip(), right.strip()
    if not (left.isascii() and left.isdecimal() and right.isascii() and right.isdecimal()):
        raise ConfigError(
            f"invalid shard {shard!r}: expected positive integers 'I/N'",
            code="CFG_INVALID",
        )
    index, total = int(left), int(right)
    if total < 1 or not (1 <= index <= total):
        raise ConfigError(
            f"invalid shard {shard!r}: expected 1 <= I <= N with N >= 1",
            code="CFG_INVALID",
        )
    return index, total


def _normalize_patterns(value: object, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
        return list(value)
    raise ConfigError(
        f"selection {name!r} must be a sequence of strings, got {value!r}",
        code="CFG_INVALID",
    )


def _normalize_stages(stage: object) -> list[str] | None:
    if stage is None:
        return None
    if isinstance(stage, Stage):
        return [stage.value]
    if isinstance(stage, str):
        raw = [part.strip() for part in stage.split(",") if part.strip()]
    elif isinstance(stage, (list, tuple)):
        raw = []
        for item in stage:
            if isinstance(item, Stage):
                raw.append(item.value)
            elif isinstance(item, str):
                raw.append(item)
            else:
                raise ConfigError(
                    f"selection 'stage' items must be strings, got {item!r}",
                    code="CFG_INVALID",
                )
    else:
        raise ConfigError(
            f"selection 'stage' must be a sequence of strings or None, got {stage!r}",
            code="CFG_INVALID",
        )
    allowed = ", ".join(s.value for s in Stage)
    normalized: list[str] = []
    for item in raw:
        try:
            normalized.append(Stage(item).value)
        except ValueError:
            raise ConfigError(
                f"invalid stage {item!r}; allowed: {allowed}",
                code="CFG_INVALID",
            ) from None
    return normalized


def select_cases(
    cases: Sequence[Case],
    *,
    case: Sequence[str] | str = (),
    tag: Sequence[str] | str = (),
    exclude_tag: Sequence[str] | str = (),
    stage: Sequence[str] | str | None = None,
    build: str | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> list[Case]:
    """Filter ``cases`` in cli.md §1.3 order: stage -> case -> tag -> exclude_tag -> build."""
    case_pats = _normalize_patterns(case, "case")
    tag_list = _normalize_patterns(tag, "tag")
    exclude_list = _normalize_patterns(exclude_tag, "exclude_tag")
    stage_list = _normalize_stages(stage)
    if build is not None and not isinstance(build, str):
        raise ConfigError(
            f"selection 'build' must be a str or None, got {build!r}",
            code="CFG_INVALID",
        )

    if stage_list is not None:
        if Stage.PLANNED.value in stage_list:
            planned = [c.name for c in cases if c.stage == Stage.PLANNED.value]
            if planned:
                raise ConfigError(
                    f"case(s) not implemented (stage='planned'): {', '.join(planned)}",
                    code="CASE_PLANNED",
                )
        allowed_stages = set(stage_list)
        picked = [c for c in cases if c.stage in allowed_stages]
    elif case_pats:
        # When --case is given without --stage, allow matching planned/deprecated cases so
        # planned fails with CASE_PLANNED and deprecated runs with a warning (config.md §5.1).
        picked = list(cases)
    else:
        allowed_stages = set(DEFAULT_STAGES)
        picked = [c for c in cases if c.stage in allowed_stages]

    if case_pats:
        picked = [c for c in picked if any(fnmatch(c.name, pat) for pat in case_pats)]
    if tag_list:
        tag_set = set(tag_list)
        picked = [c for c in picked if any(t in tag_set for t in c.tags)]
    if exclude_list:
        exclude_set = set(exclude_list)
        picked = [c for c in picked if not any(t in exclude_set for t in c.tags)]
    if build is not None:
        picked = [c for c in picked if c.build == build]

    planned_picked = [c.name for c in picked if c.stage == Stage.PLANNED.value]
    if planned_picked:
        raise ConfigError(
            f"case(s) not implemented (stage='planned'): {', '.join(planned_picked)}",
            code="CASE_PLANNED",
        )
    if on_warning is not None:
        for selected in picked:
            if selected.stage == Stage.DEPRECATED.value:
                on_warning(f"case {selected.name!r} is deprecated")
    return picked


def _empty_selection_error(
    *,
    cases: Sequence[Case],
    case_pats: list[str],
    tag_list: list[str],
    shard: str | None,
    had_picked: bool,
) -> ConfigError:
    if had_picked and shard is not None:
        return ConfigError(
            f"shard {shard!r} selected 0 jobs",
            code="SELECT_EMPTY",
            hint="use a smaller shard count or pass --allow-empty",
        )
    all_names = [c.name for c in cases]
    all_tags = sorted({t for c in cases for t in c.tags})
    if case_pats:
        suggestions: list[str] = []
        for pat in case_pats:
            for match in get_close_matches(pat, all_names, n=2, cutoff=0.4):
                if match not in suggestions:
                    suggestions.append(match)
        msg = f"no cases matched selection (case={', '.join(case_pats)})"
        if suggestions:
            msg += f"; did you mean {', '.join(repr(s) for s in suggestions)}?"
            hint = f"closest case(s): {', '.join(suggestions)}"
        elif all_names:
            hint = f"available cases: {', '.join(all_names[:10])}"
        else:
            hint = "no cases defined; pass --allow-empty if intentional"
        return ConfigError(msg, code="SELECT_EMPTY", hint=hint)

    if tag_list:
        suggestions = []
        for t in tag_list:
            for match in get_close_matches(t, all_tags, n=2, cutoff=0.4):
                if match not in suggestions:
                    suggestions.append(match)
        msg = f"no cases matched selection (tag={', '.join(tag_list)})"
        if suggestions:
            msg += f"; did you mean {', '.join(repr(s) for s in suggestions)}?"
        hint = (
            f"known tags: {', '.join(all_tags)}"
            if all_tags
            else "no tags defined on cases; pass --allow-empty if intentional"
        )
        return ConfigError(msg, code="SELECT_EMPTY", hint=hint)

    hint = (
        f"known tags: {', '.join(all_tags)}"
        if all_tags
        else "check --stage / --build filters or pass --allow-empty"
    )
    return ConfigError("no jobs matched selection", code="SELECT_EMPTY", hint=hint)


def _resolve_cli_seeds(selection: dict[str, object]) -> object:
    if selection.get("seeds") is not None:
        return selection["seeds"]
    if selection.get("seed") is not None:
        raw_seed = selection["seed"]
        if isinstance(raw_seed, int) and not isinstance(raw_seed, bool):
            return [raw_seed]
        return raw_seed
    return None


def _case_rng(seed_key: str, case_name: str) -> random.Random:
    digest = hashlib.sha256(f"solouvm:seeds:{seed_key}:{case_name}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def build_plan(
    *,
    cases: list[Case],
    config: Config,
    selection: dict[str, object] | None = None,
    run_id: str,
    parent_run: str | None = None,
    builds: dict[str, str] | None = None,
    durations: dict[str, float] | None = None,
    allow_empty: bool = False,
    force: bool = False,
    order: str = "name",
    shard: str | None = None,
    on_warning: Callable[[str], None] | None = None,
    rng: random.Random | None = None,
    rng_seed: str | int | None = None,
) -> Plan:
    """Select cases, expand seeds into deterministic ``Job``s, sort, shard, and enforce limits."""
    if order not in ("name", "duration"):
        raise ConfigError(
            f"invalid job order {order!r}; expected 'name' or 'duration'",
            code="CFG_INVALID",
        )

    sel = dict(selection or {})
    case_pats = _normalize_patterns(sel.get("case"), "case")
    tag_list = _normalize_patterns(sel.get("tag"), "tag")
    exclude_list = _normalize_patterns(sel.get("exclude_tag"), "exclude_tag")
    stage_list = _normalize_stages(sel.get("stage"))
    build_filter = sel.get("build")
    if build_filter is not None and not isinstance(build_filter, str):
        raise ConfigError(
            f"selection 'build' must be a str or None, got {build_filter!r}",
            code="CFG_INVALID",
        )
    effective_shard = shard if shard is not None else sel.get("shard")
    if effective_shard is not None and not isinstance(effective_shard, str):
        raise ConfigError(
            f"selection 'shard' must be a str or None, got {effective_shard!r}",
            code="CFG_INVALID",
        )
    shard_spec = parse_shard(effective_shard) if effective_shard is not None else None
    cli_seeds = _resolve_cli_seeds(sel)

    stored_selection: dict[str, object] = {
        "case": case_pats,
        "tag": tag_list,
        "exclude_tag": exclude_list,
        "build": build_filter,
        "stage": stage_list if stage_list is not None else list(DEFAULT_STAGES),
        "shard": effective_shard,
    }
    if sel.get("seeds") is not None:
        stored_selection["seeds"] = sel["seeds"]
    if sel.get("seed") is not None:
        stored_selection["seed"] = sel["seed"]

    picked = select_cases(
        cases,
        case=case_pats,
        tag=tag_list,
        exclude_tag=exclude_list,
        stage=stage_list,
        build=build_filter,
        on_warning=on_warning,
    )

    explicit_rng_seed = rng_seed is not None or bool(
        os.environ.get("SOLOUVM_SHARD_SEED", "").strip()
    )
    seed_key = (
        str(rng_seed)
        if rng_seed is not None
        else (os.environ.get("SOLOUVM_SHARD_SEED", "").strip() or run_id)
    )
    warned_random_shard = False
    seen_cases: set[str] = set()
    jobs: list[Job] = []
    for selected in picked:
        if selected.name in seen_cases:
            raise ConfigError(f"duplicate case name {selected.name!r}", code="CASE_INVALID")
        seen_cases.add(selected.name)
        if selected.build not in config.builds:
            known = ", ".join(sorted(config.builds))
            raise ConfigError(
                f"case {selected.name!r}: unknown build mode {selected.build!r}; "
                f"defined builds: {known}",
                code="CASE_INVALID",
            )
        seed_spec = (
            selected.seeds
            if selected.seeds is not None
            else (cli_seeds if cli_seeds is not None else config.regress.seeds)
        )
        if (
            shard_spec is not None
            and not explicit_rng_seed
            and rng is None
            and isinstance(seed_spec, str)
            and seed_spec.strip().startswith("random:")
            and not warned_random_shard
            and on_warning is not None
        ):
            warned_random_shard = True
            on_warning(
                "using --shard with 'random:N' seeds without --shard-seed or "
                "SOLOUVM_SHARD_SEED; pass --shard-seed KEY so all shards expand identical seeds"
            )
        case_rng = rng if rng is not None else _case_rng(seed_key, selected.name)
        try:
            seeds = parse_seeds(seed_spec, rng=case_rng)  # type: ignore[arg-type]
        except ValueError as exc:
            raise ConfigError(
                f"invalid seed specification {seed_spec!r}: {exc}",
                code="CFG_INVALID",
            ) from None
        timeout_s = parse_duration(
            selected.timeout if selected.timeout is not None else config.run.timeout
        )
        hang_spec = selected.hang if selected.hang is not None else config.run.hang
        hang_s = parse_duration(hang_spec) if hang_spec is not None else None
        retry = selected.retry if selected.retry is not None else config.regress.retry
        uvm_test = selected.uvm_test if selected.uvm_test is not None else config.uvm_test
        merged_env = {**config.run.env, **selected.env}
        merged_opts = [*config.run.opts, *config.builds[selected.build].run_opts, *selected.opts]
        for seed in seeds:
            jid = job_id(selected.name, seed)
            jobs.append(
                Job(
                    job_id=jid,
                    case=selected.name,
                    seed=seed,
                    build=selected.build,
                    uvm_test=uvm_test,
                    params=dict(selected.params),
                    opts=list(merged_opts),
                    env=dict(merged_env),
                    timeout_s=timeout_s,
                    hang_s=hang_s,
                    retry=retry,
                    expect_fail=selected.expect_fail,
                    tags=tuple(selected.tags),
                    dir=f".solouvm/runs/{run_id}/{jid}",
                )
            )

    if order == "duration":
        dur_map = durations or {}
        jobs.sort(key=lambda j: (-float(dur_map.get(j.case, 0.0)), j.case, j.seed))
    else:
        jobs.sort(key=lambda j: (j.case, j.seed))

    if shard_spec is not None:
        index, total = shard_spec
        jobs = [j for idx, j in enumerate(jobs) if idx % total == index - 1]

    created_at = datetime.now().astimezone().replace(microsecond=0)
    if not jobs:
        if allow_empty:
            return Plan(
                run_id=run_id,
                created_at=created_at,
                parent_run=parent_run,
                provider=config.provider,
                selection=stored_selection,
                builds={},
                jobs=[],
            )
        raise _empty_selection_error(
            cases=cases,
            case_pats=case_pats,
            tag_list=tag_list,
            shard=effective_shard,
            had_picked=bool(picked),
        )

    if not force and len(jobs) > config.limits.max_jobs:
        raise ConfigError(
            f"{len(jobs)} jobs exceed limits.max_jobs ({config.limits.max_jobs})",
            code="LIMIT_EXCEEDED",
            hint="pass --force or narrow selection",
        )

    plan_builds = {
        mode: (builds or {}).get(mode, mode) for mode in dict.fromkeys(j.build for j in jobs)
    }
    return Plan(
        run_id=run_id,
        created_at=created_at,
        parent_run=parent_run,
        provider=config.provider,
        selection=stored_selection,
        builds=plan_builds,
        jobs=jobs,
    )


def _ordered_selection(selection: dict[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key in _SELECTION_ORDER:
        if key in selection:
            out[key] = selection[key]
    for key in sorted(selection):
        if key not in out:
            out[key] = selection[key]
    return out


def _job_to_dict(job: Job) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "case": job.case,
        "seed": job.seed,
        "build": job.build,
        "uvm_test": job.uvm_test,
        "params": {k: job.params[k] for k in sorted(job.params)},
        "opts": list(job.opts),
        "env": {k: job.env[k] for k in sorted(job.env)},
        "timeout_s": job.timeout_s,
        "hang_s": job.hang_s,
        "retry": job.retry,
        "expect_fail": job.expect_fail,
        "tags": list(job.tags),
        "dir": job.dir,
    }


def _plan_to_dict(plan: Plan) -> dict[str, object]:
    stamp = plan.created_at
    if stamp.tzinfo is None:
        stamp = stamp.astimezone()
    return {
        "schema": SUPPORTED_PLAN_SCHEMA,
        "run_id": plan.run_id,
        "created_at": stamp.isoformat(timespec="seconds"),
        "parent_run": plan.parent_run,
        "provider": plan.provider,
        "selection": _ordered_selection(plan.selection),
        "builds": {k: plan.builds[k] for k in sorted(plan.builds)},
        "jobs": [_job_to_dict(job) for job in plan.jobs],
    }


def write_plan(path: Path, plan: Plan) -> None:
    """Serialize ``plan`` deterministically to ``plan.json`` via an atomic replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_plan_to_dict(plan), indent=2, ensure_ascii=False) + "\n"
    data = text.encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _require_int(data: dict[str, object], key: str, path: Path) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError(
            f"plan field {key!r} must be an int, got {value!r}",
            code="CFG_INVALID",
            file=path,
        )
    return value


def _require_str(data: dict[str, object], key: str, path: Path) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ConfigError(
            f"plan field {key!r} must be a str, got {value!r}",
            code="CFG_INVALID",
            file=path,
        )
    return value


def _parse_timestamp(raw: object, path: Path) -> datetime:
    if not isinstance(raw, str):
        raise ConfigError(
            f"plan field 'created_at' must be an ISO 8601 string, got {raw!r}",
            code="CFG_INVALID",
            file=path,
        )
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        raise ConfigError(
            f"plan field 'created_at' has invalid timestamp {raw!r}",
            code="CFG_INVALID",
            file=path,
        ) from None
    if stamp.tzinfo is None:
        raise ConfigError(
            f"plan field 'created_at' {raw!r} has no timezone offset",
            code="CFG_INVALID",
            file=path,
        )
    return stamp


def _job_str_list(raw: dict[str, object], key: str, path: Path) -> list[str]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(
            f"job field {key!r} must be a list[str], got {value!r}",
            code="CFG_INVALID",
            file=path,
        )
    return list(value)


def _validate_safe_segment(value: str, key: str, path: Path) -> str:
    if (
        not value
        or value in (".", "..")
        or "/" in value
        or "\\" in value
        or "\n" in value
        or "\r" in value
        or "\x00" in value
    ):
        raise ConfigError(
            f"plan field {key!r} contains invalid path segment {value!r}",
            code="CFG_INVALID",
            file=path,
        )
    return value


def _job_number(
    raw: dict[str, object], key: str, path: Path, *, allow_none: bool = False
) -> float | None:
    value = raw.get(key)
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        expected = "number or None" if allow_none else "number"
        raise ConfigError(
            f"job field {key!r} must be a {expected}, got {value!r}",
            code="CFG_INVALID",
            file=path,
        )
    try:
        num = float(value)
    except OverflowError:
        num = math.inf
    if not math.isfinite(num) or num < 0:
        raise ConfigError(
            f"job field {key!r} must be a finite, non-negative number, got {value!r}",
            code="CFG_INVALID",
            file=path,
        )
    return abs(num)


def _parse_job(raw: object, path: Path, *, run_id: str) -> Job:
    if not isinstance(raw, dict):
        raise ConfigError("plan job entry must be a JSON object", code="CFG_INVALID", file=path)
    parsed_job_id = _validate_safe_segment(_require_str(raw, "job_id", path), "job_id", path)
    uvm_test = raw.get("uvm_test")
    if uvm_test is not None and not isinstance(uvm_test, str):
        raise ConfigError(
            f"job field 'uvm_test' must be a str or None, got {uvm_test!r}",
            code="CFG_INVALID",
            file=path,
        )
    params = raw.get("params")
    if not isinstance(params, dict):
        raise ConfigError(
            f"job field 'params' must be a dict, got {params!r}",
            code="CFG_INVALID",
            file=path,
        )
    env = raw.get("env")
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise ConfigError(
            f"job field 'env' must be a dict[str, str], got {env!r}",
            code="CFG_INVALID",
            file=path,
        )
    expect_fail = raw.get("expect_fail", False)
    if not isinstance(expect_fail, (bool, str)):
        raise ConfigError(
            f"job field 'expect_fail' must be bool or str, got {expect_fail!r}",
            code="CFG_INVALID",
            file=path,
        )
    timeout_s = _job_number(raw, "timeout_s", path)
    assert timeout_s is not None
    retry = _require_int(raw, "retry", path)
    if retry < 0:
        raise ConfigError(
            f"job field 'retry' must be >= 0, got {retry!r}",
            code="CFG_INVALID",
            file=path,
        )
    job_dir = _require_str(raw, "dir", path)
    if not job_dir or "\\" in job_dir or "\n" in job_dir or "\r" in job_dir or "\x00" in job_dir:
        raise ConfigError(
            f"job field 'dir' contains invalid path {job_dir!r}",
            code="CFG_INVALID",
            file=path,
        )
    dir_path = PurePosixPath(job_dir)
    if (
        dir_path.is_absolute()
        or ".." in dir_path.parts
        or dir_path.parts != (".solouvm", "runs", run_id, parsed_job_id)
    ):
        raise ConfigError(
            f"job field 'dir' must be '.solouvm/runs/{run_id}/{parsed_job_id}', got {job_dir!r}",
            code="CFG_INVALID",
            file=path,
        )
    return Job(
        job_id=parsed_job_id,
        case=_require_str(raw, "case", path),
        seed=_require_int(raw, "seed", path),
        build=_require_str(raw, "build", path),
        uvm_test=uvm_test,
        params=dict(params),
        opts=_job_str_list(raw, "opts", path),
        env=dict(env),
        timeout_s=timeout_s,
        hang_s=_job_number(raw, "hang_s", path, allow_none=True),
        retry=retry,
        expect_fail=expect_fail,
        tags=tuple(_job_str_list(raw, "tags", path)),
        dir=job_dir,
    )


def read_plan(path: Path) -> Plan:
    """Read and validate ``plan.json``; check ``schema == 1`` per N-08."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            f"cannot read plan file {path}: {exc}",
            code="CFG_LOAD_FAILED",
            file=path,
        ) from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid JSON in plan file {path}: {exc}",
            code="CFG_LOAD_FAILED",
            file=path,
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError("plan file must contain a JSON object", code="CFG_INVALID", file=path)

    schema = data.get("schema")
    if type(schema) is not int or schema != SUPPORTED_PLAN_SCHEMA:
        raise ConfigError(
            f"unsupported plan schema {schema!r} (expected {SUPPORTED_PLAN_SCHEMA}); regenerate",
            code="CFG_INVALID",
            file=path,
            hint="re-run 'solouvm gen' to regenerate plan.json",
        )

    parent_run = data.get("parent_run")
    if parent_run is not None and not isinstance(parent_run, str):
        raise ConfigError(
            f"plan field 'parent_run' must be a str or None, got {parent_run!r}",
            code="CFG_INVALID",
            file=path,
        )
    selection = data.get("selection")
    if not isinstance(selection, dict):
        raise ConfigError(
            f"plan field 'selection' must be a dict, got {selection!r}",
            code="CFG_INVALID",
            file=path,
        )
    builds = data.get("builds")
    if not isinstance(builds, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in builds.items()
    ):
        raise ConfigError(
            f"plan field 'builds' must be a dict[str, str], got {builds!r}",
            code="CFG_INVALID",
            file=path,
        )
    raw_jobs = data.get("jobs")
    if not isinstance(raw_jobs, list):
        raise ConfigError(
            f"plan field 'jobs' must be a list, got {raw_jobs!r}",
            code="CFG_INVALID",
            file=path,
        )
    run_id = _validate_safe_segment(_require_str(data, "run_id", path), "run_id", path)
    return Plan(
        run_id=run_id,
        created_at=_parse_timestamp(data.get("created_at"), path),
        parent_run=parent_run,
        provider=_require_str(data, "provider", path),
        selection=dict(selection),
        builds=dict(builds),
        jobs=[_parse_job(item, path, run_id=run_id) for item in raw_jobs],
    )
