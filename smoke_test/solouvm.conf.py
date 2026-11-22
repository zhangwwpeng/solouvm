CFG = {
    "simulator": "fake",
    "top": "tb_top",
    "sim": {
        "build": "bash {root}/demo_build.sh",
        "run": "bash {root}/demo_sim.sh",
    },
    "job": {
        "pre": 'echo "[job.pre] preparing workdir $(pwd)"',
        "post": 'echo "[job.post] done workdir $(pwd)"',
    },
}
