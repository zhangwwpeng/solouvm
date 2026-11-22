"""Tests for solouvm.uvm and solouvm.uvmgen (task 23, U-01..U-11)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from solouvm import Case, Expect
from solouvm.collect.cases import collect_cases
from solouvm.config.load import load_config
from solouvm.errors import ConfigError, InfraError
from solouvm.init import init_project
from solouvm.uvm import Agent, Env, Field, Interface, Signal
from solouvm.uvmgen import (
    compute_checksum,
    create_component,
    find_svh_inconsistencies,
    lint_style,
    load_tbspec,
    merge_regions,
    parse_header,
    render_component,
    update_pkg_files,
    user_regions,
    uvm_diff,
    uvm_sync,
)


def _sample_spec() -> list[Env]:
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
        params=("ADDR_W = 32",),
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
    mon = Agent(name="axi_slv_mon", interface=axi, role="monitor", like="axi_mst")
    return [
        Env(
            name="axi",
            agents=(mst, mon),
            scoreboard=True,
            ref_model=True,
            coverage=True,
        )
    ]


def _write_tbspec(root: Path, body: str) -> Path:
    path = root / "tbspec.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# ---------------------------------------------------------------- spec & tbspec.py


def test_uvm_public_reexports() -> None:
    sig = Signal("awaddr", 32, "m2s")
    iface = Interface("axi", "aclk", ("aresetn", "low"), [sig])
    fld = Field("addr", 32)
    ag = Agent("axi_mst", iface, "master", [fld], coverage=True)
    env = Env("axi", [ag])
    assert isinstance(env.agents, tuple)
    assert isinstance(iface.signals, tuple)
    assert isinstance(ag.fields, tuple)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Signal("BadName", 32, "m2s"),
        lambda: Signal("awaddr", 0, "m2s"),
        lambda: Signal("awaddr", True, "m2s"),  # type: ignore[arg-type]
        lambda: Signal("awaddr", 32, "x"),
        lambda: Interface("axi", "aclk", ("aresetn", "active"), (Signal("d", 1, "m2s"),)),
        lambda: Interface("axi", "1clk", ("aresetn", "low"), (Signal("d", 1, "m2s"),)),
        lambda: Field("addr", 0),
        lambda: Field("addr", True),
        lambda: Agent("axi_mst", Interface("axi", "clk", ("rst", "low"), ()), "bogus"),
        lambda: Env(
            "axi",
            (
                Agent("a", Interface("i", "clk", ("rst", "low"), ()), "master"),
                Agent("a", Interface("i", "clk", ("rst", "low"), ()), "slave"),
            ),
        ),
        lambda: Env(
            "axi",
            (Agent("a", Interface("i", "clk", ("rst", "low"), ()), "master", like="nope"),),
        ),
    ],
)
def test_spec_validation_errors(factory) -> None:
    with pytest.raises(ConfigError) as exc_info:
        factory()
    assert exc_info.value.code == "CFG_INVALID"


def test_tbspec_duplicate_env_and_singular_naming(tmp_path: Path) -> None:
    _write_tbspec(
        tmp_path,
        """\
        from solouvm.uvm import Agent, Env, Interface, Signal

        INTERFACE = Interface("apb", "pclk", ("presetn", "low"), [Signal("paddr", 16, "m2s")])
        ENV = Env("apb", [Agent("apb_mst", INTERFACE, "master")])
        """,
    )
    spec = load_tbspec(tmp_path)
    assert len(spec.ENVS) == 1
    assert len(spec.INTERFACES) == 1
    assert len(spec.AGENTS) == 1
    assert spec.ENVS[0].name == "apb"

    dup_dir = tmp_path / "dup"
    dup_dir.mkdir()
    _write_tbspec(
        dup_dir,
        """\
        from solouvm.uvm import Agent, Env, Interface, Signal

        I = Interface("apb", "pclk", ("presetn", "low"), [Signal("paddr", 16, "m2s")])
        ENVS = [
            Env("apb", [Agent("apb_mst", I, "master")]),
            Env("apb", [Agent("apb_slv", I, "slave")]),
        ]
        """,
    )
    with pytest.raises(ConfigError) as exc_info:
        load_tbspec(dup_dir)
    assert exc_info.value.code == "CFG_INVALID"


def test_tbspec_missing_raises_cfg_not_found(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_tbspec(tmp_path)
    assert exc_info.value.code == "CFG_NOT_FOUND"


# ---------------------------------------------------------------- regions & checksum


def test_regions_extract_checksum_and_merge() -> None:
    rendered = render_component("agent", "axi_mst")
    drv_path = Path("verif/agents/axi_mst/axi_mst_drv.sv")
    drv_text = rendered[drv_path]

    header = parse_header(drv_text)
    assert header is not None
    style, tmpl, ver, cs = header
    assert style == "1"
    assert tmpl == "agent_drv"
    assert ver == "1"
    assert len(cs) == 8
    assert compute_checksum(drv_text) == cs

    regions = user_regions(drv_text)
    assert "run_phase" in regions
    assert "drive_item" in regions

    # Editing inside a user region keeps checksum identical.
    edited_inside = drv_text.replace(
        "  // solouvm:user begin fields\n",
        "  // solouvm:user begin fields\n  int custom_cnt = 0;\n",
    )
    assert edited_inside != drv_text
    assert compute_checksum(edited_inside) == cs

    # Editing outside user regions changes checksum.
    edited_outside = drv_text + "\n// manual outside edit\n"
    assert compute_checksum(edited_outside) != cs

    # Merging into new template preserves user body and detects outside edit.
    merged, orphans, modified_outside = merge_regions(drv_text, edited_inside)
    assert "int custom_cnt = 0;" in merged
    assert orphans == {}
    assert modified_outside is False

    _, _, modified_outside2 = merge_regions(drv_text, edited_outside)
    assert modified_outside2 is True


def test_regions_errors_and_orphans() -> None:
    dup_src = (
        "// solouvm:user begin a\nx\n// solouvm:user end a\n"
        "// solouvm:user begin a\n// solouvm:user end a\n"
    )
    with pytest.raises(ConfigError) as exc1:
        user_regions(dup_src)
    assert exc1.value.code == "CFG_INVALID"

    with pytest.raises(ConfigError) as exc2:
        user_regions("// solouvm:user begin a\nx\n")
    assert exc2.value.code == "CFG_INVALID"

    hdr = (
        "// generated by solouvm (style 1, template t@1, checksum 00000000)"
        " -- edit only inside user regions\n"
    )
    old = (
        hdr
        + "// solouvm:user begin keep\n  keep_me();\n// solouvm:user end keep\n"
        + "// solouvm:user begin old_only\n  orphan_me();\n// solouvm:user end old_only\n"
    )
    old = old.replace("00000000", compute_checksum(old))
    new = hdr + "// solouvm:user begin keep\n// solouvm:user end keep\n"
    merged, orphans, modified = merge_regions(new, old)
    assert not modified
    assert "keep_me();" in merged
    assert orphans == {"old_only": "  orphan_me();\n"}


# ------------------------------------------------ render_component & create_component


def test_render_unknown_kind_raises() -> None:
    with pytest.raises(ConfigError) as exc_info:
        render_component("bogus", "foo")
    assert exc_info.value.code == "CFG_INVALID"


def test_new_agent_generates_11_files_and_style(tmp_path: Path) -> None:
    created = create_component(tmp_path, "agent", "axi_mst")
    assert len(created) == 11

    base = tmp_path / "verif" / "agents" / "axi_mst"
    expected_paths = {
        base / "axi_mst_if.sv",
        base / "axi_mst_pkg.sv",
        base / "axi_mst_files.svh",
        base / "axi_mst_txn.sv",
        base / "axi_mst_cfg.sv",
        base / "axi_mst_sqr.sv",
        base / "axi_mst_drv.sv",
        base / "axi_mst_mon.sv",
        base / "axi_mst_cov.sv",
        base / "axi_mst_agent.sv",
        base / "seq" / "axi_mst_base_seq.sv",
    }
    assert set(created) == expected_paths
    for p in expected_paths:
        assert p.is_file()
        if p.suffix == ".sv":
            first_line = p.read_text(encoding="utf-8").splitlines()[0]
            assert first_line.startswith("// generated by solouvm (style 1, template ")

    drv_text = (base / "axi_mst_drv.sv").read_text(encoding="utf-8")
    assert "drv_mp" in drv_text and "drv_cb" in drv_text
    assert "vif.drv_mp.drv_cb.data <=" in drv_text

    if_text = (base / "axi_mst_if.sv").read_text(encoding="utf-8")
    assert "clocking drv_cb" in if_text
    assert "output data;" in if_text
    assert "input ready;" in if_text
    assert "clocking mon_cb" in if_text

    agent_text = (base / "axi_mst_agent.sv").read_text(encoding="utf-8")
    assert "type_id::create" in agent_text
    assert '"vif"' in agent_text
    assert '"cfg"' in agent_text

    assert lint_style(tmp_path) == []


def test_slave_driver_drives_s2m_and_like_copies_fields() -> None:
    iface = Interface(
        name="apb",
        clock="pclk",
        reset=("presetn", "low"),
        signals=(Signal("paddr", 16, "m2s"), Signal("prdata", 32, "s2m")),
    )
    mst = Agent("apb_mst", iface, "master", fields=(Field("paddr", 16),))
    slv = Agent("apb_slv", iface, "slave", like="apb_mst")
    env = Env("apb", (mst, slv))

    slv_files = render_component("agent", "apb_slv", env)
    slv_drv = slv_files[Path("verif/agents/apb_slv/apb_slv_drv.sv")]
    assert "prdata" in slv_drv
    assert "paddr <=" not in slv_drv
    slv_txn = slv_files[Path("verif/agents/apb_slv/apb_slv_txn.sv")]
    assert "rand bit [15:0] paddr;" in slv_txn

    # Slave-only interface uses drv_cb/drv_mp for s2m
    slv_standalone = Agent("apb_slv", iface, "slave", fields=(Field("paddr", 16),))
    slv_only_files = render_component("agent", "apb_slv", slv_standalone)
    apb_if = slv_only_files[Path("verif/agents/apb/apb_if.sv")]
    assert "output prdata;" in apb_if
    assert "input paddr;" in apb_if


def test_render_determinism() -> None:
    spec = _sample_spec()
    first = render_component("env", "axi", spec)
    second = render_component("env", "axi", spec)
    assert first == second


def test_sync_and_new_preserve_explicit_include_block(tmp_path: Path) -> None:
    init_project(root=tmp_path, simulator="fake")
    agent_dir = tmp_path / "verif/agents/axi_mst"
    manifest = agent_dir / "axi_mst_files.svh"
    (agent_dir / "local_defs.svh").write_text("// Local definitions\n", encoding="utf-8")
    block = (
        "// solouvm:files begin\n"
        '`include "agents/axi_mst/local_defs.svh"\n'
        "// solouvm:files end\n"
    )
    original = manifest.read_text(encoding="utf-8")
    first_line, generated = original.split("\n", 1)
    manifest.write_text(first_line + "\n" + block + generated, encoding="utf-8")

    uvm_sync(tmp_path)
    create_component(tmp_path, "seq", "extra", agent_name="axi_mst")

    assert manifest.read_text(encoding="utf-8").startswith(first_line + "\n" + block)
    assert find_svh_inconsistencies(tmp_path) == []


def test_new_env_with_agents_and_tb_top(tmp_path: Path) -> None:
    spec = _sample_spec()
    rendered = render_component("env", "axi", spec)
    assert Path("verif/envs/axi/axi_env_pkg.sv") in rendered
    assert Path("verif/envs/axi/axi_env_files.svh") in rendered
    assert Path("verif/envs/axi/axi_env_cfg.sv") in rendered
    assert Path("verif/envs/axi/axi_scb.sv") in rendered
    assert Path("verif/envs/axi/axi_ref.sv") in rendered
    assert Path("verif/envs/axi/axi_cov.sv") in rendered
    assert Path("verif/envs/axi/axi_vsqr.sv") in rendered
    assert Path("verif/envs/axi/axi_env.sv") in rendered
    assert Path("verif/envs/axi/vseq/axi_base_vseq.sv") in rendered
    assert Path("verif/tests/axi/axi_test_pkg.sv") in rendered
    assert Path("verif/tests/axi/axi_test_files.svh") in rendered
    assert Path("verif/tests/axi/axi_base_test.sv") in rendered
    assert Path("verif/tb/tb_top.sv") in rendered
    assert Path("verif/tb/binds.sv") in rendered

    scb = rendered[Path("verif/envs/axi/axi_scb.sv")]
    assert '"AXI_SCB"' in scb
    assert "compared=%0d matched=%0d mismatched=%0d pending=%0d" in scb

    tb_top = rendered[Path("verif/tb/tb_top.sv")]
    assert "uvm_config_db" in tb_top and '"vif"' in tb_top

    base_test = rendered[Path("verif/tests/axi/axi_base_test.sv")]
    assert "start_of_simulation_phase" in base_test

    # With --agents flag without tbspec.py
    created = create_component(tmp_path, "env", "pcie", agents=["ep", "rc"])
    assert any(p.name == "pcie_env.sv" for p in created)
    assert any(p.name == "pcie_base_test.sv" for p in created)
    assert any(p.name == "tb_top.sv" for p in created)


def test_new_seq_vseq_test_and_pkg_files_order(tmp_path: Path) -> None:
    create_component(tmp_path, "agent", "axi_mst")
    create_component(tmp_path, "env", "axi", agents=["axi_mst"])
    create_component(tmp_path, "seq", "zeta", agent_name="axi_mst")
    create_component(tmp_path, "seq", "alpha", agent_name="axi_mst")
    create_component(tmp_path, "vseq", "burst", env_name="axi")
    create_component(tmp_path, "test", "burst", env_name="axi")

    # Agent _files.svh order: components -> base_seq -> alpha -> zeta
    ag_svh = (tmp_path / "verif/agents/axi_mst/axi_mst_files.svh").read_text(encoding="utf-8")
    ag_lines = [line for line in ag_svh.splitlines() if line.startswith("`include")]
    assert ag_lines == [
        '`include "agents/axi_mst/axi_mst_txn.sv"',
        '`include "agents/axi_mst/axi_mst_cfg.sv"',
        '`include "agents/axi_mst/axi_mst_sqr.sv"',
        '`include "agents/axi_mst/axi_mst_drv.sv"',
        '`include "agents/axi_mst/axi_mst_mon.sv"',
        '`include "agents/axi_mst/axi_mst_cov.sv"',
        '`include "agents/axi_mst/axi_mst_agent.sv"',
        '`include "agents/axi_mst/seq/axi_mst_base_seq.sv"',
        '`include "agents/axi_mst/seq/axi_mst_alpha_seq.sv"',
        '`include "agents/axi_mst/seq/axi_mst_zeta_seq.sv"',
    ]

    vseq_text = (tmp_path / "verif/envs/axi/vseq/axi_burst_vseq.sv").read_text(encoding="utf-8")
    assert '[AXI_BURST_VSEQ]`, "start"' not in vseq_text  # valid macro args
    assert '`uvm_info("AXI_BURST_VSEQ", "start"' in vseq_text
    assert "done items=%0d" in vseq_text

    # Registered case in case.py
    (tmp_path / "solouvm.conf.py").write_text(
        'CFG = {"simulator": "fake", "top": "tb_top"}\n', encoding="utf-8"
    )
    cfg = load_config(tmp_path)
    cases = collect_cases(cfg)
    assert len(cases) == 1
    case = cases[0]
    assert case == Case(
        name="burst",
        uvm_test="axi_burst_test",
        stage="wip",
        expect=[Expect(scoreboard="AXI_SCB", min=1)],
    )
    assert find_svh_inconsistencies(tmp_path) == []
    assert lint_style(tmp_path) == []


def test_new_test_respects_case_dirs(tmp_path: Path) -> None:
    (tmp_path / "solouvm.conf.py").write_text(
        'CFG = {"simulator": "fake", "top": "tb_top", "case_dirs": ["cases"]}\n',
        encoding="utf-8",
    )
    create_component(tmp_path, "agent", "axi_mst")
    create_component(tmp_path, "env", "axi", agents=["axi_mst"])
    create_component(tmp_path, "test", "burst", env_name="axi")

    assert (tmp_path / "cases" / "burst.py").is_file()
    assert not (tmp_path / "case.py").exists()
    cases = collect_cases(load_config(tmp_path))
    assert [c.name for c in cases] == ["burst"]


def test_create_component_conflict_lists_all_files(tmp_path: Path) -> None:
    create_component(tmp_path, "agent", "axi_mst")
    with pytest.raises(ConfigError) as exc_info:
        create_component(tmp_path, "agent", "axi_mst")
    assert exc_info.value.code == "UVMGEN_EXISTS"
    msg = str(exc_info.value)
    assert "axi_mst_drv.sv" in msg
    assert "axi_mst_agent.sv" in msg
    assert "axi_mst_base_seq.sv" in msg


# ---------------------------------------------------------------- custom templates


def test_custom_template_override_and_missing_error(tmp_path: Path) -> None:
    custom = tmp_path / "my_templates"
    custom.mkdir()
    hdr = (
        "// generated by solouvm (style 1, template ${template}@${version}, "
        "checksum ${checksum}) -- edit only inside user regions\n"
    )
    (custom / "binds.sv").write_text(
        hdr + "// team custom binds\n// solouvm:user begin binds\n// solouvm:user end binds\n",
        encoding="utf-8",
    )
    rendered = render_component("env", "axi", _sample_spec(), templates_dir=custom)
    binds = rendered[Path("verif/tb/binds.sv")]
    assert "// team custom binds" in binds
    assert parse_header(binds) is not None

    with pytest.raises(InfraError) as exc_info:
        render_component("agent", "axi_mst", templates_dir=tmp_path / "missing_templates")
    assert exc_info.value.code == "PROVIDER_FAILED"


# ---------------------------------------------------------------- uvm sync / diff / lint_style


def test_uvm_sync_diff_orphan_outside_edit_and_extra_files(tmp_path: Path) -> None:
    _write_tbspec(
        tmp_path,
        """\
        from solouvm.uvm import Agent, Env, Field, Interface, Signal

        AXI = Interface(
            name="axi",
            clock="aclk",
            reset=("aresetn", "low"),
            signals=[Signal("awaddr", 32, "m2s"), Signal("awready", 1, "s2m")],
        )
        ENVS = [
            Env("axi", [Agent("axi_mst", AXI, "master", fields=[Field("addr", 32)])]),
        ]
        """,
    )
    create_component(tmp_path, "agent", "axi_mst")
    create_component(tmp_path, "env", "axi")
    create_component(tmp_path, "seq", "burst", agent_name="axi_mst")

    # 1) User edits inside a user region in axi_mst_drv.sv -> preserved on sync.
    drv_path = tmp_path / "verif/agents/axi_mst/axi_mst_drv.sv"
    drv_text = drv_path.read_text(encoding="utf-8")
    drv_path.write_text(
        drv_text.replace(
            "  // solouvm:user begin fields\n",
            "  // solouvm:user begin fields\n  int user_flag = 1;\n",
        ),
        encoding="utf-8",
    )

    # 2) User edits outside user region in axi_mst_mon.sv -> reported & NOT overwritten.
    mon_path = tmp_path / "verif/agents/axi_mst/axi_mst_mon.sv"
    mon_modified = mon_path.read_text(encoding="utf-8") + "\n// manual edit outside\n"
    mon_path.write_text(mon_modified, encoding="utf-8")

    # 3) Update tbspec.py to add a field + add a signal, and place an orphan-triggering template.
    custom_tpl = tmp_path / "custom_tpl"
    custom_tpl.mkdir()
    # Copy agent_drv without `build_phase` user region to trigger `.orphan` when non-empty.
    drv_with_bp = drv_path.read_text(encoding="utf-8").replace(
        "    // solouvm:user begin build_phase\n",
        "    // solouvm:user begin build_phase\n    custom_bp();\n",
    )
    drv_path.write_text(drv_with_bp, encoding="utf-8")

    from importlib.resources import files as res_files

    orig_drv_tpl = (res_files("solouvm.uvmgen.templates.style1") / "agent_drv.sv").read_text(
        encoding="utf-8"
    )
    (custom_tpl / "agent_drv.sv").write_text(
        orig_drv_tpl.replace(
            "    // solouvm:user begin build_phase\n    // solouvm:user end build_phase\n",
            "",
        ),
        encoding="utf-8",
    )

    # Add a stale extra file under verif/agents/old_agt/
    stale = tmp_path / "verif/agents/old_agt/old_agt_drv.sv"
    stale.parent.mkdir(parents=True)
    stale.write_text("// stale\n", encoding="utf-8")

    preview = uvm_diff(tmp_path, templates_dir=custom_tpl)
    assert "axi_mst_drv.sv" in preview
    # dry-run did not write .orphan yet
    assert not (tmp_path / "verif/agents/axi_mst/axi_mst_drv.sv.orphan").exists()

    report = uvm_sync(tmp_path, templates_dir=custom_tpl)
    assert Path("verif/agents/axi_mst/axi_mst_drv.sv") in report.updated
    assert Path("verif/agents/axi_mst/axi_mst_drv.sv.orphan") in report.orphans
    assert Path("verif/agents/axi_mst/axi_mst_mon.sv") in report.modified_outside
    assert Path("verif/agents/old_agt/old_agt_drv.sv") in report.extra_files

    # User code inside preserved region stayed intact, orphan code saved, outside edit untouched!
    new_drv = drv_path.read_text(encoding="utf-8")
    assert "int user_flag = 1;" in new_drv
    orphan_text = (tmp_path / "verif/agents/axi_mst/axi_mst_drv.sv.orphan").read_text(
        encoding="utf-8"
    )
    assert "custom_bp();" in orphan_text
    assert mon_path.read_text(encoding="utf-8") == mon_modified

    # Sequence remained in axi_mst_files.svh
    svh = (tmp_path / "verif/agents/axi_mst/axi_mst_files.svh").read_text(encoding="utf-8")
    assert "axi_mst_burst_seq.sv" in svh


def test_lint_style_catches_all_violations_and_ignores_comments(tmp_path: Path) -> None:
    create_component(tmp_path, "agent", "axi_mst")
    assert lint_style(tmp_path) == []

    # Comments / strings with $display or #10 should not trigger false positives.
    drv = tmp_path / "verif/agents/axi_mst/axi_mst_drv.sv"
    orig_drv = drv.read_text(encoding="utf-8")
    drv.write_text(
        orig_drv.replace(
            "  // solouvm:user begin methods\n",
            "  // solouvm:user begin methods\n  // avoid $display #10\n"
            '  string s = "$finish #5";\n',
        ),
        encoding="utf-8",
    )
    assert lint_style(tmp_path) == []

    # Inject multiple real violations in a bad file.
    bad = tmp_path / "verif/agents/axi_mst/BadFile.sv"
    bad.write_text(
        textwrap.dedent(
            """\
            class wrong_cls;
              function void foo();
                $display("hi");
                $finish;
                #10;
                obj = axi_mst_txn::new("obj");
                uvm_config_db #(int)::set(null, "*", "vif", 0);
                `uvm_info("", "no id", UVM_LOW)
                `uvm_error("WRONG_ID", "bad id")
              endfunction
            endclass
            """
        ),
        encoding="utf-8",
    )
    issues = lint_style(tmp_path)
    formatted = "\n".join(str(i) for i in issues)
    assert "$display" in formatted
    assert "$finish" in formatted
    assert "bare delay" in formatted
    assert "::new()" in formatted
    assert '"vif"' in formatted
    assert "empty message ID" in formatted
    assert "WRONG_ID" in formatted
    assert "BadFile.sv" in formatted
    assert "not included" in formatted

    # Outside-region modification triggers checksum issue
    drv.write_text(orig_drv + "\n// outside\n", encoding="utf-8")
    issues2 = lint_style(tmp_path)
    assert any("checksum" in str(i) for i in issues2)


def test_update_pkg_files_explicit_list_and_env_flags(tmp_path: Path) -> None:
    svh = update_pkg_files(
        tmp_path,
        "axi_mst",
        [
            "agents/axi_mst/seq/axi_mst_z_seq.sv",
            "agents/axi_mst/axi_mst_drv.sv",
            "agents/axi_mst/seq/axi_mst_base_seq.sv",
            "agents/axi_mst/seq/axi_mst_a_seq.sv",
            "agents/axi_mst/axi_mst_txn.sv",
            "agents/axi_mst/axi_mst_sqr.sv",
        ],
    )
    assert svh == tmp_path / "verif/agents/axi_mst/axi_mst_files.svh"
    includes = [
        line for line in svh.read_text(encoding="utf-8").splitlines() if line.startswith("`include")
    ]
    assert includes == [
        '`include "agents/axi_mst/axi_mst_txn.sv"',
        '`include "agents/axi_mst/axi_mst_sqr.sv"',
        '`include "agents/axi_mst/axi_mst_drv.sv"',
        '`include "agents/axi_mst/seq/axi_mst_base_seq.sv"',
        '`include "agents/axi_mst/seq/axi_mst_a_seq.sv"',
        '`include "agents/axi_mst/seq/axi_mst_z_seq.sv"',
    ]

    # Env with scoreboard=False, ref_model=False, coverage=False omits optional files
    iface = Interface("apb", "pclk", ("presetn", "low"), (Signal("paddr", 16, "m2s"),))
    env = Env(
        "apb",
        (Agent("apb_mst", iface, "master", (Field("paddr", 16, rand=False),)),),
        scoreboard=False,
        ref_model=False,
        coverage=False,
    )
    rendered = render_component("env", "apb", env)
    assert Path("verif/envs/apb/apb_scb.sv") not in rendered
    assert Path("verif/envs/apb/apb_ref.sv") not in rendered
    assert Path("verif/envs/apb/apb_cov.sv") not in rendered

    ag_rendered = render_component("agent", "apb_mst", env)
    txn_text = ag_rendered[Path("verif/agents/apb_mst/apb_mst_txn.sv")]
    assert "  bit [15:0] paddr;" in txn_text
    assert "rand bit [15:0] paddr;" not in txn_text


def test_config_uvm_templates_and_filtered_sync(tmp_path: Path) -> None:
    custom = tmp_path / "team_tpl"
    custom.mkdir()
    hdr = (
        "// generated by solouvm (style 1, template ${template}@${version}, "
        "checksum ${checksum}) -- edit only inside user regions\n"
    )
    (custom / "binds.sv").write_text(
        hdr + "// from solouvm.conf.py\n// solouvm:user begin binds\n// solouvm:user end binds\n",
        encoding="utf-8",
    )
    (tmp_path / "solouvm.conf.py").write_text(
        'CFG = {"simulator": "fake", "top": "tb_top", "uvm": {"templates": "team_tpl"}}\n',
        encoding="utf-8",
    )
    _write_tbspec(
        tmp_path,
        """\
        from solouvm.uvm import Agent, Env, Interface, Signal

        I = Interface("apb", "pclk", ("presetn", "low"), [Signal("paddr", 16, "m2s")])
        ENVS = [Env("apb", [Agent("apb_mst", I, "master")])]
        """,
    )
    create_component(tmp_path, "agent", "apb_mst")
    create_component(tmp_path, "env", "apb")
    assert "// from solouvm.conf.py" in (tmp_path / "verif/tb/binds.sv").read_text(encoding="utf-8")

    # Filtered uvm_sync on single agent works and catches unknown filter
    rep = uvm_sync(tmp_path, agent="apb_mst")
    assert Path("verif/agents/apb_mst/apb_mst_drv.sv") in rep.unchanged

    with pytest.raises(ConfigError) as exc_info:
        uvm_sync(tmp_path, env="nonexistent")
    assert exc_info.value.code == "CFG_INVALID"


def test_uvm_sync_preserves_existing_orphan_across_multiple_removals(tmp_path: Path) -> None:
    _write_tbspec(
        tmp_path,
        """\
        from solouvm.uvm import Agent, Env, Field, Interface, Signal

        AXI = Interface(
            name="axi",
            clock="aclk",
            reset=("aresetn", "low"),
            signals=[Signal("awaddr", 32, "m2s")],
        )
        ENVS = [
            Env("axi", [Agent("axi_mst", AXI, "master", fields=[Field("addr", 32)])]),
        ]
        """,
    )
    create_component(tmp_path, "agent", "axi_mst")

    drv_path = tmp_path / "verif/agents/axi_mst/axi_mst_drv.sv"
    drv_text = drv_path.read_text(encoding="utf-8")
    drv_text = drv_text.replace(
        "  // solouvm:user begin fields\n",
        "  // solouvm:user begin fields\n  int user_field = 7;\n",
    ).replace(
        "    // solouvm:user begin build_phase\n",
        "    // solouvm:user begin build_phase\n    user_bp_call();\n",
    )
    drv_path.write_text(drv_text, encoding="utf-8")

    from importlib.resources import files as res_files

    orig_tpl = (res_files("solouvm.uvmgen.templates.style1") / "agent_drv.sv").read_text(
        encoding="utf-8"
    )
    custom_tpl = tmp_path / "tpl"
    custom_tpl.mkdir()

    # Sync 1: remove build_phase region
    tpl_1 = orig_tpl.replace(
        "    // solouvm:user begin build_phase\n    // solouvm:user end build_phase\n",
        "",
    )
    (custom_tpl / "agent_drv.sv").write_text(tpl_1, encoding="utf-8")
    rep1 = uvm_sync(tmp_path, agent="axi_mst", templates_dir=custom_tpl)
    orphan_path = tmp_path / "verif/agents/axi_mst/axi_mst_drv.sv.orphan"
    assert Path("verif/agents/axi_mst/axi_mst_drv.sv.orphan") in rep1.orphans
    assert "user_bp_call();" in orphan_path.read_text(encoding="utf-8")

    # Sync 2: remove fields region too -> previous build_phase orphan must remain intact
    tpl_2 = tpl_1.replace(
        "  // solouvm:user begin fields\n  // solouvm:user end fields\n",
        "",
    )
    (custom_tpl / "agent_drv.sv").write_text(tpl_2, encoding="utf-8")
    rep2 = uvm_sync(tmp_path, agent="axi_mst", templates_dir=custom_tpl)
    assert Path("verif/agents/axi_mst/axi_mst_drv.sv.orphan") in rep2.orphans
    merged_orphan = orphan_path.read_text(encoding="utf-8")
    assert "user_bp_call();" in merged_orphan
    assert "int user_field = 7;" in merged_orphan
