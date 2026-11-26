"""Opt-in live SSH end-to-end tests against real EDA & open-source simulators.

Skipped during normal offline ``pytest`` runs. Enable with::

    SOLOUVM_LIVE_E2E=1 .venv/bin/pytest -v -s tests/test_live_simulators.py
    SOLOUVM_LIVE_E2E=1 .venv/bin/pytest -v -s tests/test_live_simulators.py -k questa
    SOLOUVM_LIVE_E2E=1 .venv/bin/pytest -v -s tests/test_live_simulators.py -k vcs
    SOLOUVM_LIVE_E2E=1 .venv/bin/pytest -v -s tests/test_live_simulators.py -k verilator
    SOLOUVM_LIVE_E2E=1 .venv/bin/pytest -v -s tests/test_live_simulators.py -k xezim

Environment overrides:
    SOLOUVM_UBUNTU_HOST   (default: 192.168.3.10)
    SOLOUVM_ROCKY_HOST    (default: 192.168.3.25)
    SOLOUVM_QUESTA_BIN    (default: /home/gtr/tools/mentor_2024/questasim/linux_x86_64)
    SOLOUVM_QUESTA_LIC    (default: /home/gtr/tools/mentor_2024/mentor2021_crack/licGen/license.dat)
    SOLOUVM_UVM_DIR       (default: /home/gtr/tools/mentor_2024/questasim/verilog_src/uvm-1.2)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("SOLOUVM_LIVE_E2E") != "1",
    reason="live simulator E2E tests require SOLOUVM_LIVE_E2E=1",
)

UBUNTU_HOST = os.environ.get("SOLOUVM_UBUNTU_HOST", "192.168.3.10")
ROCKY_HOST = os.environ.get("SOLOUVM_ROCKY_HOST", "192.168.3.25")
QUESTA_BIN = os.environ.get(
    "SOLOUVM_QUESTA_BIN", "/home/gtr/tools/mentor_2024/questasim/linux_x86_64"
)
QUESTA_LIC = os.environ.get(
    "SOLOUVM_QUESTA_LIC", "/home/gtr/tools/mentor_2024/mentor2021_crack/licGen/license.dat"
)
UVM_DIR = os.environ.get(
    "SOLOUVM_UVM_DIR", "/home/gtr/tools/mentor_2024/questasim/verilog_src/uvm-1.2"
)

ADDER_SV = """\
module adder (
  input  logic        clk,
  input  logic        rst_n,
  input  logic [7:0]  a,
  input  logic [7:0]  b,
  output logic [8:0]  sum
);
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) sum <= '0;
    else if (a == 8'hFF) sum <= 9'h1FF;
    else sum <= a + b;
  end
endmodule
"""

UVM_TB_SV = """\
`include "uvm_macros.svh"
import uvm_pkg::*;

class smoke_test extends uvm_test;
  `uvm_component_utils(smoke_test)
  function new(string name="smoke_test", uvm_component parent=null);
    super.new(name, parent);
  endfunction
  task run_phase(uvm_phase phase);
    phase.raise_objection(this);
    #50ns;
    `uvm_info("adder_scb", "compared=4 errors=0", UVM_NONE)
    `uvm_info("TEST", "SOLOUVM_DONE", UVM_NONE)
    phase.drop_objection(this);
  endtask
endclass

module tb_top;
  logic clk = 0;
  logic rst_n = 0;
  logic [7:0] a = 8'd3, b = 8'd5;
  logic [8:0] sum;
  always #5ns clk = ~clk;
  adder u_dut (.clk(clk), .rst_n(rst_n), .a(a), .b(b), .sum(sum));
  initial begin
    #10ns rst_n = 1;
    #10ns a = 8'd10; b = 8'd20;
    #10ns a = 8'hFF; b = 8'd1;
  end
  initial run_test();
endmodule
"""

VERILATOR_TB_SV = """\
`timescale 1ns/1ps
module tb_top;
  logic clk = 0;
  logic rst_n = 0;
  logic [7:0] a = 8'd3, b = 8'd5;
  logic [8:0] sum;
  always #5ns clk = ~clk;
  adder u_dut (.clk(clk), .rst_n(rst_n), .a(a), .b(b), .sum(sum));
  initial begin
    if ($test$plusargs("SOLOUVM_WAVE=1")) begin
      $dumpfile("wave.vcd");
      $dumpvars(0, tb_top);
    end
    #10ns rst_n = 1;
    #10ns a = 8'd10; b = 8'd20;
    #10ns a = 8'hFF; b = 8'd1;
    #20ns;
    $display("SOLOUVM_DONE status=pass");
    $finish;
  end
endmodule
"""


def _solouvm(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-m", "solouvm", *args]
    res = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)
    assert res.returncode == 0, (
        f"solouvm {' '.join(args)} failed (rc={res.returncode})\n"
        f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )
    return res


def _run_live_simulator(
    tmp_path: Path,
    *,
    simulator: str,
    host: str,
    setup: str,
    builds: dict[str, object],
    use_uvm: bool,
    check_wave_query: bool,
) -> None:
    tag = uuid.uuid4().hex[:8]
    remote_work = f"/tmp/solouvm_live_{simulator}_{tag}"
    ctl_path = f"/tmp/suvm_live_{tag}_%h_%p"
    ssh_opts = [
        "-o",
        "ControlMaster=auto",
        "-o",
        "ControlPersist=120",
        "-o",
        f"ControlPath={ctl_path}",
    ]

    for _ in range(4):
        probe = subprocess.run(
            ["ssh", *ssh_opts, host, f"rm -rf {remote_work} && mkdir -p {remote_work}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            break
        time.sleep(2)
    else:
        pytest.fail(f"Cannot connect to {host}: {probe.stderr}")

    proj = tmp_path / f"proj_{simulator}"
    (proj / "rtl").mkdir(parents=True)
    (proj / "tb").mkdir(parents=True)
    (proj / "rtl" / "adder.sv").write_text(ADDER_SV, encoding="utf-8")
    (proj / "tb" / "tb_top.sv").write_text(
        UVM_TB_SV if use_uvm else VERILATOR_TB_SV, encoding="utf-8"
    )
    (proj / "filelist.py").write_text(
        'FILES = {"rtl": ["rtl/adder.sv"], "tb": [], "top": ["tb/tb_top.sv"]}\n',
        encoding="utf-8",
    )

    cfg = {
        "simulator": simulator,
        "top": "tb_top",
        "provider": "remote",
        "builds": builds,
        "providers": {
            "remote": {
                "transport": {
                    "kind": "ssh",
                    "host": host,
                    "workdir": remote_work,
                    "ssh_opts": ssh_opts,
                },
                "sync": {"kind": "rsync"},
                "launcher": {"kind": "make"},
                "setup": setup,
            }
        },
        "regress": {"jobs": 2},
    }
    (proj / "solouvm.conf.py").write_text(f"CFG = {cfg!r}\n", encoding="utf-8")

    if use_uvm:
        case_py = """\
from solouvm import Case, Expect
CASES = [
    Case(
        name="smoke",
        uvm_test="smoke_test",
        seeds=[1, 2],
        expect=[Expect(scoreboard="adder_scb", min=4), Expect(sim_time_min="40ns")],
    ),
    Case(
        name="smoke_cov",
        uvm_test="smoke_test",
        seeds=[1],
        build="cov",
        expect=[Expect(scoreboard="adder_scb", min=4), Expect(sim_time_min="40ns")],
    ),
]
"""
    else:
        case_py = """\
from solouvm import Case, Expect
CASES = [
    Case(name="smoke", seeds=[1, 2], expect=[Expect(sim_time_min="40ns")]),
    Case(name="smoke_cov", seeds=[1], build="cov", expect=[Expect(sim_time_min="40ns")]),
]
"""
    (proj / "case.py").write_text(case_py, encoding="utf-8")

    try:
        _solouvm(proj, "validate")
        _solouvm(proj, "doctor")
        _solouvm(proj, "regress")
        _solouvm(proj, "cov", "merge")
        r_cov = _solouvm(proj, "cov", "report", "--json")
        metrics = json.loads(r_cov.stdout)["data"]["metrics"]
        assert metrics, f"Expected non-empty coverage metrics for {simulator}"

        _solouvm(proj, "run", "--case", "smoke", "--seed", "1", "--wave")
        if check_wave_query:
            r_wave = _solouvm(
                proj,
                "wave",
                "smoke__1",
                "--signals",
                "tb_top.u_dut.sum",
                "--from",
                "0ns",
                "--to",
                "40ns",
                "--changes",
                "--json",
            )
            rows = json.loads(r_wave.stdout)["data"]["rows"]
            values = [row["values"]["tb_top.u_dut.sum"] for row in rows]
            assert values == ["0x0", "0x8", "0x1e", "0x1ff"], (
                f"Unexpected waveform transitions for {simulator}: {rows}"
            )

        _solouvm(proj, "clean", "--all")
    finally:
        subprocess.run(
            ["ssh", *ssh_opts, host, f"rm -rf {remote_work}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["ssh", "-O", "exit", "-o", f"ControlPath={ctl_path}", host],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def test_live_questa_qrun(tmp_path: Path) -> None:
    _run_live_simulator(
        tmp_path,
        simulator="questa",
        host=UBUNTU_HOST,
        setup=f'export PATH="{QUESTA_BIN}:$PATH" && export LM_LICENSE_FILE="{QUESTA_LIC}"',
        builds={"cov": {"coverage": True}},
        use_uvm=True,
        check_wave_query=True,
    )


def test_live_vcs(tmp_path: Path) -> None:
    _run_live_simulator(
        tmp_path,
        simulator="vcs",
        host=ROCKY_HOST,
        setup="",
        builds={"cov": {"coverage": True}},
        use_uvm=True,
        check_wave_query=True,
    )


def test_live_verilator(tmp_path: Path) -> None:
    _run_live_simulator(
        tmp_path,
        simulator="verilator",
        host=UBUNTU_HOST,
        setup='export PATH="$HOME/.local/bin:$PATH"',
        builds={
            "default": {"compile_opts": ["-Wno-fatal"]},
            "cov": {"coverage": True, "compile_opts": ["-Wno-fatal"]},
        },
        use_uvm=False,
        check_wave_query=True,
    )


def test_live_xezim(tmp_path: Path) -> None:
    _run_live_simulator(
        tmp_path,
        simulator="xezim",
        host=UBUNTU_HOST,
        setup=f'export PATH="$HOME/.local/bin:$PATH" && export XEZIM_UVM_DIR="{UVM_DIR}"',
        builds={
            "default": {"compile_opts": ["--uvm"], "run_opts": ["--uvm"]},
            "cov": {"coverage": True, "compile_opts": ["--uvm"], "run_opts": ["--uvm"]},
        },
        use_uvm=True,
        check_wave_query=False,
    )
