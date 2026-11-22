"""Project initialization: ``solouvm init`` (Z-01, U-03, AI-03, AI-12)."""

from __future__ import annotations

import textwrap
from pathlib import Path

from solouvm.backends import get_backend
from solouvm.collect.cases import collect_cases
from solouvm.collect.files import collect_files, load_filelist
from solouvm.config.load import load_config
from solouvm.errors import ConfigError
from solouvm.gen.makefile import render_root_makefile
from solouvm.model import validate_records
from solouvm.uvmgen.pkgfiles import find_svh_inconsistencies, update_pkg_files
from solouvm.uvmgen.render import render_component
from solouvm.uvmgen.sample import inject_sample
from solouvm.uvmgen.spec import Agent, Env, Field, Interface, Signal, Tbspec, load_tbspec

SUPPORTED_AGENTS_FRAMEWORKS = ("claude",)

GITIGNORE_ENTRIES = (
    ".solouvm/",
    "solouvm.local.py",
    "results.json",
    "results.txt",
    "junit.xml",
    "*.orphan",
)

__all__ = ["GITIGNORE_ENTRIES", "SUPPORTED_AGENTS_FRAMEWORKS", "init_project"]


def _sample_spec() -> Tbspec:
    axi = Interface(
        name="axi",
        clock="aclk",
        reset=("aresetn", "low"),
        signals=(
            Signal("awaddr", 32, "m2s"),
            Signal("awlen", 8, "m2s"),
            Signal("awvalid", 1, "m2s"),
            Signal("awready", 1, "s2m"),
        ),
    )
    mst = Agent(
        name="axi_mst",
        interface=axi,
        role="master",
        fields=(
            Field("addr", 32),
            Field("len", 8, constraint="len inside {[0:255]}"),
            Field("burst", "bit [1:0]"),
        ),
        coverage=True,
    )
    mon = Agent(
        name="axi_slv_mon",
        interface=axi,
        role="monitor",
        like="axi_mst",
    )
    env = Env(
        name="axi",
        agents=(mst, mon),
        scoreboard=True,
        ref_model=False,
    )
    return Tbspec(envs=(env,), interfaces=(axi,), agents=(mst, mon))


def _render_conf(*, simulator: str, bare: bool) -> str:
    sys_doc = "[]" if bare else '["doc/verif/system.md"]'
    return textwrap.dedent(
        f"""\
        CFG = {{
            "simulator": {simulator!r},
            "top": "tb_top",
            "builds": {{
                "default": {{}},
            }},
            "regress": {{
                "seeds": "random:1",
                "jobs": 4,
                "retry": 0,
            }},
            "checks": {{
                "max_uvm_error": 0,
                "max_uvm_fatal": 0,
            }},
            "check": {{
                "slang_args": ["--timescale", "1ns/1ps"],
            }},
            "case_dirs": [],
            "filelist_dirs": [],
            "system_doc": {sys_doc},
        }}
        """
    )


def _render_filelist() -> str:
    return textwrap.dedent(
        """\
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
        """
    )


def _render_tbspec() -> str:
    return textwrap.dedent(
        """\
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
        """
    )


def _render_case(simulator: str) -> str:
    opts_line = '        opts=["+SOLOUVM_FAKE_SCB=AXI_SCB:10"],\n' if simulator == "fake" else ""
    return (
        "from solouvm import Case, Expect\n\n"
        "CASES = [\n"
        "    Case(\n"
        '        name="smoke",\n'
        '        uvm_test="axi_smoke_test",\n'
        f"{opts_line}"
        '        stage="wip",\n'
        '        intent="Verify basic AXI master-to-slave transfer and scoreboard comparison.",\n'
        "        method=(\n"
        '            "Run axi_smoke_test via axi_base_vseq; "\n'
        '            "AXI_SCB checks observed transactions."\n'
        "        ),\n"
        '        spec_refs=["AXI4 A3.1"],\n'
        "        expect=[\n"
        '            Expect(scoreboard="AXI_SCB", min=1),\n'
        '            Expect(sim_time_min="1us"),\n'
        "        ],\n"
        '        doc="""\n'
        "        Basic AXI smoke test generated by solouvm init.\n"
        '        """,\n'
        "    ),\n"
        "]\n"
    )


def _render_system_doc() -> str:
    return textwrap.dedent(
        """\
        # System Verification Guide

        ## 1. DUT 功能概述与接口

        - Sample DUT: `axi_slv_dummy` (`rtl/axi_slv_dummy.sv`)
        - Interface: `axi_if` (`aclk`, `aresetn`, `awaddr`, `awlen`, `awvalid`, `awready`)

        ## 2. 验证环境结构

        - Agents: `axi_mst` (master, coverage enabled) and `axi_slv_mon` (passive monitor)
        - Environment: `axi_env` with `axi_scb` scoreboard and `axi_vsqr` virtual sequencer
        - Tests: `verif/tests/axi/axi_base_test.sv`, `verif/tests/axi/axi_smoke_test.sv`

        ## 3. 日志约定

        - Component UVM message IDs use uppercase class names (`AXI_MST_DRV`, `AXI_SCB`, etc.).
        - Scoreboard summary format in `report_phase`:
          `[AXI_SCB] compared=<N> matched=<M> mismatched=<K> pending=<P>`
        - Virtual sequence start/end format:
          `[AXI_BASE_VSEQ] start` / `[AXI_BASE_VSEQ] done items=<N>`

        ## 4. 已知限制与不验证的部分

        - `axi_slv_dummy` is a minimal starter stub; replace it with the real RTL DUT.

        ## 5. 调试指引

        - Top-level testbench: `verif/tb/tb_top.sv` (`u_axi_if`, DUT `u_dut`).
        - Key signals: `tb_top.aclk`, `tb_top.aresetn`, `awvalid`, `awready`.
        """
    )


def _render_dummy_rtl() -> str:
    return textwrap.dedent(
        """\
        module axi_slv_dummy (
          input  logic        aclk,
          input  logic        aresetn,
          input  logic [31:0] awaddr,
          input  logic [7:0]  awlen,
          input  logic        awvalid,
          output logic        awready
        );
          always_ff @(posedge aclk or negedge aresetn) begin
            if (!aresetn) begin
              awready <= 1'b0;
            end else begin
              awready <= awvalid;
            end
          end
        endmodule : axi_slv_dummy
        """
    )


def _render_claude_triage_agent() -> str:
    return textwrap.dedent(
        """\
        ---
        name: solouvm-triage
        description: Independent triage reviewer for solouvm regression tasks.
        tools: Read, Grep, Glob, Bash(solouvm --role triage :*)
        ---

        You are an independent reviewer of verification results. Start by running:
          solouvm --role triage triage packet <task>

        Follow the instructions in the context packet strictly. Do NOT edit source files directly.
        All outputs must be written through:
        - `solouvm --role triage bug add ...`
        - `solouvm --role triage note add ...`
        - `solouvm --role triage triage write ... --doc -`

        When finished, summarize your verdict, confidence, and key evidence in one short paragraph.
        """
    )


def _update_gitignore(root: Path) -> bool:
    path = root / ".gitignore"
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        existing_lines = {line.rstrip("\r") for line in existing.splitlines()}
        missing = [entry for entry in GITIGNORE_ENTRIES if entry not in existing_lines]
        if not missing:
            return False
        prefix = existing if (not existing or existing.endswith("\n")) else f"{existing}\n"
        path.write_text(prefix + "\n".join(missing) + "\n", encoding="utf-8")
        return True
    path.write_text("\n".join(GITIGNORE_ENTRIES) + "\n", encoding="utf-8")
    return True


def _check_svh(root_path: Path) -> None:
    svh_issues = find_svh_inconsistencies(root_path)
    if svh_issues:
        raise ConfigError("; ".join(svh_issues), code="CFG_INVALID")


def _self_validate(root_path: Path, *, bare: bool) -> None:
    try:
        cfg = load_config(root_path / "solouvm.conf.py")
        load_tbspec(root_path)
        if not bare:
            collect_files(root_path, load_filelist(root_path))
            _check_svh(root_path)
        cases = collect_cases(cfg)
        for case in cases:
            validate_records(case)
    except Exception as exc:
        raise ConfigError(
            f"init generated an invalid project: {exc}",
            code="CFG_INVALID",
        ) from exc


def init_project(
    *,
    root: Path,
    simulator: str = "fake",
    bare: bool = False,
    agents: str | None = None,
) -> list[str]:
    """Initialize a starter solouvm project under ``root`` and self-validate it."""
    if agents is not None and agents not in SUPPORTED_AGENTS_FRAMEWORKS:
        supported = ", ".join(SUPPORTED_AGENTS_FRAMEWORKS)
        raise ConfigError(
            f"unsupported agents framework {agents!r}; supported: {supported}",
            code="CFG_INVALID",
        )
    get_backend(simulator)

    root_path = Path(root)
    targets: dict[Path, str] = {
        Path("solouvm.conf.py"): _render_conf(simulator=simulator, bare=bare),
        Path("filelist.py"): _render_filelist(),
        Path("tbspec.py"): _render_tbspec(),
        Path("case.py"): _render_case(simulator),
    }

    if agents == "claude":
        targets[Path(".claude") / "agents" / "solouvm-triage.md"] = _render_claude_triage_agent()

    if not bare:
        spec = _sample_spec()
        targets[Path("doc") / "verif" / "system.md"] = _render_system_doc()
        targets[Path("rtl") / "axi_slv_dummy.sv"] = _render_dummy_rtl()
        for ag in spec.agents:
            targets.update(render_component("agent", ag.name, spec))
        targets.update(render_component("env", "axi", spec))
        targets.update(render_component("test", "smoke", spec, env_name="axi"))
        targets = {rel: inject_sample(rel, content) for rel, content in targets.items()}
        if not (root_path / "Makefile").exists():
            targets[Path("Makefile")] = render_root_makefile()

    conflicts = sorted(rel.as_posix() for rel in targets if (root_path / rel).exists())
    if conflicts:
        raise ConfigError(
            "refusing to overwrite existing file(s): " + ", ".join(conflicts),
            code="CFG_INVALID",
        )

    created: list[str] = []
    for rel, content in sorted(targets.items(), key=lambda kv: kv[0].as_posix()):
        dest = root_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        created.append(rel.as_posix())

    if not bare:
        for pkg in ("axi_mst", "axi_slv_mon", "axi_env", "axi_test"):
            update_pkg_files(root_path, pkg)

    if _update_gitignore(root_path):
        created.append(".gitignore")

    _self_validate(root_path, bare=bare)
    return sorted(set(created))
