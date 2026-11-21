"""Built-in Siemens Questa / ModelSim backend (S-03, S-10)."""

from __future__ import annotations

from solouvm.backends.base import Backend

# 不确定：各版本的确切选项需逐一核对（S-10）
QUESTA = Backend(
    name="questa",
    build="vlib work && vlog -work work -sv -f {filelist} && vopt -work work {top} -o {simv}",
    run="vsim -c -lib {build_dir}/work {simv} -do 'run -all; quit -f'",
    simv="solouvm_opt",
    seed="-sv_seed {seed}",
    wave="-wlf {job_dir}/wave.wlf -do 'log -r /*; run -all; quit -f'",
    cov_build="+cover=bcesft",
    cov_run="-coverage -covdb {job_dir}/cov.ucdb",
    compile_only=(
        "vlib {check_dir}/work && vlog -work {check_dir}/work -sv -f {filelist_tb} {fake_top}"
        " && vopt -work {check_dir}/work solouvm_fake_top -o fake_opt"
    ),
    error_regex=(r"^\*\* Error", r"^\*\* Fatal"),
    license_regex=("Failed to obtain license", "Unable to checkout a license"),
    time_regex=r"\*\* Note: \$finish\s*:\s*.*Time:\s*(\S+)",
    version="vsim -version",
)
