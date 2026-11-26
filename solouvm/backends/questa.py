"""Built-in Siemens Questa / ModelSim backend (S-03, S-10)."""

from __future__ import annotations

from solouvm.backends.base import Backend

# 不确定：各版本的确切选项需逐一核对（S-10）
QUESTA = Backend(
    name="questa",
    build=(
        "qrun -64 -sv -optimize +acc -outdir qrun.out -f {filelist} -top {top} -o {simv_name}"
    ),
    run=(
        "qrun -64 -c -simulate -outdir {build_dir}/qrun.out -o {simv_name}"
        " -do 'run -all; quit -f'"
    ),
    simv="solouvm_opt",
    seed="-sv_seed {seed}",
    wave="-wlf {job_dir}/wave.wlf -do 'log -r /*; run -all; quit -f'",
    cov_build="-coverage +cover=bcesft",
    cov_run="-coverage -do 'coverage save -onexit {job_dir}/cov.ucdb'",
    compile_only="qrun -64 -sv -compile -outdir {check_dir}/qrun.out -f {filelist_tb} {fake_top}",
    error_regex=(r"^(?:#\s*)?\*\* Error", r"^(?:#\s*)?\*\* Fatal"),
    license_regex=("Failed to obtain license", "Unable to checkout a license"),
    time_regex=r"(?:#\s*)?(?:\*\* Note: \$finish\s*:\s*.*)?Time:\s*(\d+(?:\.\d+)?\s*[a-zA-Z]+)",
    version="qrun -version",
)
