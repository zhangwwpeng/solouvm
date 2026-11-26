"""Built-in Xezim open-source SystemVerilog/UVM simulator backend (S-04, S-10)."""

from __future__ import annotations

from solouvm.backends.base import Backend

# 不确定：各版本的确切选项需逐一核对（S-10）；参见 https://github.com/aionhw/xezim
XEZIM = Backend(
    name="xezim",
    build="xezim --compile -F {filelist} -s {top}",
    run="xezim -F {filelist} -s {top}",
    simv="xezim.cache",
    seed="-svseed {seed}",
    wave="--fst wave.fst",
    cov_build="--code-coverage",
    cov_run="--code-coverage",
    compile_only="xezim --compile -F {filelist_tb} {fake_top} -s solouvm_fake_top",
    error_regex=(r"(?i)^error:", r"(?i)^fatal:", r": error:", r"Assertion failed"),
    license_regex=(),
    time_regex=r"(?:Simulation finished|\$finish).* at (?:time )?(\d+(?:\.\d+)?\s*\w*)",
    version="xezim -V",
)
