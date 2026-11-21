from solouvm.uvm import Agent, Env, Field, Interface, Signal

AXI = Interface(
    name="axi",
    clock="aclk",
    reset=("aresetn", "low"),
    signals=[
        Signal("awaddr", 32, "m2s"),
        Signal("awlen", 8, "m2s"),
        Signal("awvalid", 1, "m2s"),
        Signal("awready", 1, "s2m"),
    ],
)

ENVS = [
    Env(
        name="axi",
        agents=[
            Agent(
                name="axi_mst",
                interface=AXI,
                role="master",
                fields=[
                    Field("addr", 32),
                    Field("len", 8, constraint="len inside {[0:255]}"),
                    Field("burst", "bit [1:0]"),
                ],
                coverage=True,
            ),
            Agent(
                name="axi_slv_mon",
                interface=AXI,
                role="monitor",
                like="axi_mst",
            ),
        ],
        scoreboard=True,
        ref_model=False,
    ),
]
