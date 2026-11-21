"""Built-in Icarus Verilog backend (S-04, S-10)."""

from __future__ import annotations

from solouvm.backends.base import Backend

# 不确定：各版本的确切选项需逐一核对（S-10）
IVERILOG = Backend(
    name="iverilog",
    build="iverilog -g2012 -f {filelist} -s {top} -o {simv}",
    run="vvp {simv}",
    simv="simv.vvp",
    seed="+SOLOUVM_SEED={seed}",
    wave="+dumpfile={job_dir}/wave.vcd",
    cov_build="",
    cov_run="",
    compile_only=(
        "iverilog -g2012 -f {filelist_tb} {fake_top} -s solouvm_fake_top -o {check_dir}/fake.vvp"
    ),
    error_regex=(r"^ERROR:", r"syntax error", r": error:"),
    license_regex=(),
    time_regex=r"\$finish called at (\d+)",
    version="iverilog -V",
)
