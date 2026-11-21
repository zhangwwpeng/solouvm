"""Built-in fake simulator backend used for self-tests and ``solouvm init`` (S-06, Q-01)."""

from __future__ import annotations

from pathlib import Path

from solouvm.backends.base import Backend

FAKE_SIM_NAME = "fake_sim.sh"
# 生成阶段把 fake_sim.sh 渲染到这里
_FAKE_SIM = "bash {root}/.solouvm/gen/bin/" + FAKE_SIM_NAME

FAKE = Backend(
    name="fake",
    build=_FAKE_SIM,
    run=_FAKE_SIM,
    simv=FAKE_SIM_NAME,
    seed="+ntb_random_seed={seed}",
    wave="+fake_wave",
    cov_build="",
    cov_run="",
    compile_only=_FAKE_SIM,
    error_regex=(r"^FAKE-ERROR:",),
    license_regex=("Failed to obtain license",),
    time_regex=r"sim time: (\d+)ns",
    version=_FAKE_SIM + " +SOLOUVM_FAKE_MODE=version",
)


def fake_sim_source() -> str:
    """Return the text of the bundled ``fake_sim.sh``."""
    # 不依赖当前工作目录
    return (Path(__file__).parent / FAKE_SIM_NAME).read_text(encoding="utf-8")
