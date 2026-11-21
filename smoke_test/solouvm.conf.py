from pathlib import Path

_DIR = Path(__file__).resolve().parent

CFG = {
    "simulator": "fake",
    "top": "tb_top",
    "sim": {
        "build": f"bash {_DIR}/demo_build.sh",
        "run": f"bash {_DIR}/demo_sim.sh",
    },
    "job": {
        "pre": 'echo "[job.pre] preparing workdir $(pwd)"',
        "post": 'echo "[job.post] done workdir $(pwd)"',
    },
}
