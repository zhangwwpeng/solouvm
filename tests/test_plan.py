"""Tests for solouvm.plan (task 12 + Wave 1A extensions)."""

from __future__ import annotations

import json
import random
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from solouvm import Case
from solouvm.config import Config, load_config
from solouvm.errors import ConfigError
from solouvm.model import Plan
from solouvm.plan import build_plan, job_id, parse_shard, read_plan, select_cases, write_plan


def _make_config(tmp_path: Path, extra: str = "") -> Config:
    root = tmp_path / "proj"
    root.mkdir(parents=True, exist_ok=True)
    body = textwrap.dedent(
        f"""
        CFG = {{
            "simulator": "fake",
            "top": "tb_top",
            "uvm_test": "default_uvm_test",
            "builds": {{
                "default": {{"run_opts": ["+DEFAULT_BUILD"]}},
                "gate": {{"run_opts": ["+GATE_OPT"]}},
            }},
            "run": {{
                "timeout": "20m",
                "hang": "5m",
                "opts": ["+GLOBAL_OPT"],
                "env": {{"G": "1", "SHARED": "cfg"}},
            }},
            "regress": {{
                "seeds": "10,11",
                "retry": 1,
            }},
            {extra}
        }}
        """
    )
    (root / "solouvm.conf.py").write_text(body, encoding="utf-8")
    return load_config(root / "solouvm.conf.py")


def _raises(code: str, func, *args, **kwargs) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        func(*args, **kwargs)
    assert info.value.code == code, str(info.value)
    return info.value


# ---------------------------------------------------------------- job_id & expansion


def test_job_id() -> None:
    assert job_id("burst", 4417) == "burst__4417"


def test_one_case_three_seeds(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cases = [Case(name="burst", seeds=[1, 2, 3])]
    plan = build_plan(cases=cases, config=cfg, run_id="r1")
    assert [j.job_id for j in plan.jobs] == ["burst__1", "burst__2", "burst__3"]


def test_two_cases_two_seeds_sorted_by_case_and_seed(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cases = [
        Case(name="zebra", seeds=[20, 5]),
        Case(name="alpha", seeds=[9, 2]),
    ]
    plan = build_plan(cases=cases, config=cfg, run_id="r1")
    assert [(j.case, j.seed) for j in plan.jobs] == [
        ("alpha", 2),
        ("alpha", 9),
        ("zebra", 5),
        ("zebra", 20),
    ]


def test_case_seeds_deduplicated(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cases = [Case(name="smoke", seeds=[1, 1, 2])]
    plan = build_plan(cases=cases, config=cfg, run_id="r1")
    assert [j.seed for j in plan.jobs] == [1, 2]


def test_seed_priority_case_over_cli_over_config(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cases = [
        Case(name="c_case", seeds=[100]),
        Case(name="c_default"),
    ]
    plan_cli = build_plan(cases=cases, config=cfg, selection={"seeds": "7,8"}, run_id="r1")
    assert [(j.case, j.seed) for j in plan_cli.jobs] == [
        ("c_case", 100),
        ("c_default", 7),
        ("c_default", 8),
    ]

    plan_single = build_plan(
        cases=[Case(name="c_default")],
        config=cfg,
        selection={"seed": 42},
        run_id="r2",
    )
    assert [j.seed for j in plan_single.jobs] == [42]

    plan_cfg = build_plan(cases=[Case(name="c_default")], config=cfg, run_id="r3")
    assert [j.seed for j in plan_cfg.jobs] == [10, 11]


def test_bare_int_case_seeds_raises_type_error(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    with pytest.raises(TypeError):
        build_plan(cases=[Case(name="bad", seeds=3)], config=cfg, run_id="r1")  # type: ignore[arg-type]


# ---------------------------------------------------------------- select_cases


def test_stage_default_and_explicit() -> None:
    cases = [
        Case(name="c_planned", stage="planned"),
        Case(name="c_wip", stage="wip"),
        Case(name="c_ready", stage="ready"),
        Case(name="c_signoff", stage="signoff"),
        Case(name="c_dep", stage="deprecated"),
    ]
    assert [c.name for c in select_cases(cases)] == ["c_wip", "c_ready", "c_signoff"]
    assert [c.name for c in select_cases(cases, stage=["ready"])] == ["c_ready"]
    assert [c.name for c in select_cases(cases, stage="ready,signoff")] == ["c_ready", "c_signoff"]


def test_case_glob_tag_exclude_tag_and_build() -> None:
    cases = [
        Case(name="smoke_a", tags=("nightly", "fast"), build="default"),
        Case(name="smoke_b", tags=("nightly", "slow"), build="gate"),
        Case(name="burst_c", tags=("axi",), build="gate"),
    ]
    assert [c.name for c in select_cases(cases, case=["smoke*"])] == ["smoke_a", "smoke_b"]
    assert [c.name for c in select_cases(cases, tag=["nightly"])] == ["smoke_a", "smoke_b"]
    assert [c.name for c in select_cases(cases, tag=["fast", "axi"])] == ["smoke_a", "burst_c"]
    assert [c.name for c in select_cases(cases, tag=["nightly"], exclude_tag=["slow"])] == [
        "smoke_a"
    ]
    assert [c.name for c in select_cases(cases, build="gate")] == ["smoke_b", "burst_c"]


def test_planned_raises_on_stage_or_case() -> None:
    cases = [
        Case(name="future", stage="planned"),
        Case(name="active", stage="wip"),
    ]
    err1 = _raises("CASE_PLANNED", select_cases, cases, stage=["planned"])
    assert "future" in err1.message
    err2 = _raises("CASE_PLANNED", select_cases, cases, case=["future"])
    assert "future" in err2.message
    # Explicit stage without planned skips planned without error
    assert [c.name for c in select_cases(cases, stage=["wip"], case=["*"])] == ["active"]


def test_deprecated_runs_with_warning() -> None:
    cases = [
        Case(name="old", stage="deprecated", tags=("legacy",)),
        Case(name="current", stage="ready", tags=("fast",)),
    ]
    warnings_stage: list[str] = []
    picked = select_cases(cases, stage=["deprecated"], on_warning=warnings_stage.append)
    assert [c.name for c in picked] == ["old"]
    assert len(warnings_stage) == 1 and "old" in warnings_stage[0]

    warnings_case: list[str] = []
    picked2 = select_cases(cases, case=["old"], on_warning=warnings_case.append)
    assert [c.name for c in picked2] == ["old"]
    assert len(warnings_case) == 1

    # Filtered-out deprecated case must not warn
    warnings_none: list[str] = []
    picked3 = select_cases(
        cases, stage=["deprecated"], exclude_tag=["legacy"], on_warning=warnings_none.append
    )
    assert picked3 == []
    assert warnings_none == []


# ---------------------------------------------------------------- empty, limits, builds, job fields


def test_empty_selection_suggests_close_case_and_allow_empty(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cases = [Case(name="burst", tags=("axi",)), Case(name="smoke", tags=("quick",))]

    err = _raises(
        "SELECT_EMPTY",
        build_plan,
        cases=cases,
        config=cfg,
        selection={"case": ["burts"]},
        run_id="r1",
    )
    assert "burts" in err.message
    assert "burst" in err.message

    empty_plan = build_plan(
        cases=cases,
        config=cfg,
        selection={"case": ["burts"]},
        run_id="r1",
        allow_empty=True,
    )
    assert empty_plan.jobs == []
    assert empty_plan.builds == {}


def test_max_jobs_limit_and_force(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path, extra='"limits": {"max_jobs": 3},')
    cases = [Case(name="burst", seeds=[1, 2, 3, 4])]

    err = _raises("LIMIT_EXCEEDED", build_plan, cases=cases, config=cfg, run_id="r1")
    assert "4" in err.message and "3" in err.message

    forced = build_plan(cases=cases, config=cfg, run_id="r1", force=True)
    assert len(forced.jobs) == 4


def test_plan_builds_matches_used_job_builds_and_merges_job_fields(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cases = [
        Case(
            name="burst",
            uvm_test="axi_burst_test",
            build="gate",
            seeds=[4417],
            params={"NUM": 32},
            opts=["+CASE_OPT"],
            env={"SHARED": "case", "C": "2"},
            timeout="1h",
            hang=None,
            retry=2,
            expect_fail="ERR_EXP",
            tags=("axi",),
        ),
        Case(name="unused_default", build="default", seeds=[1]),
    ]
    plan = build_plan(
        cases=cases,
        config=cfg,
        selection={"build": "gate"},
        builds={"default": "k_def", "gate": "k_gate"},
        run_id="r1",
    )
    assert plan.builds == {"gate": "k_gate"}
    assert set(plan.builds) == {j.build for j in plan.jobs}
    job = plan.jobs[0]
    assert job.uvm_test == "axi_burst_test"
    assert job.timeout_s == 3600.0
    assert job.hang_s == 300.0
    assert job.retry == 2
    assert job.expect_fail == "ERR_EXP"
    assert job.env == {"G": "1", "SHARED": "case", "C": "2"}
    assert job.opts == ["+GLOBAL_OPT", "+GATE_OPT", "+CASE_OPT"]
    assert job.dir == ".solouvm/runs/r1/burst__4417"


# ---------------------------------------------------------------- rng, duration order, shard


def test_rng_reproducible_and_random_seeds_round_trip(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cases = [Case(name="a", seeds="random:5"), Case(name="b", seeds="random:3")]
    p1 = build_plan(cases=cases, config=cfg, run_id="r1", rng=random.Random(0))
    p2 = build_plan(cases=cases, config=cfg, run_id="r1", rng=random.Random(0))
    assert p1.jobs == p2.jobs
    assert len([j for j in p1.jobs if j.case == "a"]) == 5

    plan_path = tmp_path / "plan.json"
    write_plan(plan_path, p1)
    loaded = read_plan(plan_path)
    assert [j.seed for j in loaded.jobs] == [j.seed for j in p1.jobs]
    assert loaded == p1


def test_duration_order(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cases = [
        Case(name="short", seeds=[1]),
        Case(name="long", seeds=[1]),
        Case(name="medium", seeds=[1]),
    ]
    plan = build_plan(
        cases=cases,
        config=cfg,
        run_id="r1",
        order="duration",
        durations={"short": 5.0, "long": 120.0, "medium": 30.0},
    )
    assert [j.case for j in plan.jobs] == ["long", "medium", "short"]
    _raises("CFG_INVALID", build_plan, cases=cases, config=cfg, run_id="r1", order="invalid")


def test_parse_shard_and_sharding_partition(tmp_path: Path) -> None:
    assert parse_shard("1/3") == (1, 3)
    assert parse_shard(" 3 / 3 ") == (3, 3)
    for bad in ("0/3", "4/3", "1/0", "a/b", "1/2/3", "-1/3", "1"):
        _raises("CFG_INVALID", parse_shard, bad)

    cfg = _make_config(tmp_path, extra='"limits": {"max_jobs": 3},')
    cases = [Case(name=f"c{i}", seeds=[1, 2]) for i in range(4)]  # 8 jobs total
    full = build_plan(cases=cases, config=cfg, run_id="r1", force=True)
    shards = [build_plan(cases=cases, config=cfg, run_id="r1", shard=f"{i}/3") for i in (1, 2, 3)]
    assert [len(s.jobs) for s in shards] == [3, 3, 2]
    combined = {j.job_id for s in shards for j in s.jobs}
    assert combined == {j.job_id for j in full.jobs}


# ---------------------------------------------------------------- plan.json format & schema checks


def test_write_and_read_plan_format_determinism_and_schema(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cases = [Case(name="burst", seeds=[4417], params={"Z": 1, "A": 2}, tags=("nightly", "axi"))]
    plan = build_plan(
        cases=cases,
        config=cfg,
        selection={"tag": ["nightly"]},
        builds={"default": "3c9e1f2a"},
        run_id="20261103-101500-a1b2",
        parent_run="20261102-090000-0000",
    )
    plan.created_at = datetime(2026, 11, 3, 10, 15, 0, tzinfo=timezone(timedelta(hours=8)))

    p1 = tmp_path / "p1" / "plan.json"
    p2 = tmp_path / "p2" / "plan.json"
    write_plan(p1, plan)
    write_plan(p2, plan)

    text1 = p1.read_text(encoding="utf-8")
    text2 = p2.read_text(encoding="utf-8")
    assert text1 == text2
    assert text1.endswith("\n")

    raw = json.loads(text1)
    assert list(raw.keys()) == [
        "schema",
        "run_id",
        "created_at",
        "parent_run",
        "provider",
        "selection",
        "builds",
        "jobs",
    ]
    assert raw["schema"] == 1
    assert raw["created_at"] == "2026-11-03T10:15:00+08:00"
    assert list(raw["jobs"][0].keys()) == [
        "job_id",
        "case",
        "seed",
        "build",
        "uvm_test",
        "params",
        "opts",
        "env",
        "timeout_s",
        "hang_s",
        "retry",
        "expect_fail",
        "tags",
        "dir",
    ]
    assert list(raw["jobs"][0]["params"].keys()) == ["A", "Z"]

    # Unknown optional fields are ignored on read (formats.md §1)
    raw["extra_top"] = "ok"
    raw["jobs"][0]["extra_job"] = 123
    p1.write_text(json.dumps(raw), encoding="utf-8")
    assert read_plan(p1) == plan

    # Unsupported schema raises CFG_INVALID
    raw["schema"] = 2
    p1.write_text(json.dumps(raw), encoding="utf-8")
    _raises("CFG_INVALID", read_plan, p1)

    raw["schema"] = True
    p1.write_text(json.dumps(raw), encoding="utf-8")
    _raises("CFG_INVALID", read_plan, p1)

    # Missing or corrupt file raises CFG_LOAD_FAILED
    _raises("CFG_LOAD_FAILED", read_plan, tmp_path / "missing.json")
    p1.write_text("{bad json", encoding="utf-8")
    _raises("CFG_LOAD_FAILED", read_plan, p1)


def test_plan_dataclass_type(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    plan = build_plan(cases=[Case(name="s", seeds=[1])], config=cfg, run_id="r1")
    assert isinstance(plan, Plan)


def test_random_seeds_shard_partition_deterministic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_config(tmp_path)
    cases = [Case(name="alpha", seeds="random:4"), Case(name="beta", seeds="random:3")]

    full = build_plan(cases=cases, config=cfg, run_id="shared-run")
    s1 = build_plan(cases=cases, config=cfg, run_id="shared-run", shard="1/2")
    s2 = build_plan(cases=cases, config=cfg, run_id="shared-run", shard="2/2")
    assert [j.job_id for j in s1.jobs] + [j.job_id for j in s2.jobs] != []
    assert len(s1.jobs) + len(s2.jobs) == len(full.jobs)
    assert set(j.job_id for j in s1.jobs).isdisjoint(j.job_id for j in s2.jobs)
    assert {j.job_id for j in s1.jobs} | {j.job_id for j in s2.jobs} == {
        j.job_id for j in full.jobs
    }

    # Explicit rng_seed or SOLOUVM_SHARD_SEED produces identical partition across different run_ids
    s1_env = build_plan(cases=cases, config=cfg, run_id="r-a", shard="1/2", rng_seed="ci-42")
    s2_env = build_plan(cases=cases, config=cfg, run_id="r-b", shard="2/2", rng_seed="ci-42")
    full_env = build_plan(cases=cases, config=cfg, run_id="r-c", rng_seed="ci-42")
    assert {j.job_id for j in s1_env.jobs} | {j.job_id for j in s2_env.jobs} == {
        j.job_id for j in full_env.jobs
    }

    monkeypatch.setenv("SOLOUVM_SHARD_SEED", "env-seed-99")
    e1 = build_plan(cases=cases, config=cfg, run_id="run-1", shard="1/2")
    e2 = build_plan(cases=cases, config=cfg, run_id="run-2", shard="2/2")
    efull = build_plan(cases=cases, config=cfg, run_id="run-3")
    assert {j.job_id for j in e1.jobs}.isdisjoint(j.job_id for j in e2.jobs)
    assert {j.job_id for j in e1.jobs} | {j.job_id for j in e2.jobs} == {
        j.job_id for j in efull.jobs
    }
    monkeypatch.delenv("SOLOUVM_SHARD_SEED")

    # Different run_ids without SOLOUVM_SHARD_SEED still generate distinct random seeds
    other = build_plan(cases=cases, config=cfg, run_id="different-run")
    assert [j.seed for j in other.jobs] != [j.seed for j in full.jobs]


def test_read_plan_rejects_bad_dir_run_id_retry_and_non_finite_numbers(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    cases = [Case(name="burst", seeds=[1])]
    plan = build_plan(cases=cases, config=cfg, run_id="r1")
    plan_path = tmp_path / "plan.json"
    write_plan(plan_path, plan)
    base_raw = json.loads(plan_path.read_text(encoding="utf-8"))

    # timeout_s / hang_s == 0 is valid per units.parse_duration
    zero_raw = json.loads(json.dumps(base_raw))
    zero_raw["jobs"][0]["timeout_s"] = 0
    zero_raw["jobs"][0]["hang_s"] = 0.0
    plan_path.write_text(json.dumps(zero_raw), encoding="utf-8")
    loaded_zero = read_plan(plan_path)
    assert loaded_zero.jobs[0].timeout_s == 0.0
    assert loaded_zero.jobs[0].hang_s == 0.0

    for bad_num in ("NaN", "Infinity", "-Infinity", "-5", "1e400"):
        txt = json.dumps(base_raw).replace("1200.0", bad_num, 1)
        plan_path.write_text(txt, encoding="utf-8")
        _raises("CFG_INVALID", read_plan, plan_path)

        txt_hang = json.dumps(base_raw).replace("300.0", bad_num, 1)
        plan_path.write_text(txt_hang, encoding="utf-8")
        _raises("CFG_INVALID", read_plan, plan_path)

    for bad_dir in ("../escape", "/etc/passwd", ".solouvm/runs/r2/burst__1", ".solouvm/runs/r1/x"):
        bad_raw = json.loads(json.dumps(base_raw))
        bad_raw["jobs"][0]["dir"] = bad_dir
        plan_path.write_text(json.dumps(bad_raw), encoding="utf-8")
        _raises("CFG_INVALID", read_plan, plan_path)

    for bad_rid in ("", "..", "a/b", "a\\b"):
        bad_raw = json.loads(json.dumps(base_raw))
        bad_raw["run_id"] = bad_rid
        plan_path.write_text(json.dumps(bad_raw), encoding="utf-8")
        _raises("CFG_INVALID", read_plan, plan_path)

    bad_retry = json.loads(json.dumps(base_raw))
    bad_retry["jobs"][0]["retry"] = -1
    plan_path.write_text(json.dumps(bad_retry), encoding="utf-8")
    _raises("CFG_INVALID", read_plan, plan_path)
