"""Built-in Synopsys VCS backend (S-01, S-10)."""

from __future__ import annotations

from solouvm.backends.base import Backend

# 不确定：各版本的确切选项需逐一核对（S-10）
VCS = Backend(
    name="vcs",
    build=(
        "vcs -full64 -sverilog -ntb_opts uvm-1.2 -timescale=1ns/1ps"
        " -f {filelist} -top {top} -o {simv}"
    ),
    run="{simv}",
    simv="simv",
    seed="+ntb_random_seed={seed}",
    wave="+fsdb+all=on",
    cov_build="-cm line+cond+fsm+tgl+branch",
    cov_run="-cm line+cond+fsm+tgl+branch -cm_dir {job_dir}/cov.vdb",
    # fast-compile.md §3.3：{fake_top} 是 fake_top 文件，顶层模块固定为 solouvm_fake_top
    compile_only=(
        "vcs -full64 -sverilog -ntb_opts uvm-1.2"
        " -f {filelist_tb} {fake_top} -top solouvm_fake_top -o {check_dir}/simv"
    ),
    error_regex=(r"^Error-\[", r"^Fatal:", r"Offending '"),
    license_regex=("Failed to obtain license",),
    time_regex=r"\$finish.* at time (\d+)",
    version="vcs -ID",
)
