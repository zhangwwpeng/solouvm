"""Tests for solouvm.notes.export and solouvm.notes.progress (M-08..M-10, M-20..M-22)."""

from __future__ import annotations

import json
import textwrap
from datetime import date
from pathlib import Path

from solouvm.collect import collect_cases
from solouvm.config import load_config
from solouvm.notes import GENERATED_MARKER, compute_progress, export_docs, format_progress


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _setup_project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    _write(
        root / "solouvm.conf.py",
        "CFG = {\n"
        '    "project": "axi_vip",\n'
        '    "simulator": "fake",\n'
        '    "top": "tb_top",\n'
        '    "doc": {"lang": "zh", "out": "docs/cases"},\n'
        "}\n",
    )
    _write(
        root / "case.py",
        textwrap.dedent(
            """\
            from solouvm import Bug, Case, Expect, Log, Note, Todo, Triage

            CASES = [
                Case(
                    name="burst",
                    owner="wp",
                    stage="ready",
                    uvm_test="axi_burst_test",
                    params={"NUM": 32, "FAST": True},
                    tags=("axi", "nightly"),
                    intent="verify AXI burst wrap",
                    method="randomized burst lengths",
                    spec_refs=["AXI4 A3.4"],
                    expect=[Expect(scoreboard="AXI_SCB", min=100)],
                    doc=\"\"\"
                    Verify AXI INCR/WRAP/FIXED bursts.
                    \"\"\",
                    notes=[
                        Log("2026-11-05", "later log"),
                        Bug(
                            id=1,
                            title="wrap addr bug",
                            status="open",
                            seed=4417,
                            found="2026-11-02",
                        ),
                        Bug(
                            id=2,
                            title="last beat bug",
                            status="fixed",
                            seed=12,
                            found="2026-11-01",
                            fixed="2026-11-03",
                        ),
                        Bug(
                            id=3,
                            title="verified reg",
                            status="verified",
                            seed=99,
                            found="2026-10-20",
                            fixed="2026-10-21",
                            verified="2026-10-22",
                        ),
                        Todo("overdue narrow", due="2026-11-10"),
                        Note("early note", date="2026-11-01"),
                        Triage(
                            task="run1/1",
                            verdict="design_bug",
                            confidence="high",
                            summary="wrap logic broken",
                            evidence=["log:12"],
                            judged_status={"burst__4417": "fail"},
                            doc="docs/debug/run1/1.md",
                            bugs=["burst#1"],
                        ),
                    ],
                ),
                Case(name="smoke", owner="alice", stage="signoff"),
                Case(name="planned_c", owner="alice", stage="planned"),
                Case(name="old_c", owner="bob", stage="deprecated"),
            ]
            """
        ),
    )
    rdir = root / ".solouvm" / "runs" / "20261118-100000-r1"
    rdir.mkdir(parents=True)
    _write(
        rdir / "plan.json",
        json.dumps(
            {
                "schema": 1,
                "run_id": "20261118-100000-r1",
                "created_at": "2026-11-18T10:00:00+08:00",
                "parent_run": None,
                "provider": "local",
                "selection": {},
                "builds": {"default": "1111"},
                "jobs": [],
            }
        ),
    )
    _write(
        rdir / "results.json",
        json.dumps(
            {
                "schema": 1,
                "run_id": "20261118-100000-r1",
                "judged_at": "2026-11-18T10:05:00+08:00",
                "partial": False,
                "summary": {"total": 4, "passed": 3},
                "jobs": [
                    {
                        "job_id": "burst__4417",
                        "case": "burst",
                        "seed": 4417,
                        "status": "pass",
                        "reason": "clean",
                    },
                    {
                        "job_id": "burst__12",
                        "case": "burst",
                        "seed": 12,
                        "status": "pass",
                        "reason": "clean",
                    },
                    {
                        "job_id": "burst__99",
                        "case": "burst",
                        "seed": 99,
                        "status": "fail",
                        "reason": "UVM_ERROR=1",
                    },
                    {
                        "job_id": "smoke__1",
                        "case": "smoke",
                        "seed": 1,
                        "status": "pass",
                        "reason": "clean",
                    },
                ],
                "signatures": [],
                "waivers": {"used": [], "unused": [], "expired": []},
                "hints": [],
            }
        ),
    )
    return root


def test_compute_progress_stages_owners_overdue_and_hints(tmp_path: Path):
    root = _setup_project(tmp_path)
    config = load_config(root / "solouvm.conf.py")
    cases = collect_cases(config)
    today = date(2026, 11, 18)

    prog = compute_progress(config=config, cases=cases, today=today)
    assert prog["stages"] == {
        "planned": 1,
        "wip": 0,
        "ready": 1,
        "signoff": 1,
        "deprecated": 1,
    }
    assert prog["latest_run"] == {
        "run_id": "20261118-100000-r1",
        "passed": 3,
        "total": 4,
        "pass_rate": 75.0,
    }
    assert prog["by_owner"]["wp"] == {
        "open_bugs": 1,
        "fixed_bugs": 1,
        "open_todos": 1,
        "overdue_todos": 1,
    }
    assert len(prog["overdue_todos"]) == 1
    assert prog["overdue_todos"][0]["title"] == "overdue narrow"

    hints = prog["hints"]
    assert "burst#1 may be fixed (seed 4417 passed)" in hints
    assert "burst#2 can be marked verified (seed 12 passed)" in hints
    assert "regression? burst#3 was verified (seed 99 fail)" in hints

    rendered = format_progress(prog)
    assert "3/4 passed (75.0%)" in rendered
    assert "OWNER" in rendered
    assert "overdue narrow" in rendered
    assert "burst#2 can be marked verified" in rendered


def test_export_docs_languages_determinism_and_stale_cleanup(tmp_path: Path):
    root = _setup_project(tmp_path)
    config = load_config(root / "solouvm.conf.py")
    out_dir = root / "docs" / "cases"
    today = date(2026, 11, 18)

    # Create a stale solouvm-generated file AND a hand-written file
    stale_gen = _write(out_dir / "deleted_case.md", f"{GENERATED_MARKER}\n# deleted_case\n")
    hand_written = _write(out_dir / "README.md", "# Hand-written notes\nKeep me!\n")

    # Export in Chinese (config default)
    res_zh_1 = export_docs(config=config, out_dir=out_dir, with_results=True, today=today)
    assert res_zh_1["lang"] == "zh"
    assert not stale_gen.exists()
    assert str(stale_gen) in res_zh_1["removed"]
    assert hand_written.exists()
    assert hand_written.read_text(encoding="utf-8") == "# Hand-written notes\nKeep me!\n"

    index_zh = (out_dir / "index.md").read_text(encoding="utf-8")
    burst_zh = (out_dir / "burst.md").read_text(encoding="utf-8")
    assert index_zh.startswith(GENERATED_MARKER)
    assert burst_zh.startswith(GENERATED_MARKER)
    assert "进度总览" in index_zh
    assert "未解决 Bug 汇总" in index_zh
    assert "burst#1" in index_zh
    assert "20261118-100000-r1" in index_zh and "20261118-100000-r1" in burst_zh

    # Chronological order: 2026-11-01 (early note) < 2026-11-02 (bug#1) < 2026-11-05 (later log)
    assert (
        burst_zh.index("early note") < burst_zh.index("wrap addr bug") < burst_zh.index("later log")
    )

    # Determinism: byte-identical second export
    res_zh_2 = export_docs(config=config, out_dir=out_dir, with_results=True, today=today)
    assert res_zh_2["removed"] == []
    assert (out_dir / "index.md").read_bytes() == index_zh.encode("utf-8")
    assert (out_dir / "burst.md").read_bytes() == burst_zh.encode("utf-8")

    # Export in English without results
    en_dir = root / "docs" / "en"
    res_en = export_docs(
        config=config,
        out_dir=en_dir,
        lang="en",
        with_results=False,
        today=today,
    )
    assert res_en["lang"] == "en"
    index_en = (en_dir / "index.md").read_text(encoding="utf-8")
    burst_en = (en_dir / "burst.md").read_text(encoding="utf-8")
    assert "Progress Overview" in index_en
    assert "Unresolved Bugs" in index_en
    assert "20261118-100000-r1" not in index_en
    assert "20261118-100000-r1" not in burst_en
    assert "Verify AXI INCR/WRAP/FIXED bursts." in burst_en


def test_progress_no_runs(tmp_path: Path):
    root = tmp_path / "proj"
    _write(
        root / "solouvm.conf.py", 'CFG = {"project": "empty", "simulator": "fake", "top": "tb"}\n'
    )
    _write(root / "case.py", 'from solouvm import Case\nCASES = [Case(name="a")]\n')
    config = load_config(root / "solouvm.conf.py")
    prog = compute_progress(config=config, today=date(2026, 11, 18))
    assert prog["latest_run"] is None
    assert prog["hints"] == []
    assert "Latest run: none" in format_progress(prog)
