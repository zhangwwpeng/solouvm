"""Tests for solouvm.cov (V-01..V-04) and solouvm.wave (AI-13)."""

from __future__ import annotations

import json
import os
import shlex
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from solouvm.config import load_config
from solouvm.config.schema import Config
from solouvm.cov import cov_merge, cov_report
from solouvm.errors import ConfigError, InfraError
from solouvm.model import Job, Plan
from solouvm.providers import (
    LocalTransport,
    MakeLauncher,
    NoneSync,
    Provider,
    SshTransport,
    get_provider,
)
from solouvm.wave import find_waveform, format_wave_table, query_wave

_SAMPLE_VCD = """\
$date
   Nov 18, 2026
$end
$version
   solouvm-test 1.0
$end
$timescale
   1ps
$end
$scope module tb_top $end
$scope module dut $end
$var wire 1 ! clk $end
$var wire 1 " rst_n $end
$var reg 32 # araddr [31:0] $end
$var wire 1 $ arvalid $end
$upscope $end
$scope module mon $end
$var wire 1 ! clk $end
$var wire 8 % other_cnt $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
$dumpvars
0!
0"
b0 #
0$
b00000001 %
$end
#5000
1!
#10000
b00000010 %
#15000
0!
1"
#20000
1!
b1000000000000 #
1$
#25000
b00000011 %
#30000
0!
b1000000000100 #
0$
"""


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def _install_stub(bin_dir: Path, name: str, script: str) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    target = bin_dir / name
    header = "#!/usr/bin/env bash\nset -euo pipefail\n"
    target.write_text(header + textwrap.dedent(script), encoding="utf-8")
    target.chmod(0o755)
    return target


def _make_config(proj: Path, *, simulator: str = "vcs") -> Config:
    _write(
        proj / "solouvm.conf.py",
        f"""
        CFG = {{
            "simulator": {simulator!r},
            "top": "tb_top",
            "builds": {{
                "default": {{}},
                "cov": {{"coverage": True}},
            }},
        }}
        """,
    )
    return load_config(proj)


def _make_job(run_id: str, job_id: str, *, build: str = "cov") -> Job:
    case, _, seed_str = job_id.partition("__")
    return Job(
        job_id=job_id,
        case=case,
        seed=int(seed_str),
        build=build,
        uvm_test=f"{case}_test",
        params={},
        opts=[],
        env={},
        timeout_s=60.0,
        hang_s=None,
        retry=1,
        expect_fail=False,
        tags=(),
        dir=f".solouvm/runs/{run_id}/{job_id}",
    )


def _make_plan(run_id: str, job_ids: list[str], *, build: str = "cov") -> Plan:
    return Plan(
        run_id=run_id,
        created_at=datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc),
        parent_run=None,
        provider="local",
        selection={"case": []},
        builds={build: "112233445566"},
        jobs=[_make_job(run_id, jid, build=build) for jid in job_ids],
    )


def _setup_ssh_stub(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "ssh_bin"
    remote_root = tmp_path / "remote_b"
    remote_root.mkdir(parents=True, exist_ok=True)
    helper = tmp_path / "ssh_stub.py"
    helper.write_text(
        textwrap.dedent(
            """
            import subprocess
            import sys

            args = sys.argv[1:]
            i = 0
            while i < len(args) and args[i].startswith("-"):
                if args[i] in ("-p", "-o", "-i", "-l", "-F") and i + 1 < len(args):
                    i += 2
                else:
                    i += 1
            cmd = args[i + 1] if i + 1 < len(args) else ""
            proc = subprocess.run(["sh", "-c", cmd])
            sys.exit(proc.returncode)
            """
        ),
        encoding="utf-8",
    )
    _install_stub(
        bin_dir,
        "ssh",
        f'exec python3 {shlex.quote(str(helper))} "$@"\n',
    )
    return bin_dir, remote_root


# ---------------------------------------------------------------- cov_merge & cov_report


def test_cov_merge_and_report_vcs_urg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "proj_vcs"
    cfg = _make_config(proj, simulator="vcs")
    prov = get_provider(cfg)
    run_id = "20261118-120000-vcs1"
    plan = _make_plan(run_id, ["smoke__1", "burst__2", "broken__3", "skip__4"])

    # smoke__1 passed on try1; burst__2 failed try1, passed try2 (and has non-numeric tryr1);
    # broken__3 failed (must NOT be merged); skip__4 skipped.
    (proj / ".solouvm" / "runs" / run_id / "smoke__1" / "try1" / "cov.vdb").mkdir(parents=True)
    (proj / ".solouvm" / "runs" / run_id / "burst__2" / "try1" / "cov.vdb").mkdir(parents=True)
    (proj / ".solouvm" / "runs" / run_id / "burst__2" / "try2" / "cov.vdb").mkdir(parents=True)
    (proj / ".solouvm" / "runs" / run_id / "burst__2" / "tryr1" / "cov.vdb").mkdir(parents=True)
    (proj / ".solouvm" / "runs" / run_id / "broken__3" / "try1" / "cov.vdb").mkdir(parents=True)

    results_path = proj / ".solouvm" / "runs" / run_id / "results.json"
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "run_id": run_id,
                "jobs": [
                    {
                        "job_id": "smoke__1",
                        "status": "pass",
                        "attempts": [{"try": 1, "status": "pass"}],
                    },
                    {
                        "job_id": "burst__2",
                        "status": "pass",
                        "attempts": [
                            {"try": 1, "status": "fail"},
                            {"try": 2, "status": "pass"},
                            {"try": "r1", "status": "pass"},
                        ],
                    },
                    {
                        "job_id": "broken__3",
                        "status": "fail",
                        "attempts": [{"try": 1, "status": "fail"}],
                    },
                    {
                        "job_id": "skip__4",
                        "status": "skip",
                        "attempts": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    bin_dir = tmp_path / "bin"
    calls_log = tmp_path / "urg_calls.log"
    _install_stub(
        bin_dir,
        "urg",
        f"""
        printf '%s\\n' "$*" >> {shlex.quote(str(calls_log))}
        dbname=""
        report_dir=""
        while [ $# -gt 0 ]; do
            case "$1" in
                -dbname) dbname="$2"; shift 2 ;;
                -report) report_dir="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        if [ -n "$dbname" ]; then
            mkdir -p "$dbname"
            echo "urg merged $dbname"
        fi
        if [ -n "$report_dir" ]; then
            mkdir -p "$report_dir"
            cat > "$report_dir/dashboard.txt" <<'EOF'
        Total Coverage Summary
        SCORE   LINE   COND   TOGGLE FSM    BRANCH
         87.50  92.30  81.25  88.00  75.00  90.10
        EOF
            echo "<html>urg full report</html>" > "$report_dir/dashboard.html"
            echo "urg report generated in $report_dir"
        fi
        """,
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    merged = cov_merge(config=cfg, plan=plan, provider=prov)
    assert merged["run_id"] == run_id
    assert merged["job_count"] == 2
    assert merged["tool"] == "urg"
    assert merged["merged_dir"] == f".solouvm/runs/{run_id}/cov"
    assert (proj / ".solouvm" / "runs" / run_id / "cov" / "merged.vdb").is_dir()

    merge_cmd = calls_log.read_text(encoding="utf-8").splitlines()[0]
    assert "-full64" in merge_cmd
    assert f".solouvm/runs/{run_id}/smoke__1/try1/cov.vdb" in merge_cmd
    assert f".solouvm/runs/{run_id}/burst__2/try2/cov.vdb" in merge_cmd
    assert "burst__2/try1" not in merge_cmd
    assert "burst__2/tryr1" not in merge_cmd
    assert "broken__3" not in merge_cmd

    report = cov_report(
        config=cfg,
        plan=plan,
        provider=prov,
        merged_dir=str(merged["merged_dir"]),
    )
    assert report["run_id"] == run_id
    assert report["tool"] == "urg"
    assert (proj / ".solouvm" / "runs" / run_id / "cov" / "report" / "dashboard.html").is_file()
    assert report["metrics"] == {
        "total": 87.5,
        "line": 92.3,
        "cond": 81.25,
        "toggle": 88.0,
        "fsm": 75.0,
        "branch": 90.1,
    }


def test_cov_merge_and_report_questa_vcover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj_questa"
    cfg = _make_config(proj, simulator="questa")
    prov = get_provider(cfg)
    run_id = "20261118-120000-qst1"
    plan = _make_plan(run_id, ["smoke__1", "neg__2"])

    # No attempts array in results_data -> fallback probing finds try1/cov.ucdb
    ucdb1 = proj / ".solouvm" / "runs" / run_id / "smoke__1" / "try1" / "cov.ucdb"
    ucdb2 = proj / ".solouvm" / "runs" / run_id / "neg__2" / "try1" / "cov.ucdb"
    _write(ucdb1, "ucdb1")
    _write(ucdb2, "ucdb2")
    results_data: dict[str, object] = {
        "schema": 1,
        "run_id": run_id,
        "jobs": [
            {"job_id": "smoke__1", "status": "pass"},
            {"job_id": "neg__2", "status": "xfail"},
        ],
    }

    bin_dir = tmp_path / "bin"
    calls_log = tmp_path / "vcover_calls.log"
    _install_stub(
        bin_dir,
        "vcover",
        f"""
        printf '%s\\n' "$*" >> {shlex.quote(str(calls_log))}
        subcmd="$1"
        shift
        if [ "$subcmd" = "merge" ]; then
            out="$1"
            mkdir -p "$(dirname "$out")"
            printf 'merged ucdb\\n' > "$out"
            echo "vcover merged -> $out"
        elif [ "$subcmd" = "report" ]; then
            cat <<'EOF'
        Coverage Summary:
        Statements:       91.20% (456/500)
        Branches:         85.50% (171/200)
        Conditions:       78.00% (78/100)
        FSM States:       90.00%
        Toggles:          75.00%
        TOTAL COVERGROUP COVERAGE: 88.50%  COVERGROUP TYPES: 5
        Total Coverage By Instance (filtered view): 83.45%
        EOF
        fi
        """,
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    merged = cov_merge(config=cfg, plan=plan, provider=prov, results_data=results_data)
    assert merged["job_count"] == 2
    assert merged["tool"] == "vcover"
    assert (proj / ".solouvm" / "runs" / run_id / "cov" / "merged.ucdb").is_file()

    report = cov_report(config=cfg, plan=plan, provider=prov)
    assert report["tool"] == "vcover"
    assert report["metrics"] == {
        "line": 91.2,
        "branch": 85.5,
        "cond": 78.0,
        "fsm": 90.0,
        "toggle": 75.0,
        "total": 83.45,
    }


def test_cov_xrun_verilator_and_custom_sim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    _install_stub(
        bin_dir,
        "imc",
        """
        echo "imc $*"
        echo "Overall Average: 84.20%"
        echo "Block: 90.00%"
        echo "Branch: 82.00%"
        echo "Expression: 76.50%"
        echo "Toggle: 88.00%"
        echo "FSM: 80.00%"
        """,
    )
    _install_stub(
        bin_dir,
        "verilator_coverage",
        """
        echo "verilator_coverage $*"
        echo "Total coverage (42/50) 84.00%"
        """,
    )
    _install_stub(
        bin_dir,
        "mycov",
        """
        echo "Line: 95.0%"
        echo "Total: 95.0%"
        """,
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    # Xcelium (imc)
    proj_x = tmp_path / "proj_xrun"
    cfg_x = _make_config(proj_x, simulator="xrun")
    prov_x = get_provider(cfg_x)
    plan_x = _make_plan("run_x", ["smoke__1"])
    (proj_x / ".solouvm" / "runs" / "run_x" / "smoke__1" / "try1" / "cov_work").mkdir(parents=True)
    res_x: dict[str, object] = {"jobs": [{"job_id": "smoke__1", "status": "pass"}]}
    m_x = cov_merge(config=cfg_x, plan=plan_x, provider=prov_x, results_data=res_x)
    assert m_x["tool"] == "imc"
    assert (proj_x / ".solouvm" / "runs" / "run_x" / "cov" / "merge.tcl").is_file()
    r_x = cov_report(config=cfg_x, plan=plan_x, provider=prov_x)
    assert r_x["metrics"]["total"] == 84.2
    assert r_x["metrics"]["cond"] == 76.5

    # Verilator
    proj_v = tmp_path / "proj_verilator"
    cfg_v = _make_config(proj_v, simulator="verilator")
    prov_v = get_provider(cfg_v)
    plan_v = _make_plan("run_v", ["smoke__1"])
    _write(proj_v / ".solouvm" / "runs" / "run_v" / "smoke__1" / "try1" / "coverage.dat", "dat")
    res_v: dict[str, object] = {"jobs": [{"job_id": "smoke__1", "status": "pass"}]}
    m_v = cov_merge(config=cfg_v, plan=plan_v, provider=prov_v, results_data=res_v)
    assert m_v["tool"] == "verilator_coverage"
    r_v = cov_report(config=cfg_v, plan=plan_v, provider=prov_v)
    assert r_v["metrics"]["total"] == 84.0

    # Custom config.sim["cov_merge"] & config.sim["cov_report"]
    cfg_v.sim["cov_merge"] = "mycov merge -o {out_dir}/custom.db {dbs}"
    cfg_v.sim["cov_report"] = ["mycov", "report", "{merged_dir}"]
    m_c = cov_merge(config=cfg_v, plan=plan_v, provider=prov_v, results_data=res_v)
    assert m_c["tool"] == "mycov"
    r_c = cov_report(config=cfg_v, plan=plan_v, provider=prov_v)
    assert r_c["tool"] == "mycov"
    assert r_c["metrics"] == {"line": 95.0, "total": 95.0}


def test_cov_errors_missing_results_db_and_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj_err"
    cfg = _make_config(proj, simulator="vcs")
    prov = get_provider(cfg)
    run_id = "20261118-120000-err1"
    plan = _make_plan(run_id, ["smoke__1"])

    # Missing results.json -> RUN_NOT_FOUND
    with pytest.raises(ConfigError) as exc_run:
        cov_merge(config=cfg, plan=plan, provider=prov)
    assert exc_run.value.code == "RUN_NOT_FOUND"

    # results.json present, no coverage databases in passed jobs -> CFG_INVALID
    with pytest.raises(ConfigError) as exc_db:
        cov_merge(
            config=cfg,
            plan=plan,
            provider=prov,
            results_data={"jobs": [{"job_id": "smoke__1", "status": "pass"}]},
        )
    assert exc_db.value.code == "CFG_INVALID"
    assert "no coverage databases found in passed jobs" in exc_db.value.message
    assert "coverage=True" in (exc_db.value.hint or "")

    # Coverage DB exists, but tool missing on PATH -> InfraError(PROVIDER_FAILED)
    (proj / ".solouvm" / "runs" / run_id / "smoke__1" / "try1" / "cov.vdb").mkdir(parents=True)
    monkeypatch.setenv("PATH", str(tmp_path / "empty_bin"))
    with pytest.raises(InfraError) as exc_merge_tool:
        cov_merge(
            config=cfg,
            plan=plan,
            provider=prov,
            results_data={"jobs": [{"job_id": "smoke__1", "status": "pass"}]},
        )
    assert exc_merge_tool.value.code == "PROVIDER_FAILED"
    assert exc_merge_tool.value.hint

    with pytest.raises(InfraError) as exc_report_tool:
        cov_report(config=cfg, plan=plan, provider=prov)
    assert exc_report_tool.value.code == "PROVIDER_FAILED"
    assert "urg" in (exc_report_tool.value.hint or "")

    # Non-zero exit code from coverage tool -> InfraError(PROVIDER_FAILED)
    bad_bin = tmp_path / "bad_bin"
    _install_stub(bad_bin, "urg", 'echo "corrupt vdb" >&2\nexit 2\n')
    monkeypatch.setenv("PATH", f"{bad_bin}:/usr/bin:/bin")
    with pytest.raises(InfraError) as exc_fail:
        cov_report(config=cfg, plan=plan, provider=prov)
    assert exc_fail.value.code == "PROVIDER_FAILED"
    assert "corrupt vdb" in exc_fail.value.message


# ---------------------------------------------------------------- find_waveform & VCD query_wave


def test_find_waveform_latest_try_explicit_try_and_missing(tmp_path: Path) -> None:
    proj = tmp_path / "proj_wave"
    cfg = _make_config(proj, simulator="fake")
    prov = get_provider(cfg)
    run_id = "20261118-120000-w1"
    job_id = "burst__4417"

    # Missing waveform -> ConfigError hinting solouvm rerun --wave
    with pytest.raises(ConfigError) as exc_missing:
        find_waveform(prov, run_id, job_id)
    assert exc_missing.value.code == "CFG_INVALID"
    assert f"solouvm rerun --run {run_id} --wave" in (exc_missing.value.hint or "")

    # Multiple tries: try1 (vcd), try2 (vcd), try3 (no waveform), try10 (vcd)
    job_root = proj / ".solouvm" / "runs" / run_id / job_id
    _write(job_root / "try1" / "wave.vcd", _SAMPLE_VCD)
    _write(job_root / "try2" / "dump.vcd", _SAMPLE_VCD)
    (job_root / "try3").mkdir(parents=True)
    _write(job_root / "try10" / "wave.fsdb", "dummy")

    assert find_waveform(prov, run_id, job_id) == f".solouvm/runs/{run_id}/{job_id}/try10/wave.fsdb"
    assert (
        find_waveform(prov, run_id, job_id, try_num="2")
        == f".solouvm/runs/{run_id}/{job_id}/try2/dump.vcd"
    )
    assert (
        find_waveform(prov, run_id, job_id, try_num="try1")
        == f".solouvm/runs/{run_id}/{job_id}/try1/wave.vcd"
    )

    # Non-numeric repro try (tryr1) wins over try10 when present with waveform
    _write(job_root / "tryr1" / "novas.fsdb", "repro")
    assert (
        find_waveform(prov, run_id, job_id) == f".solouvm/runs/{run_id}/{job_id}/tryr1/novas.fsdb"
    )


def test_query_wave_vcd_multiscope_globs_window_changes_and_limit(tmp_path: Path) -> None:
    proj = tmp_path / "proj_vcd"
    cfg = _make_config(proj, simulator="iverilog")
    prov = get_provider(cfg)
    run_id = "20261118-120000-vcd"
    job_id = "burst__4417"
    vcd_path = proj / ".solouvm" / "runs" / run_id / job_id / "try1" / "wave.vcd"
    _write(vcd_path, _SAMPLE_VCD)

    # 1. Glob tb_top.dut.* with changes_only=True across whole trace:
    #    10ns and 25ns only changed tb_top.mon.other_cnt -> skipped!
    res = query_wave(
        config=cfg,
        run_id=run_id,
        job_id=job_id,
        provider=prov,
        signals=["tb_top.dut.*"],
    )
    assert res["run_id"] == run_id
    assert res["job_id"] == job_id
    assert res["wave_file"] == f".solouvm/runs/{run_id}/{job_id}/try1/wave.vcd"
    assert res["signals"] == [
        "tb_top.dut.clk",
        "tb_top.dut.rst_n",
        "tb_top.dut.araddr",
        "tb_top.dut.arvalid",
    ]
    assert res["truncated"] is False
    times = [r["time"] for r in res["rows"]]  # type: ignore[index]
    assert times == ["0ns", "5ns", "15ns", "20ns", "30ns"]
    # Verify vector formatting & aliased signal values
    row_20 = res["rows"][3]  # type: ignore[index]
    assert row_20["time_ns"] == 20.0
    assert row_20["values"] == {
        "tb_top.dut.clk": "1",
        "tb_top.dut.rst_n": "1",
        "tb_top.dut.araddr": "0x1000",
        "tb_top.dut.arvalid": "1",
    }

    # 2. changes_only=False keeps 10ns and 25ns even though only tb_top.mon.other_cnt changed
    res_all = query_wave(
        config=cfg,
        run_id=run_id,
        job_id=job_id,
        provider=prov,
        signals=["tb_top.dut.*"],
        changes_only=False,
    )
    assert [r["time"] for r in res_all["rows"]] == [  # type: ignore[index]
        "0ns",
        "5ns",
        "10ns",
        "15ns",
        "20ns",
        "25ns",
        "30ns",
    ]

    # 3. --from / --to windowing carries prior state forward (rst_n stays "1" at 20ns)
    res_win = query_wave(
        config=cfg,
        run_id=run_id,
        job_id=job_id,
        provider=prov,
        signals=["tb_top.dut.*"],
        time_from="20ns",
        time_to="30ns",
    )
    assert [r["time"] for r in res_win["rows"]] == ["20ns", "30ns"]  # type: ignore[index]
    assert res_win["rows"][0]["values"]["tb_top.dut.rst_n"] == "1"  # type: ignore[index]
    assert res_win["rows"][1]["values"]["tb_top.dut.araddr"] == "0x1004"  # type: ignore[index]

    # 4. --limit truncation + format_wave_table output
    res_lim = query_wave(
        config=cfg,
        run_id=run_id,
        job_id=job_id,
        provider=prov,
        signals=["tb_top.dut.clk", "tb_top.dut.araddr"],
        limit=2,
    )
    assert res_lim["truncated"] is True
    assert len(res_lim["rows"]) == 2  # type: ignore[arg-type]

    table = format_wave_table(res_lim)
    assert "TIME" in table
    assert "tb_top.dut.clk" in table
    assert "tb_top.dut.araddr" in table
    assert "0ns" in table
    assert "5ns" in table
    assert "truncated" in table
    assert "--limit 0" in table


def test_query_wave_fsdb_wlf_shm_and_missing_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj_ext"
    cfg = _make_config(proj, simulator="vcs")
    prov = get_provider(cfg)
    run_id = "20261118-120000-ext"

    fsdb_file = proj / ".solouvm" / "runs" / run_id / "axi__1" / "try1" / "wave.fsdb"
    _write(fsdb_file, "fake fsdb binary")

    # Missing fsdbreport -> InfraError(PROVIDER_FAILED)
    orig_path = os.environ["PATH"]
    monkeypatch.setenv("PATH", str(tmp_path / "empty_bin"))
    with pytest.raises(InfraError) as exc_missing_fsdb:
        query_wave(
            config=cfg,
            run_id=run_id,
            job_id="axi__1",
            provider=prov,
            signals=["tb_top.dut.*"],
        )
    assert exc_missing_fsdb.value.code == "PROVIDER_FAILED"
    assert "fsdbreport" in (exc_missing_fsdb.value.hint or "")

    # Install stub fsdbreport, wlf2vcd, and simvisdbutil
    bin_dir = tmp_path / "bin"
    fsdb_args_log = tmp_path / "fsdb_args.log"
    _install_stub(
        bin_dir,
        "fsdbreport",
        f"""
        printf '%s\\n' "$*" >> {shlex.quote(str(fsdb_args_log))}
        cat <<'EOF'
        Time(ns)  tb_top.dut.araddr  tb_top.dut.arvalid  tb_top.other
        --------  -----------------  ------------------  ------------
        1200      0x00001000         1                   0
        1250      0x00001000         1                   1
        1300      0x00001004         0                   1
        EOF
        """,
    )
    _install_stub(
        bin_dir,
        "wlf2vcd",
        f"""
        out=""
        while [ $# -gt 0 ]; do
            case "$1" in
                -o) out="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        cat <<'EOF' > "$out"
{textwrap.indent(_SAMPLE_VCD, "        ")}
        EOF
        """,
    )
    _install_stub(
        bin_dir,
        "simvisdbutil",
        f"""
        out=""
        while [ $# -gt 0 ]; do
            case "$1" in
                -output) out="$2"; shift 2 ;;
                *) shift ;;
            esac
        done
        cat <<'EOF' > "$out"
{textwrap.indent(_SAMPLE_VCD, "        ")}
        EOF
        """,
    )
    monkeypatch.setenv("PATH", f"{bin_dir}:{orig_path}")

    res_fsdb = query_wave(
        config=cfg,
        run_id=run_id,
        job_id="axi__1",
        provider=prov,
        signals=["tb_top.dut.*"],
        time_from="1.2us",
        time_to="1500ns",
    )
    args_text = fsdb_args_log.read_text(encoding="utf-8")
    assert "-s tb_top.dut.*" in args_text
    assert "-bt 1200ns" in args_text
    assert "-et 1500ns" in args_text
    assert res_fsdb["signals"] == ["tb_top.dut.araddr", "tb_top.dut.arvalid"]
    # 1250ns only changed tb_top.other -> deduplicated!
    assert [r["time"] for r in res_fsdb["rows"]] == ["1200ns", "1300ns"]  # type: ignore[index]

    # .wlf via wlf2vcd (cleans up temp VCD)
    wlf_file = proj / ".solouvm" / "runs" / run_id / "wlf__1" / "try1" / "wave.wlf"
    _write(wlf_file, "fake wlf")
    res_wlf = query_wave(
        config=cfg,
        run_id=run_id,
        job_id="wlf__1",
        provider=prov,
        signals=["tb_top.dut.araddr"],
    )
    assert res_wlf["signals"] == ["tb_top.dut.araddr"]
    assert res_wlf["rows"][-1]["values"]["tb_top.dut.araddr"] == "0x1004"  # type: ignore[index]
    assert not Path(f"{wlf_file}.solouvm_tmp.vcd").exists()

    # .shm directory via simvisdbutil (cleans up temp VCD)
    shm_dir = proj / ".solouvm" / "runs" / run_id / "shm__1" / "try1" / "waves.shm"
    shm_dir.mkdir(parents=True)
    res_shm = query_wave(
        config=cfg,
        run_id=run_id,
        job_id="shm__1",
        provider=prov,
        signals=["tb_top.dut.arvalid"],
    )
    assert res_shm["signals"] == ["tb_top.dut.arvalid"]
    assert not Path(f"{shm_dir}.solouvm_tmp.vcd").exists()


def test_cov_and_wave_over_ssh_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ssh_bin, remote_root = _setup_ssh_stub(tmp_path)
    proj = tmp_path / "local_a"
    cfg = _make_config(proj, simulator="vcs")
    ssh = SshTransport(host="simbox", workdir=str(remote_root), root=proj)
    prov = Provider(
        name="farm",
        transport=ssh,
        launcher=MakeLauncher(),
        sync=NoneSync(),
    )

    run_id = "20261118-120000-ssh"
    plan = _make_plan(run_id, ["smoke__1"])
    (remote_root / ".solouvm" / "runs" / run_id / "smoke__1" / "try1" / "cov.vdb").mkdir(
        parents=True
    )
    _write(
        remote_root / ".solouvm" / "runs" / run_id / "smoke__1" / "try1" / "wave.vcd",
        _SAMPLE_VCD,
    )

    tool_bin = tmp_path / "tool_bin"
    _install_stub(
        tool_bin,
        "urg",
        """
        echo "SCORE LINE"
        echo "91.0 94.0"
        """,
    )
    monkeypatch.setenv("PATH", f"{ssh_bin}:{tool_bin}:{os.environ['PATH']}")

    merged = cov_merge(
        config=cfg,
        plan=plan,
        provider=prov,
        results_data={"jobs": [{"job_id": "smoke__1", "status": "pass"}]},
    )
    assert merged["job_count"] == 1
    report = cov_report(config=cfg, plan=plan, provider=prov)
    assert report["metrics"] == {"total": 91.0, "line": 94.0}

    wave = query_wave(
        config=cfg,
        run_id=run_id,
        job_id="smoke__1",
        provider=prov,
        signals=["tb_top.dut.araddr"],
        time_from="20ns",
    )
    assert [r["time"] for r in wave["rows"]] == ["20ns", "30ns"]  # type: ignore[index]


def test_wave_and_cov_local_transport_smoke(tmp_path: Path) -> None:
    # Verify query_wave rejects empty signals and bad time bounds with ConfigError
    proj = tmp_path / "proj_bad"
    cfg = _make_config(proj, simulator="fake")
    prov = Provider(
        name="local",
        transport=LocalTransport(root=proj),
        launcher=MakeLauncher(),
        sync=NoneSync(),
    )
    with pytest.raises(ConfigError) as exc_sig:
        query_wave(config=cfg, run_id="r1", job_id="j1", provider=prov, signals=[])
    assert exc_sig.value.code == "CFG_INVALID"

    with pytest.raises(ConfigError) as exc_win:
        query_wave(
            config=cfg,
            run_id="r1",
            job_id="j1",
            provider=prov,
            signals=["a"],
            time_from="200ns",
            time_to="100ns",
        )
    assert exc_win.value.code == "CFG_INVALID"

    # VCD x/z values and unmatched signal pattern handling
    vcd = proj / ".solouvm" / "runs" / "r1" / "j1" / "try1" / "wave.vcd"
    _write(
        vcd,
        """\
        $timescale 1ns $end
        $scope module top $end
        $var wire 1 ! sig_x $end
        $var wire 4 " bus_z $end
        $upscope $end
        $enddefinitions $end
        #0
        x!
        b10xz "
        #10
        z!
        b1111 "
        """,
    )
    res_xz = query_wave(
        config=cfg,
        run_id="r1",
        job_id="j1",
        provider=prov,
        signals=["top.*"],
    )
    assert res_xz["rows"][0]["values"] == {"top.sig_x": "x", "top.bus_z": "b10xz"}  # type: ignore[index]
    assert res_xz["rows"][1]["values"] == {"top.sig_x": "z", "top.bus_z": "0xf"}  # type: ignore[index]

    res_none = query_wave(
        config=cfg,
        run_id="r1",
        job_id="j1",
        provider=prov,
        signals=["top.missing_*"],
    )
    assert res_none["signals"] == []
    assert res_none["rows"] == []
    assert res_none["truncated"] is False
    assert "no matching signals" in format_wave_table(res_none)


def test_query_wave_vcd_memory_arrays_and_split_slices(tmp_path: Path) -> None:
    proj = tmp_path / "proj_mem"
    cfg = _make_config(proj, simulator="iverilog")
    prov = Provider(
        name="local",
        transport=LocalTransport(root=proj),
        launcher=MakeLauncher(),
        sync=NoneSync(),
    )
    run_id = "20261118-120000-mem"
    job_id = "mem__1"
    vcd = proj / ".solouvm" / "runs" / run_id / job_id / "try1" / "wave.vcd"
    _write(
        vcd,
        """\
        $timescale 1ns $end
        $scope module tb $end
        $var wire 8 ! mem [0] $end
        $var wire 8 " mem [1] $end
        $var reg 8 # arr [0] [7:0] $end
        $var reg 8 $ arr [1] [7:0] $end
        $var wire 4 % bus [7:4] $end
        $var wire 4 & bus [3:0] $end
        $upscope $end
        $enddefinitions $end
        #0
        b00000001 !
        b00000010 "
        b00001010 #
        b00001011 $
        b1100 %
        b0011 &
        """,
    )

    res_mem = query_wave(
        config=cfg, run_id=run_id, job_id=job_id, provider=prov, signals=["tb.mem*"]
    )
    assert res_mem["signals"] == ["tb.mem[0]", "tb.mem[1]"]
    assert res_mem["rows"][0]["values"] == {"tb.mem[0]": "0x1", "tb.mem[1]": "0x2"}  # type: ignore[index]

    res_elem = query_wave(
        config=cfg, run_id=run_id, job_id=job_id, provider=prov, signals=["tb.mem[[]0]"]
    )
    assert res_elem["signals"] == ["tb.mem[0]"]
    assert res_elem["rows"][0]["values"] == {"tb.mem[0]": "0x1"}  # type: ignore[index]

    res_arr = query_wave(
        config=cfg, run_id=run_id, job_id=job_id, provider=prov, signals=["tb.arr*"]
    )
    assert res_arr["signals"] == ["tb.arr[0]", "tb.arr[1]"]
    assert res_arr["rows"][0]["values"] == {"tb.arr[0]": "0xa", "tb.arr[1]": "0xb"}  # type: ignore[index]

    res_bus = query_wave(
        config=cfg, run_id=run_id, job_id=job_id, provider=prov, signals=["tb.bus"]
    )
    assert res_bus["signals"] == ["tb.bus[7:4]", "tb.bus[3:0]"]
    assert res_bus["rows"][0]["values"] == {"tb.bus[7:4]": "0xc", "tb.bus[3:0]": "0x3"}  # type: ignore[index]
