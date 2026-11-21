"""Built-in Verilator backend (S-05, S-10)."""

from __future__ import annotations

from solouvm.backends.base import Backend

# 不确定：各版本的确切选项需逐一核对（S-10）
VERILATOR = Backend(
    name="verilator",
    build="verilator --binary --sv -f {filelist} --top-module {top} --Mdir {build_dir} -o {simv}",
    run="{simv}",
    simv="Vsim",
    seed="+verilator+seed+{seed}",
    wave="--trace +trace_file={job_dir}/wave.vcd",
    cov_build="--coverage",
    cov_run="+verilator+coverage+file+{job_dir}/coverage.dat",
    compile_only=(
        "verilator --lint-only -Wall --sv -f {filelist_tb} {fake_top} --top-module solouvm_fake_top"
    ),
    error_regex=(r"^%Error", r"Assertion failed"),
    license_regex=(),
    time_regex=r"-\$ .*:\$finish at (\d+)",
    version="verilator --version",
)
