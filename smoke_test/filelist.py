FILES = {
    "rtl": [
        "rtl/**/*.sv",
    ],
    "tb": [
        "+incdir+verif",
        "verif/agents/*/*_if.sv",
        "verif/agents/*/*_pkg.sv",
        "verif/envs/*/*_env_pkg.sv",
        "verif/tests/*/*_test_pkg.sv",
    ],
    "top": [
        "verif/tb/tb_top.sv",
        "verif/tb/binds.sv",
    ],
}
