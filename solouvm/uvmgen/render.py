"""Render SystemVerilog UVM components from templates and ``tbspec`` (U-01, U-06)."""

from __future__ import annotations

import re
from collections.abc import Sequence
from importlib.resources import files as resource_files
from pathlib import Path
from string import Template

from solouvm.errors import ConfigError, InfraError
from solouvm.uvmgen.pkgfiles import pkg_files_relpath, render_pkg_files
from solouvm.uvmgen.regions import finalize_checksum
from solouvm.uvmgen.spec import (
    Agent,
    Env,
    Field,
    Interface,
    Signal,
    Tbspec,
    resolve_agent_fields,
)

TEMPLATE_STYLE = 1
TEMPLATE_VERSION = "1"
VALID_KINDS = ("agent", "env", "test", "seq", "vseq")

_IDENT_RE = re.compile(r"[a-z][a-z0-9_]*\Z", re.ASCII)
_TEMPLATES_PKG = "solouvm.uvmgen.templates.style1"

__all__ = [
    "TEMPLATE_STYLE",
    "TEMPLATE_VERSION",
    "VALID_KINDS",
    "render_component",
    "render_tb_top",
]


def _check_ident(label: str, value: str | None) -> str:
    if not isinstance(value, str) or not _IDENT_RE.fullmatch(value):
        raise ConfigError(
            f"{label} {value!r} must match [a-z][a-z0-9_]*",
            code="CFG_INVALID",
        )
    return value


def _placeholder_interface(name: str) -> Interface:
    return Interface(
        name=name,
        clock="clk",
        reset=("rst_n", "low"),
        signals=(
            Signal("data", 32, "m2s"),
            Signal("valid", 1, "m2s"),
            Signal("ready", 1, "s2m"),
        ),
    )


def _placeholder_agent(name: str) -> Agent:
    return Agent(
        name=name,
        interface=_placeholder_interface(name),
        role="master",
        fields=(Field("data", 32),),
    )


def _unpack_spec(
    spec: object,
) -> tuple[dict[str, Env], dict[str, Agent], dict[str, Interface]]:
    envs: dict[str, Env] = {}
    agents: dict[str, Agent] = {}
    ifaces: dict[str, Interface] = {}

    if spec is None:
        return envs, agents, ifaces

    items: Sequence[object]
    if isinstance(spec, Tbspec):
        items = [*spec.envs, *spec.agents, *spec.interfaces]
    elif isinstance(spec, (Env, Agent, Interface)):
        items = [spec]
    elif isinstance(spec, (list, tuple)):
        items = spec
    else:
        raise ConfigError(
            f"unsupported spec type {type(spec).__name__}",
            code="CFG_INVALID",
        )

    for item in items:
        if isinstance(item, Env):
            envs[item.name] = item
            for ag in item.agents:
                agents[ag.name] = ag
                ifaces[ag.interface.name] = ag.interface
        elif isinstance(item, Agent):
            agents[item.name] = item
            ifaces[item.interface.name] = item.interface
        elif isinstance(item, Interface):
            ifaces[item.name] = item
        else:
            raise ConfigError(
                f"unsupported spec item {type(item).__name__}",
                code="CFG_INVALID",
            )
    return envs, agents, ifaces


def _load_template(filename: str, templates_dir: Path | None) -> str:
    if templates_dir is not None:
        tdir = Path(templates_dir)
        if not tdir.is_dir():
            raise InfraError(
                f"custom templates directory does not exist: {tdir}",
                code="PROVIDER_FAILED",
            )
        override = tdir / filename
        if override.is_file():
            try:
                return override.read_text(encoding="utf-8")
            except OSError as exc:
                raise InfraError(
                    f"failed to read custom template {override}: {exc}",
                    code="PROVIDER_FAILED",
                ) from exc

    try:
        resource = resource_files(_TEMPLATES_PKG) / filename
        return resource.read_text(encoding="utf-8")
    except (FileNotFoundError, ModuleNotFoundError, OSError) as exc:
        raise InfraError(
            f"missing template {filename!r}: {exc}",
            code="PROVIDER_FAILED",
        ) from exc


def _render_template(
    filename: str,
    mapping: dict[str, str],
    templates_dir: Path | None,
) -> str:
    raw = _load_template(filename, templates_dir)
    template_name = Path(filename).stem
    values = {
        "template": template_name,
        "version": TEMPLATE_VERSION,
        "checksum": "00000000",
        **mapping,
    }
    try:
        rendered = Template(raw).substitute(values)
    except (KeyError, ValueError) as exc:
        raise InfraError(
            f"template {filename!r} substitution failed: {exc}",
            code="PROVIDER_FAILED",
        ) from exc
    return finalize_checksum(rendered)


_SV_PARAM_TYPE_PREFIXES = (
    "int ",
    "integer ",
    "type ",
    "bit ",
    "logic ",
    "reg ",
    "byte ",
    "shortint ",
    "longint ",
    "real ",
    "shortreal ",
    "string ",
    "time ",
)


def _format_interface_param(raw_param: str) -> str:
    stripped = raw_param.strip()
    if stripped.startswith("parameter "):
        return f"  {stripped}"
    if stripped.startswith(_SV_PARAM_TYPE_PREFIXES) or stripped.startswith(("bit[", "logic[")):
        return f"  parameter {stripped}"
    if "=" in stripped:
        return f"  parameter int {stripped}"
    return f"  parameter int {stripped} = 0"


def _render_interface(
    iface: Interface,
    roles_on_iface: set[str],
    templates_dir: Path | None,
) -> str:
    if iface.params:
        formatted = [_format_interface_param(param) for param in iface.params]
        params_decl = "#(\n" + ",\n".join(formatted) + "\n) "
    else:
        params_decl = ""

    sig_lines: list[str] = []
    for sig in iface.signals:
        if sig.width == 1:
            sig_lines.append(f"  logic {sig.name};")
        elif isinstance(sig.width, int):
            sig_lines.append(f"  logic [{sig.width - 1}:0] {sig.name};")
        elif sig.width.isidentifier():
            sig_lines.append(f"  logic [{sig.width}-1:0] {sig.name};")
        else:
            sig_lines.append(f"  logic [({sig.width})-1:0] {sig.name};")
    signal_decls = "\n".join(sig_lines) + ("\n" if sig_lines else "")

    rst_name = iface.reset[0]
    has_master = "master" in roles_on_iface or "slave" not in roles_on_iface
    has_slave = "slave" in roles_on_iface

    def _clocking_block(cb_name: str, mp_name: str, out_dir: str | None) -> list[str]:
        lines = [f"  clocking {cb_name} @(posedge {iface.clock});"]
        for sig in iface.signals:
            direction = "output" if (out_dir is not None and sig.dir == out_dir) else "input"
            lines.append(f"    {direction} {sig.name};")
        lines.append(f"  endclocking : {cb_name}")
        lines.append(
            f"  modport {mp_name} (clocking {cb_name}, input {iface.clock}, input {rst_name});"
        )
        return lines

    cb_lines: list[str] = []
    if has_master:
        cb_lines.extend(_clocking_block("drv_cb", "drv_mp", "m2s"))
        if has_slave:
            cb_lines.append("")
            cb_lines.extend(_clocking_block("slv_drv_cb", "slv_drv_mp", "s2m"))
    else:
        cb_lines.extend(_clocking_block("drv_cb", "drv_mp", "s2m"))
    cb_lines.append("")
    cb_lines.extend(_clocking_block("mon_cb", "mon_mp", None))
    clocking_blocks = "\n".join(cb_lines)

    return _render_template(
        "agent_if.sv",
        {
            "iface": iface.name,
            "iface_upper": iface.name.upper(),
            "params_decl": params_decl,
            "clock": iface.clock,
            "reset": rst_name,
            "signal_decls": signal_decls,
            "clocking_blocks": clocking_blocks,
        },
        templates_dir,
    )


def _render_agent_files(
    agent: Agent,
    agents_by_name: dict[str, Agent],
    templates_dir: Path | None,
) -> dict[Path, str]:
    iface = agent.interface
    fields = resolve_agent_fields(agent, agents_by_name)
    roles_on_iface = {
        ag.role for ag in agents_by_name.values() if ag.interface.name == iface.name
    } or {agent.role}

    field_decl_lines: list[str] = []
    field_macro_lines: list[str] = []
    constraint_lines: list[str] = []
    for fld in fields:
        prefix = "rand " if fld.rand else ""
        if isinstance(fld.type, int):
            decl_type = "bit" if fld.type == 1 else f"bit [{fld.type - 1}:0]"
            field_macro_lines.append(f"    `uvm_field_int({fld.name}, UVM_ALL_ON)")
        else:
            decl_type = fld.type
        field_decl_lines.append(f"  {prefix}{decl_type} {fld.name};")
        if fld.constraint:
            constraint_lines.append(f"  constraint c_{fld.name} {{ {fld.constraint}; }}")

    if agent.role == "slave":
        driven_dir = "s2m"
        if "master" in roles_on_iface:
            drv_mp, drv_cb = "slv_drv_mp", "slv_drv_cb"
        else:
            drv_mp, drv_cb = "drv_mp", "drv_cb"
    else:
        driven_dir = "m2s"
        drv_mp, drv_cb = "drv_mp", "drv_cb"

    if agent.role == "monitor":
        drive_lines = "    // monitor agent is passive (UVM_PASSIVE)"
    else:
        driven_sigs = [sig for sig in iface.signals if sig.dir == driven_dir]
        drive_lines = (
            "\n".join(f"    vif.{drv_mp}.{drv_cb}.{sig.name} <= '0;" for sig in driven_sigs)
            if driven_sigs
            else "    // no signals to drive"
        )

    sampled_sigs = list(iface.signals)
    sample_lines = (
        "\n".join(f"      // sample vif.mon_mp.mon_cb.{sig.name}" for sig in sampled_sigs)
        if sampled_sigs
        else "      // sample interface signals"
    )

    is_active = "UVM_PASSIVE" if agent.role == "monitor" else "UVM_ACTIVE"
    cov_enable = "1'b1" if agent.coverage else "1'b0"

    common = {
        "agent": agent.name,
        "agent_upper": agent.name.upper(),
        "iface": iface.name,
    }

    out: dict[Path, str] = {}
    out[Path("verif") / "agents" / iface.name / f"{iface.name}_if.sv"] = _render_interface(
        iface, roles_on_iface, templates_dir
    )

    agent_dir = Path("verif") / "agents" / agent.name
    out[agent_dir / f"{agent.name}_pkg.sv"] = _render_template(
        "agent_pkg.sv", common, templates_dir
    )
    out[agent_dir / f"{agent.name}_txn.sv"] = _render_template(
        "agent_txn.sv",
        {
            **common,
            "field_decls": "\n".join(field_decl_lines) + ("\n" if field_decl_lines else ""),
            "field_macros": "\n".join(field_macro_lines),
            "constraints": "\n".join(constraint_lines) + ("\n" if constraint_lines else ""),
        },
        templates_dir,
    )
    out[agent_dir / f"{agent.name}_cfg.sv"] = _render_template(
        "agent_cfg.sv",
        {**common, "is_active": is_active, "cov_enable": cov_enable},
        templates_dir,
    )
    out[agent_dir / f"{agent.name}_sqr.sv"] = _render_template(
        "agent_sqr.sv", common, templates_dir
    )
    out[agent_dir / f"{agent.name}_drv.sv"] = _render_template(
        "agent_drv.sv",
        {**common, "drv_mp": drv_mp, "drv_cb": drv_cb, "drive_lines": drive_lines},
        templates_dir,
    )
    out[agent_dir / f"{agent.name}_mon.sv"] = _render_template(
        "agent_mon.sv",
        {**common, "sample_lines": sample_lines},
        templates_dir,
    )
    out[agent_dir / f"{agent.name}_cov.sv"] = _render_template(
        "agent_cov.sv", common, templates_dir
    )
    out[agent_dir / f"{agent.name}_agent.sv"] = _render_template(
        "agent_agent.sv", common, templates_dir
    )
    out[agent_dir / "seq" / f"{agent.name}_base_seq.sv"] = _render_template(
        "agent_base_seq.sv", common, templates_dir
    )

    pkg_rel_files = [
        path.relative_to("verif").as_posix()
        for path in out
        if path.parts[:2] == ("verif", "agents")
        and path.parts[2] == agent.name
        and not path.name.endswith("_pkg.sv")
        and not path.name.endswith("_if.sv")
    ]
    out[Path(pkg_files_relpath(agent.name))] = render_pkg_files(agent.name, pkg_rel_files)
    return out


def render_tb_top(
    envs: Sequence[Env],
    *,
    root: Path | None = None,
    templates_dir: Path | None = None,
) -> dict[Path, str]:
    """Render ``verif/tb/tb_top.sv`` and ``verif/tb/binds.sv`` for ``envs``."""
    pkg_imports = "".join(f"  import {env.name}_test_pkg::*;\n" for env in envs)

    seen_ifaces: dict[str, Interface] = {}
    vif_lines: list[str] = []
    for env in envs:
        for ag in env.agents:
            iface = ag.interface
            seen_ifaces.setdefault(iface.name, iface)
            vif_lines.append(
                f"    uvm_config_db #(virtual {iface.name}_if)::set(null, "
                f'"uvm_test_top.env.{ag.name}*", "vif", u_{iface.name}_if);'
            )

    clk_rst_decl_lines: list[str] = []
    clk_rst_gen_lines: list[str] = []
    if_inst_lines: list[str] = []
    clocks = {iface.clock for iface in seen_ifaces.values()}
    resets: dict[str, str] = {}
    for iface in seen_ifaces.values():
        reset, polarity = iface.reset
        if reset in clocks or (reset in resets and resets[reset] != polarity):
            raise ConfigError(
                f"incompatible shared clock/reset declaration for {reset!r}", code="CFG_INVALID"
            )
        resets[reset] = polarity
    if (clocks | resets.keys()) & {f"u_{name}_if" for name in seen_ifaces}:
        raise ConfigError("clock/reset collides with an interface instance", code="CFG_INVALID")
    seen_signals: set[str] = set()
    for iface in seen_ifaces.values():
        rst_name, rst_active = iface.reset
        if iface.clock not in seen_signals:
            seen_signals.add(iface.clock)
            clk_rst_decl_lines.append(f"  logic {iface.clock};")
            clk_rst_gen_lines.extend(
                [
                    "  initial begin",
                    f"    {iface.clock} = 1'b0;",
                    f"    forever #5ns {iface.clock} = ~{iface.clock};",
                    "  end",
                    "",
                ]
            )
        if rst_name not in seen_signals:
            seen_signals.add(rst_name)
            clk_rst_decl_lines.append(f"  logic {rst_name};")
            init_val = "1'b1" if rst_active == "high" else "1'b0"
            release_val = "1'b0" if rst_active == "high" else "1'b1"
            clk_rst_gen_lines.extend(
                [
                    "  initial begin",
                    f"    {rst_name} = {init_val};",
                    "    #20ns;",
                    f"    {rst_name} = {release_val};",
                    "  end",
                    "",
                ]
            )
        if_inst_lines.append(
            f"  {iface.name}_if u_{iface.name}_if (.{iface.clock}({iface.clock}), "
            f".{rst_name}({rst_name}));"
        )

    out: dict[Path, str] = {
        Path("verif") / "tb" / "tb_top.sv": _render_template(
            "tb_top.sv",
            {
                "pkg_imports": pkg_imports,
                "clk_rst_decls": "\n".join(clk_rst_decl_lines)
                + ("\n" if clk_rst_decl_lines else ""),
                "clk_rst_gen": "\n".join(clk_rst_gen_lines),
                "if_insts": "\n".join(if_inst_lines) + ("\n" if if_inst_lines else ""),
                "vif_sets": "\n".join(vif_lines),
            },
            templates_dir,
        ),
        Path("verif") / "tb" / "binds.sv": _render_template("binds.sv", {}, templates_dir),
    }
    return _prefix_root(out, root)


def _render_env_files(
    env: Env,
    *,
    templates_dir: Path | None,
    include_tb_top: bool = True,
) -> dict[Path, str]:
    agent_imports = "".join(f"  import {ag.name}_pkg::*;\n" for ag in env.agents)
    agent_cfg_decls = "".join(f"  {ag.name}_cfg {ag.name};\n" for ag in env.agents)
    agent_cfg_creates = "".join(
        f'    if ({ag.name} == null) {ag.name} = {ag.name}_cfg::type_id::create("{ag.name}");\n'
        for ag in env.agents
    )
    fifo_decls = "".join(
        f"  uvm_tlm_analysis_fifo #({ag.name}_txn) {ag.name}_fifo;\n" for ag in env.agents
    )
    fifo_creates = "".join(
        f'    {ag.name}_fifo = new("{ag.name}_fifo", this);\n' for ag in env.agents
    )
    sqr_decls = "".join(f"  {ag.name}_sqr {ag.name};\n" for ag in env.agents)

    agent_decls = "".join(f"  {ag.name}_agent {ag.name};\n" for ag in env.agents)
    opt_decl_lines: list[str] = []
    if env.scoreboard:
        opt_decl_lines.append(f"  {env.name}_scb scb;")
    if env.ref_model:
        opt_decl_lines.append(f"  {env.name}_ref ref_model;")
    if env.coverage:
        opt_decl_lines.append(f"  {env.name}_cov cov;")
    optional_decls = ("\n".join(opt_decl_lines) + "\n") if opt_decl_lines else ""

    agent_build_lines: list[str] = []
    for ag in env.agents:
        agent_build_lines.append(
            f'    uvm_config_db #({ag.name}_cfg)::set(this, "{ag.name}", "cfg", cfg.{ag.name});'
        )
        agent_build_lines.append(
            f'    {ag.name} = {ag.name}_agent::type_id::create("{ag.name}", this);'
        )
    agent_builds = ("\n".join(agent_build_lines) + "\n") if agent_build_lines else ""

    opt_build_lines: list[str] = []
    if env.scoreboard:
        opt_build_lines.append("    if (cfg.has_scoreboard) begin")
        opt_build_lines.append(f'      scb = {env.name}_scb::type_id::create("scb", this);')
        opt_build_lines.append("    end")
    if env.ref_model:
        opt_build_lines.append("    if (cfg.has_ref_model) begin")
        opt_build_lines.append(
            f'      ref_model = {env.name}_ref::type_id::create("ref_model", this);'
        )
        opt_build_lines.append("    end")
    if env.coverage:
        opt_build_lines.append("    if (cfg.has_coverage) begin")
        opt_build_lines.append(f'      cov = {env.name}_cov::type_id::create("cov", this);')
        opt_build_lines.append("    end")
    optional_builds = ("\n".join(opt_build_lines) + "\n") if opt_build_lines else ""

    agent_connect_lines = [
        f"    vsqr.{ag.name} = {ag.name}.sqr;" for ag in env.agents if ag.role != "monitor"
    ]
    agent_connects = ("\n".join(agent_connect_lines) + "\n") if agent_connect_lines else ""

    opt_connect_lines: list[str] = []
    if env.scoreboard and env.agents:
        opt_connect_lines.append("    if (scb != null) begin")
        for ag in env.agents:
            opt_connect_lines.append(
                f"      {ag.name}.ap.connect(scb.{ag.name}_fifo.analysis_export);"
            )
        opt_connect_lines.append("    end")
    optional_connects = ("\n".join(opt_connect_lines) + "\n") if opt_connect_lines else ""

    common = {"env": env.name, "env_upper": env.name.upper()}
    out: dict[Path, str] = {}
    env_dir = Path("verif") / "envs" / env.name
    out[env_dir / f"{env.name}_env_pkg.sv"] = _render_template(
        "env_pkg.sv",
        {**common, "agent_imports": agent_imports},
        templates_dir,
    )
    out[env_dir / f"{env.name}_env_cfg.sv"] = _render_template(
        "env_cfg.sv",
        {
            **common,
            "agent_cfg_decls": agent_cfg_decls,
            "agent_cfg_creates": agent_cfg_creates,
            "has_scb": "1'b1" if env.scoreboard else "1'b0",
            "has_ref": "1'b1" if env.ref_model else "1'b0",
            "has_cov": "1'b1" if env.coverage else "1'b0",
        },
        templates_dir,
    )
    if env.scoreboard:
        out[env_dir / f"{env.name}_scb.sv"] = _render_template(
            "env_scb.sv",
            {**common, "fifo_decls": fifo_decls, "fifo_creates": fifo_creates},
            templates_dir,
        )
    if env.ref_model:
        out[env_dir / f"{env.name}_ref.sv"] = _render_template("env_ref.sv", common, templates_dir)
    if env.coverage:
        out[env_dir / f"{env.name}_cov.sv"] = _render_template("env_cov.sv", common, templates_dir)
    out[env_dir / f"{env.name}_vsqr.sv"] = _render_template(
        "env_vsqr.sv",
        {**common, "sqr_decls": sqr_decls},
        templates_dir,
    )
    out[env_dir / f"{env.name}_env.sv"] = _render_template(
        "env_env.sv",
        {
            **common,
            "agent_decls": agent_decls,
            "optional_decls": optional_decls,
            "agent_builds": agent_builds,
            "optional_builds": optional_builds,
            "agent_connects": agent_connects,
            "optional_connects": optional_connects,
        },
        templates_dir,
    )
    out[env_dir / "vseq" / f"{env.name}_base_vseq.sv"] = _render_template(
        "env_base_vseq.sv", common, templates_dir
    )

    env_pkg_name = f"{env.name}_env"
    env_rel_files = [
        path.relative_to("verif").as_posix()
        for path in out
        if path.parts[:3] == ("verif", "envs", env.name) and not path.name.endswith("_pkg.sv")
    ]
    out[Path(pkg_files_relpath(env_pkg_name))] = render_pkg_files(env_pkg_name, env_rel_files)

    test_dir = Path("verif") / "tests" / env.name
    out[test_dir / f"{env.name}_test_pkg.sv"] = _render_template(
        "test_pkg.sv",
        {**common, "agent_imports": agent_imports},
        templates_dir,
    )
    base_test_path = test_dir / f"{env.name}_base_test.sv"
    out[base_test_path] = _render_template("base_test.sv", common, templates_dir)
    test_pkg_name = f"{env.name}_test"
    out[Path(pkg_files_relpath(test_pkg_name))] = render_pkg_files(
        test_pkg_name, [base_test_path.relative_to("verif").as_posix()]
    )

    if include_tb_top:
        out.update(render_tb_top([env], templates_dir=templates_dir))
    return out


def _prefix_root(mapping: dict[Path, str], root: Path | None) -> dict[Path, str]:
    if root is None:
        return dict(sorted(mapping.items(), key=lambda kv: kv[0].as_posix()))
    base = Path(root)
    return {
        base / rel: content
        for rel, content in sorted(mapping.items(), key=lambda kv: kv[0].as_posix())
    }


def render_component(
    kind: str,
    name: str,
    spec: object = None,
    *,
    root: Path | None = None,
    env_name: str | None = None,
    agent_name: str | None = None,
    agents: list[str] | None = None,
    templates_dir: Path | None = None,
) -> dict[Path, str]:
    """Render SV component files for ``kind`` (``agent``, ``env``, ``test``, ``seq``, ``vseq``)."""
    if kind not in VALID_KINDS:
        raise ConfigError(
            f"unknown component kind {kind!r}; allowed: {', '.join(VALID_KINDS)}",
            code="CFG_INVALID",
        )
    _check_ident("component name", name)
    envs_by_name, agents_by_name, _ = _unpack_spec(spec)

    if kind == "agent":
        agent_spec = agents_by_name.get(name) or _placeholder_agent(name)
        rendered = _render_agent_files(agent_spec, agents_by_name, templates_dir)
        return _prefix_root(rendered, root)

    if kind == "env":
        env_spec = envs_by_name.get(name)
        if env_spec is None:
            if not agents:
                raise ConfigError(
                    f"env {name!r} not found in tbspec and --agents was not provided",
                    code="CFG_INVALID",
                )
            resolved_agents: list[Agent] = []
            for ag_name in agents:
                _check_ident("agent name", ag_name)
                resolved_agents.append(agents_by_name.get(ag_name) or _placeholder_agent(ag_name))
            env_spec = Env(name=name, agents=tuple(resolved_agents))
        elif agents:
            requested = [_check_ident("agent name", a) for a in agents]
            by_name = {a.name: a for a in env_spec.agents}
            selected: list[Agent] = []
            for ag_name in requested:
                selected.append(
                    by_name.get(ag_name)
                    or agents_by_name.get(ag_name)
                    or _placeholder_agent(ag_name)
                )
            env_spec = Env(
                name=env_spec.name,
                agents=tuple(selected),
                scoreboard=env_spec.scoreboard,
                ref_model=env_spec.ref_model,
                coverage=env_spec.coverage,
            )
        rendered = _render_env_files(env_spec, templates_dir=templates_dir, include_tb_top=True)
        return _prefix_root(rendered, root)

    if kind == "test":
        env_id = _check_ident("env_name", env_name)
        path = Path("verif") / "tests" / env_id / f"{env_id}_{name}_test.sv"
        content = _render_template(
            "test.sv",
            {
                "env": env_id,
                "env_upper": env_id.upper(),
                "test": name,
                "test_upper": name.upper(),
            },
            templates_dir,
        )
        return _prefix_root({path: content}, root)

    if kind == "seq":
        agent_id = _check_ident("agent_name", agent_name)
        path = Path("verif") / "agents" / agent_id / "seq" / f"{agent_id}_{name}_seq.sv"
        content = _render_template(
            "agent_seq.sv",
            {
                "agent": agent_id,
                "agent_upper": agent_id.upper(),
                "seq": name,
                "seq_upper": name.upper(),
            },
            templates_dir,
        )
        return _prefix_root({path: content}, root)

    # kind == "vseq"
    env_id = _check_ident("env_name", env_name)
    path = Path("verif") / "envs" / env_id / "vseq" / f"{env_id}_{name}_vseq.sv"
    content = _render_template(
        "env_vseq.sv",
        {
            "env": env_id,
            "env_upper": env_id.upper(),
            "vseq": name,
            "vseq_upper": name.upper(),
        },
        templates_dir,
    )
    return _prefix_root({path: content}, root)
