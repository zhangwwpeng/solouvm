"""Minimal, runnable solouvm demo.

Uses a fake simulator (tb/fake_sim.py) so the demo works with no EDA tool:

    solouvm -c solouvm.conf.py regress
"""

CFG = {
    "project": "demo",
    "simulator": "demo",  # not a builtin: the sim section below defines it
    "top": "tb_top",
    "incdirs": ["tb"],
    "defines": ["SIM"],
    "regress": {"seeds": [1, 2], "retry": 0, "jobs": 2},
    "run": {"timeout": "2m"},
    "checks": {
        "max_uvm_error": 0,
        "max_uvm_fatal": 0,
        "fail_regex": ["TEST FAILED"],
    },
    "sim": {
        "build": ["python3", "{root}/tb/fake_sim.py", "--build"],
        "run": ["python3", "{root}/tb/fake_sim.py"],
    },
}
