"""Tests for solouvm.report (formats.md §6, §8, K-01..K-12, Task 18)."""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import pytest

from solouvm.errors import ConfigError, InfraError
from solouvm.judge import write_results
from solouvm.model import Result
from solouvm.report import (
    CATEGORY_ORDER,
    STATUS_ORDER,
    compare_results,
    job_duration,
    read_results_json,
    render_html,
    render_report,
    results_to_payload,
    write_reports,
)

T0 = datetime(2026, 11, 18, 11, 2, 10, tzinfo=timezone.utc)


def _sample_results() -> list[Result]:
    return [
        Result(
            job_id="a_pass__1",
            case="a_pass",
            seed=1,
            status="pass",
            judged_status="pass",
            raw_status="pass",
            category=None,
            reason="clean",
            attempts=[
                {
                    "try": 1,
                    "status": "pass",
                    "reason": "clean",
                    "duration_s": 1.25,
                    "host": "box1",
                    "log": ".solouvm/runs/R1/a_pass__1/try1/sim.log",
                    "wave": False,
                }
            ],
            repro={
                "make": "make run/a_pass__1",
                "solouvm": "solouvm run --case a_pass --seed 1 --wave",
            },
        ),
        Result(
            job_id="b_xfail__2",
            case="b_xfail",
            seed=2,
            status="xfail",
            judged_status="xfail",
            raw_status="fail",
            category=None,
            reason="UVM_ERROR=1 > 0",
            signature={"id": "deadbeef", "text": "UVM_ERROR expected"},
            attempts=[
                {
                    "try": 1,
                    "status": "xfail",
                    "reason": "UVM_ERROR=1 > 0",
                    "duration_s": 2.0,
                    "host": "box1",
                    "log": ".solouvm/runs/R1/b_xfail__2/try1/sim.log",
                    "wave": False,
                }
            ],
        ),
        Result(
            job_id="c_skip__3",
            case="c_skip",
            seed=3,
            status="skip",
            judged_status="skip",
            raw_status="skip",
            category=None,
            reason="not run: job.facts missing",
        ),
        Result(
            job_id="d_fail1__4",
            case="d_fail1",
            seed=4,
            status="fail",
            judged_status="fail",
            raw_status="fail",
            category="design",
            reason="UVM_ERROR=1 > 0",
            first_error={"line": 42, "text": "UVM_ERROR scb.sv(42): mismatch <tag>"},
            signature={"id": "aaaa1111", "text": "UVM_ERROR scb.sv: mismatch <tag>"},
            known=["d_fail1#1"],
            attempts=[
                {
                    "try": 1,
                    "status": "fail",
                    "reason": "UVM_ERROR=1 > 0",
                    "duration_s": 5.5,
                    "host": "box1",
                    "log": ".solouvm/runs/R1/d_fail1__4/try1/sim.log",
                    "wave": False,
                }
            ],
            repro={
                "make": "make TRY=r1 run/d_fail1__4",
                "solouvm": "solouvm run --case d_fail1 --seed 4 --wave",
            },
        ),
        Result(
            job_id="e_fail2__5",
            case="e_fail2",
            seed=5,
            status="fail",
            judged_status="fail",
            raw_status="fail",
            category="design",
            reason="UVM_ERROR=1 > 0",
            first_error={"line": 10, "text": "UVM_ERROR scb.sv(10): mismatch"},
            signature={"id": "aaaa1111", "text": "UVM_ERROR scb.sv: mismatch <tag>"},
            attempts=[
                {
                    "try": 1,
                    "status": "fail",
                    "reason": "UVM_ERROR=1 > 0",
                    "duration_s": 3.0,
                    "host": "box1",
                    "log": ".solouvm/runs/R1/e_fail2__5/try1/sim.log",
                    "wave": False,
                }
            ],
        ),
        Result(
            job_id="f_timeout__6",
            case="f_timeout",
            seed=6,
            status="timeout",
            judged_status="timeout",
            raw_status="timeout",
            category="execution",
            reason="wall-clock timeout after 30m",
            signature={"id": "timeout|DRV", "text": "timeout|DRV"},
            attempts=[
                {
                    "try": 1,
                    "status": "timeout",
                    "reason": "wall-clock timeout after 30m",
                    "duration_s": 1800.0,
                    "host": "box1",
                    "log": ".solouvm/runs/R1/f_timeout__6/try1/sim.log",
                    "wave": False,
                }
            ],
        ),
        Result(
            job_id="g_false__7",
            case="g_false",
            seed=7,
            status="false_pass",
            judged_status="pass",
            raw_status="pass",
            category="design",
            reason="clean (中文注释)",
            triage={
                "task": "R1/2",
                "verdict": "false_pass",
                "confidence": "high",
                "review": "accepted",
                "stale": False,
                "bugs": [],
                "doc": "docs/debug/R1/2.md",
            },
            attempts=[
                {
                    "try": 1,
                    "status": "pass",
                    "reason": "clean",
                    "duration_s": 0.5,
                    "host": "box1",
                    "log": ".solouvm/runs/R1/g_false__7/try1/sim.log",
                    "wave": False,
                }
            ],
        ),
    ]


def test_write_results_json_determinism_key_order_and_junit(tmp_path: Path) -> None:
    results = _sample_results()
    dir1 = tmp_path / "out1"
    dir2 = tmp_path / "out2"

    write_results(dir1, results, run_id="R1", partial=False, judged_at=T0, project="axi_vip")
    write_results(dir2, results, run_id="R1", partial=False, judged_at=T0, project="axi_vip")

    raw1 = (dir1 / "results.json").read_bytes()
    raw2 = (dir2 / "results.json").read_bytes()
    assert raw1 == raw2
    assert raw1.endswith(b"\n") and not raw1.endswith(b"\n\n")
    text1 = raw1.decode("utf-8")
    assert "中文注释" in text1  # ensure_ascii=False

    data = json.loads(text1)
    assert list(data.keys()) == [
        "schema",
        "run_id",
        "judged_at",
        "partial",
        "summary",
        "jobs",
        "signatures",
        "waivers",
        "hints",
    ]
    assert list(data["summary"]["by_status"].keys()) == list(STATUS_ORDER)
    assert list(data["summary"]["by_category"].keys()) == list(CATEGORY_ORDER)
    assert data["summary"]["passed"] == 3  # pass + xfail + skip
    assert data["summary"]["total"] == 7
    assert data["summary"]["by_category"] == {"design": 3, "execution": 1, "environment": 0}
    # Signatures sorted by count desc (aaaa1111 has count=2, timeout|DRV has count=1)
    assert [s["id"] for s in data["signatures"]] == ["aaaa1111", "timeout|DRV"]
    assert data["signatures"][0]["cases"] == ["d_fail1", "e_fail2"]

    # JUnit XML parseable & accurate mappings
    junit_path = dir1 / "junit.xml"
    tree = ET.parse(junit_path)
    root = tree.getroot()
    assert root.tag == "testsuite"
    assert root.attrib["name"] == "axi_vip"
    cases = {tc.attrib["name"]: tc for tc in root.findall("testcase")}
    assert list(cases["a_pass__1"]) == []
    assert cases["b_xfail__2"].find("system-out") is not None
    assert cases["c_skip__3"].find("skipped") is not None
    assert cases["d_fail1__4"].find("failure") is not None
    assert cases["f_timeout__6"].find("error") is not None
    assert cases["g_false__7"].find("failure") is not None

    # results.txt contains status counts, failures, signatures, slowest jobs
    txt = (dir1 / "results.txt").read_text(encoding="utf-8")
    assert "Summary: 3/7 passed" in txt
    assert "Failures (4):" in txt
    assert "d_fail1__4" in txt
    assert "aaaa1111 (x2" in txt
    assert "Slowest jobs" in txt
    assert "f_timeout__6: 1800.000s" in txt


def test_atomic_write_preserves_old_file_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_dir = tmp_path / "out"
    results = _sample_results()[:1]
    write_results(out_dir, results, run_id="R1", partial=False, judged_at=T0)
    old_bytes = (out_dir / "results.json").read_bytes()

    orig_write_text = Path.write_text

    def _boom(self: Path, data: str, *args: object, **kwargs: object) -> int:
        if self.name.endswith(".tmp"):
            raise OSError("disk full simulated")
        return orig_write_text(self, data, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", _boom)
    with pytest.raises(InfraError) as exc_info:
        write_results(out_dir, results, run_id="R1", partial=True, judged_at=T0)
    assert exc_info.value.code == "PROVIDER_FAILED"
    assert (out_dir / "results.json").read_bytes() == old_bytes


def test_compare_text_and_self_contained_html_and_read_validation(tmp_path: Path) -> None:
    cur_res = _sample_results()
    base_res = [
        Result(
            job_id="a_pass__1",
            case="a_pass",
            seed=1,
            status="fail",
            judged_status="fail",
            raw_status="fail",
            category="design",
            reason="was broken",
        ),
        Result(
            job_id="d_fail1__4",
            case="d_fail1",
            seed=4,
            status="fail",
            judged_status="fail",
            raw_status="fail",
            category="design",
            reason="still broken",
        ),
        Result(
            job_id="f_timeout__6",
            case="f_timeout",
            seed=6,
            status="pass",
            judged_status="pass",
            raw_status="pass",
            category=None,
            reason="ok",
        ),
    ]
    cur_payload = results_to_payload(cur_res, run_id="R2", partial=False, judged_at=T0)
    base_payload = results_to_payload(base_res, run_id="R1", partial=False, judged_at=T0)

    comp = compare_results(cur_payload, base_payload)
    assert comp["baseline_run_id"] == "R1"
    assert [x["job_id"] for x in comp["newly_fixed"]] == ["a_pass__1"]
    assert [x["job_id"] for x in comp["still_failing"]] == ["d_fail1__4"]
    assert "f_timeout__6" in [x["job_id"] for x in comp["new_failures"]]

    txt = render_report(cur_payload, fmt="text", compare=comp)
    assert "Compare vs R1:" in txt
    assert "Newly fixed (1):" in txt
    assert "a_pass__1: fail -> pass" in txt

    # HTML report: self-contained (zero external URLs) and escapes HTML special chars
    xss_payload = results_to_payload(
        [
            Result(
                job_id="xss__1",
                case="xss",
                seed=1,
                status="fail",
                judged_status="fail",
                raw_status="fail",
                category="design",
                reason="<script>alert(1)</script>",
                first_error={"line": 1, "text": "<img src=x onerror=alert(1)>"},
            )
        ],
        run_id="R_XSS",
        partial=False,
        judged_at=T0,
    )
    html_str = render_html(xss_payload, project="proj<1>", compare=comp)
    assert "<!DOCTYPE html>" in html_str
    assert "http://" not in html_str and "https://" not in html_str
    assert "<script>alert(1)</script>" not in html_str
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html_str
    assert "&lt;img src=x onerror=alert(1)&gt;" in html_str

    # write_reports with single output_path and multiple formats
    single_html = tmp_path / "custom.html"
    written = write_reports(
        tmp_path / "unused",
        cur_payload,
        formats=("html",),
        project="axi_vip",
        compare=comp,
        output_path=single_html,
    )
    assert written == [single_html]
    assert single_html.is_file()

    # read_results_json validation
    bad_schema = tmp_path / "bad_results.json"
    bad_schema.write_text('{"schema": 99}\n', encoding="utf-8")
    with pytest.raises(ConfigError) as exc_schema:
        read_results_json(bad_schema)
    assert exc_schema.value.code == "CFG_INVALID"

    with pytest.raises(ConfigError) as exc_missing:
        read_results_json(tmp_path / "missing.json")
    assert exc_missing.value.code == "RUN_NOT_FOUND"

    with pytest.raises(ConfigError):
        render_report(cur_payload, fmt="pdf")


def test_job_duration_missing_attempts_and_results_validation(tmp_path: Path) -> None:
    assert job_duration({"job_id": "x__1", "status": "pass"}) == 0.0
    assert job_duration({"job_id": "x__1", "attempts": [{"duration_s": float("nan")}]}) == 0.0

    bad_file = tmp_path / "results.json"
    for bad_payload in (
        {"schema": 1, "summary": "not-a-dict", "jobs": []},
        {"schema": 1, "summary": {"total": "bad"}, "jobs": []},
        {"schema": 1, "summary": {"duration_s": float("inf")}, "jobs": []},
        {"schema": 1, "summary": {}, "jobs": "not-a-list"},
        {"schema": 1, "summary": {}, "jobs": [123]},
        {"schema": 1, "summary": {}, "jobs": [{"attempts": "bad"}]},
    ):
        bad_file.write_text(json.dumps(bad_payload, allow_nan=True), encoding="utf-8")
        with pytest.raises(ConfigError) as exc_read:
            read_results_json(bad_file)
        assert exc_read.value.code == "CFG_INVALID"
        assert "solouvm judge" in (exc_read.value.hint or "")

        with pytest.raises(ConfigError) as exc_render:
            render_report(bad_payload)  # type: ignore[arg-type]
        assert exc_render.value.code == "CFG_INVALID"


def test_compare_results_modes() -> None:
    base = results_to_payload(
        [
            Result(
                job_id="a__1",
                case="a",
                seed=1,
                status="pass",
                judged_status="pass",
                raw_status="pass",
                category=None,
                reason="clean",
            )
        ],
        run_id="R1",
        partial=False,
        judged_at=T0,
    )
    cur = results_to_payload(
        [
            Result(
                job_id="a__1",
                case="a",
                seed=1,
                status="false_pass",
                judged_status="pass",
                raw_status="pass",
                category="design",
                reason="triage false_pass",
            )
        ],
        run_id="R2",
        partial=False,
        judged_at=T0,
    )

    comp_status = compare_results(cur, base, mode="status")
    assert [x["job_id"] for x in comp_status["new_failures"]] == ["a__1"]

    comp_judged = compare_results(cur, base, mode="judged")
    assert comp_judged["new_failures"] == []

    with pytest.raises(ConfigError) as exc_mode:
        compare_results(cur, base, mode="bogus")
    assert exc_mode.value.code == "CFG_INVALID"
