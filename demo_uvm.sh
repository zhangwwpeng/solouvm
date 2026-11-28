#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ -x "${SCRIPT_DIR}/.venv/bin/python" ]; then
  PY="${SCRIPT_DIR}/.venv/bin/python"
else
  PY="python3"
fi

log_error() {
  PYTHONPATH="${SCRIPT_DIR}" "${PY}" -c 'import sys; from solouvm import error; error(sys.argv[1])' "$*"
}

SIM=""
VFLAG=""
for arg in "$@"; do
  case "${arg}" in
    -h|--help)
      cat <<'EOF'
Usage: ./demo_uvm.sh [fake|questa|vcs|xezim] [-v]

Run ONLY the single case `burst` (seed 1) from a multi-case suite, emitting
80-100+ UVM log lines ([DRV] / [MON] / [AXI_SCB]) during simulation.

Options:
  fake|questa|vcs|xezim   Simulator backend (default: fake)
  -v                      Expand all UVM log lines permanently (default TTY uses 5-line rolling viewport)
  -h, --help              Show this help message and exit

Environment overrides for remote simulators:
  SOLOUVM_UBUNTU_HOST   (default: 192.168.3.10)
  SOLOUVM_ROCKY_HOST    (default: 192.168.3.25)
  SOLOUVM_QUESTA_BIN    (default: /home/gtr/tools/mentor_2024/questasim/linux_x86_64)
  SOLOUVM_QUESTA_LIC    (default: /home/gtr/tools/mentor_2024/mentor2021_crack/licGen/license.dat)
  SOLOUVM_UVM_DIR       (default: /home/gtr/tools/mentor_2024/questasim/verilog_src/uvm-1.2)
EOF
      exit 0
      ;;
    -v|--verbose)
      VFLAG="-v"
      ;;
    fake|questa|vcs|xezim)
      if [ -n "${SIM}" ]; then
        log_error "Multiple simulators specified: '${SIM}' and '${arg}'"
        exit 2
      fi
      SIM="${arg}"
      ;;
    *)
      log_error "Unknown argument '${arg}'. Expected: [fake|questa|vcs|xezim] [-v]"
      exit 2
      ;;
  esac
done

SIM="${SIM:-fake}"
DEMO_DIR="$(mktemp -d "/tmp/solouvm_demo_${SIM}.XXXXXX")"
HOST=""
REMOTE_WORK=""
CTL_PATH=""

cleanup() {
  if [ -n "${HOST}" ] && [ -n "${REMOTE_WORK}" ]; then
    if [ -n "${CTL_PATH}" ]; then
      ssh -o ControlPath="${CTL_PATH}" "${HOST}" "rm -rf '${REMOTE_WORK}'" >/dev/null 2>&1 || true
      ssh -O exit -o ControlPath="${CTL_PATH}" "${HOST}" >/dev/null 2>&1 || true
    else
      ssh "${HOST}" "rm -rf '${REMOTE_WORK}'" >/dev/null 2>&1 || true
    fi
  fi
  rm -rf "${DEMO_DIR}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

run_single_case() {
  if [ -n "${VFLAG}" ]; then
    PYTHONPATH="${SCRIPT_DIR}" "${PY}" -m solouvm -c "${DEMO_DIR}" "${VFLAG}" run --case burst --seed 1
  else
    PYTHONPATH="${SCRIPT_DIR}" "${PY}" -m solouvm -c "${DEMO_DIR}" run --case burst --seed 1
  fi
}

if [ "${SIM}" = "fake" ]; then
  mkdir -p "${DEMO_DIR}/rtl" "${DEMO_DIR}/tb"
  cat > "${DEMO_DIR}/rtl/adder.sv" <<'EOF'
module tb_top;
endmodule
EOF
  cat > "${DEMO_DIR}/tb/tb_top.sv" <<'EOF'
module tb_top;
endmodule
EOF
  echo 'FILES = {"rtl": ["rtl/adder.sv"], "tb": [], "top": ["tb/tb_top.sv"]}' > "${DEMO_DIR}/filelist.py"

  cat > "${DEMO_DIR}/sim_verbose_uvm.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
echo "UVM_INFO @ 0ns: reporter [RNTST] Running test burst_test..."
echo "UVM_INFO tb/tb_top.sv(10) @ 100ns: uvm_test_top.env [ENV] config randomized burst_len=25"
for i in $(seq 1 25); do
  t_ns=$((i * 1000))
  a=$(( (i * 7) % 255 ))
  b=$(( (i * 13) % 255 ))
  sum=$((a + b))
  printf "UVM_INFO tb/axi_drv.sv(84) @ %dns: uvm_test_top.env.agt.drv [DRV] beat=%02d drive a=0x%02x b=0x%02x\n" "$t_ns" "$i" "$a" "$b"
  printf "UVM_INFO tb/axi_mon.sv(61) @ %dns: uvm_test_top.env.agt.mon [MON] beat=%02d observe sum=0x%03x valid=1\n" "$((t_ns + 50))" "$i" "$sum"
  printf "UVM_INFO tb/axi_scb.sv(95) @ %dns: uvm_test_top.env.scb [AXI_SCB] beat=%02d match exp=0x%03x got=0x%03x\n" "$((t_ns + 100))" "$i" "$sum" "$sum"
  sleep 0.08
done
echo "UVM_INFO tb/axi_scb.sv(110) @ 26000ns: uvm_test_top.env.scb [AXI_SCB] compared=25 passed=25 failed=0 errors=0"
echo "UVM_INFO tb/tb_top.sv(45) @ 26000ns: uvm_test_top [TEST] SOLOUVM_DONE"
echo "UVM_INFO /tools/uvm/src/base/uvm_objection.svh(1271) @ 26000ns: reporter [TEST_DONE] 'run' phase is ready to proceed to the 'extract' phase"
echo ""
echo "--- UVM Report Summary ---"
echo ""
echo "** Report counts by severity"
echo "UVM_INFO :   80"
echo "UVM_WARNING :    0"
echo "UVM_ERROR :    0"
echo "UVM_FATAL :    0"
echo "sim time: 26000ns"
EOF
  chmod +x "${DEMO_DIR}/sim_verbose_uvm.sh"

  cat > "${DEMO_DIR}/solouvm.conf.py" <<'EOF'
CFG = {
    "simulator": "fake",
    "top": "tb_top",
    "sim": {
        "build": "touch {simv}",
        "run": "bash {root}/sim_verbose_uvm.sh",
    },
}
EOF

  cat > "${DEMO_DIR}/case.py" <<'EOF'
from solouvm import Case, Expect

CASES = [
    Case(name="smoke", uvm_test="smoke_test", seeds=[1, 2]),
    Case(
        name="burst",
        uvm_test="burst_test",
        seeds=[1, 2],
        expect=[Expect(scoreboard="AXI_SCB", min=25), Expect(sim_time_min="20us")],
    ),
    Case(name="stress", uvm_test="stress_test", seeds=[1, 2, 3]),
]
EOF

  run_single_case
  exit 0
fi

UBUNTU_HOST="${SOLOUVM_UBUNTU_HOST:-192.168.3.10}"
ROCKY_HOST="${SOLOUVM_ROCKY_HOST:-192.168.3.25}"
QUESTA_BIN="${SOLOUVM_QUESTA_BIN:-/home/gtr/tools/mentor_2024/questasim/linux_x86_64}"
QUESTA_LIC="${SOLOUVM_QUESTA_LIC:-/home/gtr/tools/mentor_2024/mentor2021_crack/licGen/license.dat}"
UVM_DIR="${SOLOUVM_UVM_DIR:-/home/gtr/tools/mentor_2024/questasim/verilog_src/uvm-1.2}"

REMOTE_WORK="/tmp/solouvm_demo_${SIM}_$$"
CTL_PATH="/tmp/suvm_d$$_%h_%p"

case "${SIM}" in
  questa)
    HOST="${UBUNTU_HOST}"
    SETUP="export PATH=\"${QUESTA_BIN}:\$PATH\" && export LM_LICENSE_FILE=\"${QUESTA_LIC}\""
    BUILDS="{}"
    ;;
  vcs)
    HOST="${ROCKY_HOST}"
    SETUP=""
    BUILDS="{}"
    ;;
  xezim)
    HOST="${UBUNTU_HOST}"
    SETUP="export PATH=\"\$HOME/.local/bin:\$PATH\" && export XEZIM_UVM_DIR=\"${UVM_DIR}\""
    BUILDS="{'default': {'compile_opts': ['--uvm'], 'run_opts': ['--uvm']}}"
    ;;
esac

mkdir -p "${DEMO_DIR}/rtl" "${DEMO_DIR}/tb"

cat > "${DEMO_DIR}/rtl/adder.sv" <<'EOF'
module adder (
  input  logic        clk,
  input  logic        rst_n,
  input  logic [7:0]  a,
  input  logic [7:0]  b,
  output logic [8:0]  sum
);
  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) sum <= '0;
    else sum <= a + b;
  end
endmodule
EOF

cat > "${DEMO_DIR}/tb/tb_top.sv" <<'EOF'
`include "uvm_macros.svh"
import uvm_pkg::*;

class burst_test extends uvm_test;
  `uvm_component_utils(burst_test)
  function new(string name="burst_test", uvm_component parent=null);
    super.new(name, parent);
  endfunction
  task run_phase(uvm_phase phase);
    int a_val, b_val, exp_val;
    phase.raise_objection(this);
    `uvm_info("ENV", "starting 30-beat burst sequence", UVM_NONE)
    for (int i = 0; i < 30; i++) begin
      #10ns;
      a_val = (i * 7) % 256;
      b_val = (i * 13) % 256;
      exp_val = a_val + b_val;
      `uvm_info("DRV", $sformatf("beat=%02d drive a=0x%02x b=0x%02x", i, a_val, b_val), UVM_NONE)
      `uvm_info("MON", $sformatf("beat=%02d observe sum=0x%03x valid=1", i, exp_val), UVM_NONE)
      `uvm_info("AXI_SCB", $sformatf("beat=%02d match exp=0x%03x got=0x%03x", i, exp_val, exp_val), UVM_NONE)
    end
    `uvm_info("AXI_SCB", "compared=30 errors=0", UVM_NONE)
    `uvm_info("TEST", "SOLOUVM_DONE", UVM_NONE)
    phase.drop_objection(this);
  endtask
endclass

module tb_top;
  logic clk = 0;
  logic rst_n = 0;
  logic [7:0] a = 8'd0, b = 8'd0;
  logic [8:0] sum;
  always #5ns clk = ~clk;
  adder u_dut (.*);
  initial begin
    #10ns rst_n = 1;
  end
  initial run_test();
endmodule
EOF

echo 'FILES = {"rtl": ["rtl/adder.sv"], "tb": [], "top": ["tb/tb_top.sv"]}' > "${DEMO_DIR}/filelist.py"

cat > "${DEMO_DIR}/case.py" <<'EOF'
from solouvm import Case, Expect

CASES = [
    Case(name="smoke", uvm_test="burst_test", seeds=[1, 2]),
    Case(
        name="burst",
        uvm_test="burst_test",
        seeds=[1, 2],
        expect=[Expect(scoreboard="AXI_SCB", min=30), Expect(sim_time_min="250ns")],
    ),
    Case(name="stress", uvm_test="burst_test", seeds=[1, 2, 3]),
]
EOF

ssh -o ControlMaster=auto -o ControlPersist=120 -o "ControlPath=${CTL_PATH}" "${HOST}" \
  "rm -rf '${REMOTE_WORK}' && mkdir -p '${REMOTE_WORK}'"

cat > "${DEMO_DIR}/solouvm.conf.py" <<EOF
CFG = {
    "simulator": "${SIM}",
    "top": "tb_top",
    "provider": "remote",
    "builds": ${BUILDS},
    "providers": {
        "remote": {
            "transport": {
                "kind": "ssh",
                "host": "${HOST}",
                "workdir": "${REMOTE_WORK}",
                "ssh_opts": [
                    "-o", "ControlMaster=auto",
                    "-o", "ControlPersist=120",
                    "-o", "ControlPath=${CTL_PATH}",
                ],
            },
            "sync": {"kind": "rsync"},
            "launcher": {"kind": "make"},
            "setup": '${SETUP}',
        },
    },
}
EOF

run_single_case
