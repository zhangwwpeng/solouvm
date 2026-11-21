"""Built-in Cadence Xcelium (xrun) backend (S-02, S-10)."""

from __future__ import annotations

from solouvm.backends.base import Backend

# 不确定：各版本的确切选项需逐一核对（S-10）
XRUN = Backend(
    name="xrun",
    build="xrun -uvm -f {filelist} -top {top} -elaborate",
    run="xrun -R",
    simv="xrun.key",
    seed="-svseed {seed}",
    wave="-input wave.tcl",
    cov_build="-coverage all",
    cov_run="-coverage all -covworkdir {job_dir}/cov_work",
    # fast-compile.md §3.3 的示意命令
    compile_only=(
        "xrun -64bit -sv -uvm -f {filelist_tb} {fake_top} -top solouvm_fake_top"
        " -elaborate -xmlibdirpath {check_dir}"
    ),
    error_regex=(r"^xmsim: \*E,", r"^xmsim: \*F,", r"^ncsim: \*E,"),
    license_regex=("Failed to obtain license",),
    time_regex=r"Simulation complete via \$finish.* at time (\d+)",
    version="xrun -version",
)
