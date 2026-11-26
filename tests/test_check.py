"""Tests for solouvm.check (slang, fake_top, incremental RTL, and formatting)."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from solouvm.backends import get_backend
from solouvm.backends.base import Backend
from solouvm.collect.files import FileEntry, FileList
from solouvm.config.schema import (
    BuildConfig,
    CheckConfig,
    Config,
    DocConfig,
    HistoryConfig,
    JobHooks,
    LimitsConfig,
    ProviderConfig,
    RegressConfig,
    RunConfig,
    TriageConfig,
    UvmConfig,
)
from solouvm.errors import ConfigError, InfraError
from solouvm.model import Checks
from solouvm.providers.base import Provider
from solouvm.providers.launcher_make import MakeLauncher
from solouvm.providers.sync_none import NoneSync
from solouvm.providers.transport_local import LocalTransport

from solouvm.check import (  # isort: skip
    FAST_STATE_REL,
    HIER_REF_HINT,
    CheckDiagnostic,
    CheckResult,
    extract_tb_packages,
    format_check_result,
    generate_check_dotf,
    plan_rtl_incremental,
    read_fast_state,
    run_check,
    write_fast_state,
)


def _make_config(
    root: Path,
    *,
    simulator: str = "vcs",
    check: CheckConfig | None = None,
    defines: list[str] | None = None,
    incdirs: list[str] | None = None,
    sim: dict[str, object] | None = None,
    sources: dict[str, str] | None = None,
) -> Config:
    return Config(
        root=root,
        project="demo",
        simulator=simulator,
        top="tb_top",
        uvm_test="smoke_test",
        incdirs=incdirs if incdirs is not None else [],
        defines=defines if defines is not None else [],
        builds={"default": BuildConfig()},
        regress=RegressConfig(),
        run=RunConfig(),
        checks=Checks(),
        job=JobHooks(),
        sim=sim if sim is not None else {},
        providers={"local": ProviderConfig(name="local")},
        provider="local",
        check=check if check is not None else CheckConfig(),
        case_dirs=[],
        system_doc=[],
        triage=TriageConfig(),
        doc=DocConfig(),
        history=HistoryConfig(),
        limits=LimitsConfig(),
        plugins=[],
        uvm=UvmConfig(),
        sources=sources if sources is not None else {},
    )


def _local_provider(root: Path, *, setup: str = "") -> Provider:
    return Provider(
        name="local",
        transport=LocalTransport(root=root),
        launcher=MakeLauncher(),
        sync=NoneSync(),
        setup=setup,
    )


def _write_exec(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _make_stub_project(root: Path) -> FileList:
    (root / "inc").mkdir(parents=True, exist_ok=True)
    (root / "inc" / "defs.svh").write_text("localparam int W = 8;\n", encoding="utf-8")
    (root / "rtl").mkdir(parents=True, exist_ok=True)
    (root / "rtl" / "axi_pkg.sv").write_text(
        "package axi_pkg;\n  typedef logic [7:0] byte_t;\nendpackage\n",
        encoding="utf-8",
    )
    (root / "rtl" / "axi_slv.sv").write_text(
        'module axi_slv;\n  `include "defs.svh"\n  import axi_pkg::*;\nendmodule\n',
        encoding="utf-8",
    )
    (root / "tb" / "pkg").mkdir(parents=True, exist_ok=True)
    (root / "tb" / "pkg" / "axi_env_pkg.sv").write_text(
        "package axi_env_pkg;\nendpackage\n",
        encoding="utf-8",
    )
    (root / "tb" / "tb_top.sv").write_text(
        "module tb_top;\nendmodule\n",
        encoding="utf-8",
    )
    return FileList(
        groups={
            "rtl": ["rtl/axi_pkg.sv", "rtl/axi_slv.sv"],
            "tb": ["tb/pkg/axi_env_pkg.sv"],
            "top": ["tb/tb_top.sv"],
        },
        incdirs={"rtl": ["inc"], "tb": ["tb/pkg"], "top": []},
        defines={"rtl": ["RTL_DEF"], "tb": ["TB_DEF"], "top": []},
        entries=[
            FileEntry("rtl/axi_pkg.sv", "rtl", "rtl/axi_pkg.sv"),
            FileEntry("rtl/axi_slv.sv", "rtl", "rtl/axi_slv.sv"),
            FileEntry("tb/pkg/axi_env_pkg.sv", "tb", "tb/pkg/axi_env_pkg.sv"),
            FileEntry("tb/tb_top.sv", "top", "tb/tb_top.sv"),
        ],
        libs={"rtl": [], "tb": [], "top": []},
    )


class _StubRemoteTransport:
    """Minimal Transport implementing only the Protocol methods (no exec_capture)."""

    kind = "ssh"

    def __init__(self, remote_root: Path, *, handler=None) -> None:
        self.remote_root = remote_root
        self.handler = handler
        self.exec_calls: list[tuple[list[str], str]] = []

    def _rpath(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else self.remote_root / p

    def exec(self, argv: list[str], *, cwd: str, check: bool = True) -> int:
        self.exec_calls.append((list(argv), cwd))
        rc = self.handler(argv, cwd, self.remote_root) if self.handler is not None else 0
        if check and rc != 0:
            raise InfraError(f"remote failed {rc}", code="PROVIDER_FAILED")
        return rc

    def spawn(self, argv: list[str], *, cwd: str):
        raise NotImplementedError

    def read_lines(
        self,
        path: str,
        *,
        keywords: list[str] | None = None,
        lines: tuple[int, int] | None = None,
    ) -> Iterator[tuple[int, str]]:
        _ = keywords, lines
        target = self._rpath(path)
        yield from enumerate(target.read_text(encoding="utf-8").splitlines(), 1)

    def read_many(self, pattern: str) -> dict[str, str]:
        _ = pattern
        return {}

    def put(self, path: str, data: bytes) -> None:
        target = self._rpath(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    def exists(self, path: str) -> bool:
        return self._rpath(path).exists()


class _StubRemoteSync:
    kind = "rsync"

    def __init__(self, remote_root: Path) -> None:
        self.remote_root = remote_root
        self.pushed_manifests: list[list[str]] = []

    def push(self, transport, tree: str, files) -> None:
        tree_p = Path(tree)
        manifest = list(files)
        self.pushed_manifests.append(manifest)
        for rel in manifest:
            src = tree_p / rel
            if src.is_file():
                transport.put(rel, src.read_bytes())


# ------------------------------------------------------------------ check.f & exclude & UVM


def test_generate_check_dotf_exclude_uvm_and_env_expansion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj"
    files = _make_stub_project(proj)
    (proj / "ip" / "encrypted").mkdir(parents=True)
    (proj / "ip" / "encrypted" / "secret.sv").write_text("module secret; endmodule\n")
    (proj / "ip" / "vendor_vip.sv").write_text("package vendor_vip; endpackage\n")
    files.groups["rtl"].extend(["ip/encrypted/secret.sv", "ip/vendor_vip.sv"])
    files.entries.extend(
        [
            FileEntry("ip/encrypted/secret.sv", "rtl", "ip/encrypted/secret.sv"),
            FileEntry("ip/vendor_vip.sv", "rtl", "ip/vendor_vip.sv"),
            FileEntry("/opt/ext/ext_ip.sv", "rtl", "/opt/ext/ext_ip.sv", outside_root=True),
            FileEntry("${IP_DIR}/var_ip.sv", "rtl", "${IP_DIR}/var_ip.sv"),
        ]
    )
    files.groups["rtl"].extend(["/opt/ext/ext_ip.sv", "${IP_DIR}/var_ip.sv"])

    uvm_dir = tmp_path / "uvm" / "src"
    uvm_dir.mkdir(parents=True)
    (uvm_dir / "uvm_pkg.sv").write_text("package uvm_pkg; endpackage\n")

    monkeypatch.setenv("UVM_HOME", str(tmp_path / "uvm"))
    monkeypatch.setenv("IP_DIR", str(tmp_path / "ip_ext"))

    cfg = _make_config(
        proj,
        defines=["SIM"],
        incdirs=["global_inc"],
        check=CheckConfig(exclude=["ip/encrypted/**", "*_vip.sv"]),
    )
    prov = _local_provider(proj)

    dotf_path, count = generate_check_dotf(
        config=cfg, files=files, provider=prov, expand_local=True
    )
    text = dotf_path.read_text(encoding="utf-8")
    lines = text.splitlines()

    assert count == 6
    assert "+define+SIM" in lines
    assert "+incdir+../../global_inc" in lines
    assert f"+incdir+{uvm_dir.as_posix()}" in lines
    assert f"{uvm_dir.as_posix()}/uvm_pkg.sv" in lines
    assert lines.index(f"{uvm_dir.as_posix()}/uvm_pkg.sv") < lines.index("../../rtl/axi_pkg.sv")
    assert "../../rtl/axi_pkg.sv" in lines
    assert "../../tb/pkg/axi_env_pkg.sv" in lines
    assert "../../tb/tb_top.sv" in lines
    assert "/opt/ext/ext_ip.sv" in lines
    assert f"{(tmp_path / 'ip_ext').as_posix()}/var_ip.sv" in lines
    assert all("secret.sv" not in ln and "vendor_vip.sv" not in ln for ln in lines)

    # Remote mode preserves ${VAR} without expanding A's env
    dotf_remote, _ = generate_check_dotf(
        config=cfg,
        files=files,
        provider=prov,
        expand_local=False,
    )
    remote_lines = dotf_remote.read_text(encoding="utf-8").splitlines()
    assert "${IP_DIR}/var_ip.sv" in remote_lines
    assert "/opt/ext/ext_ip.sv" in remote_lines


def test_generate_check_dotf_skips_missing_default_uvm_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj"
    files = _make_stub_project(proj)
    monkeypatch.delenv("UVM_HOME", raising=False)
    cfg = _make_config(proj)
    dotf_path, count = generate_check_dotf(
        config=cfg, files=files, provider=_local_provider(proj), expand_local=True
    )
    assert count == 4
    assert "uvm_pkg.sv" not in dotf_path.read_text(encoding="utf-8")


# ------------------------------------------------------------------ stub slang: JSON & text


def test_slang_json_pass_and_fail(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    files = _make_stub_project(proj)

    args_log = tmp_path / "slang_args.txt"
    stub_pass = _write_exec(
        tmp_path / "bin" / "slang_pass",
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {args_log}\necho '[]'\nexit 0\n",
    )
    cfg_pass = _make_config(
        proj,
        check=CheckConfig(
            slang=str(stub_pass),
            slang_args=["-Wno-unused"],
            faketop=False,
        ),
    )
    res_pass, rc_pass = run_check(
        config=cfg_pass,
        files=files,
        backend=get_backend("fake"),
        provider=_local_provider(proj),
    )
    assert rc_pass == 0
    assert res_pass.ok is True
    assert res_pass.where == "local"
    assert res_pass.files_checked == 4
    assert res_pass.layers == ["slang"]
    assert args_log.read_text(encoding="utf-8").splitlines() == [
        "-f",
        "check.f",
        "--lint-only",
        "--diag-json",
        "-",
        "-Wno-unused",
    ]

    # JSON fail with both error and warning, and ../../ relative path normalization
    diag_payload = json.dumps(
        [
            {
                "severity": "error",
                "message": "use of undeclared identifier 'addr_nxt'",
                "location": {"file": "../../rtl/axi_slv.sv", "line": 120, "column": 14},
            },
            {
                "severity": "warning",
                "message": "unused variable 'cnt'",
                "location": {"file": "../../tb/pkg/axi_env_pkg.sv", "line": 45, "column": 3},
            },
        ]
    )
    stub_fail = _write_exec(
        tmp_path / "bin" / "slang_fail",
        f"#!/usr/bin/env bash\ncat <<'EOF'\n{diag_payload}\nEOF\nexit 1\n",
    )
    cfg_fail = _make_config(
        proj,
        check=CheckConfig(slang=str(stub_fail)),
    )
    res_fail, rc_fail = run_check(
        config=cfg_fail,
        files=files,
        backend=get_backend("fake"),
        provider=_local_provider(proj),
    )
    assert rc_fail == 1
    assert res_fail.ok is False
    assert res_fail.error_count == 1
    assert res_fail.warning_count == 1
    assert res_fail.layers == ["slang"]  # stops before faketop
    assert res_fail.diagnostics[0] == CheckDiagnostic(
        file="rtl/axi_slv.sv",
        line=120,
        col=14,
        severity="error",
        message="use of undeclared identifier 'addr_nxt'",
    )
    assert res_fail.diagnostics[1].file == "tb/pkg/axi_env_pkg.sv"


def test_slang_text_fallback_and_formats(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    files = _make_stub_project(proj)

    stub_text = _write_exec(
        tmp_path / "bin" / "slang_text",
        "#!/usr/bin/env bash\n"
        "echo \"../../rtl/axi_slv.sv:120:14: error: use of undeclared identifier 'addr_nxt'\"\n"
        "echo \"../../tb/pkg/axi_env_pkg.sv:45:3: warning: unused variable 'cnt'\"\n"
        "exit 1\n",
    )
    cfg = _make_config(proj, check=CheckConfig(slang=str(stub_text), faketop=False))
    res, rc = run_check(
        config=cfg,
        files=files,
        backend=get_backend("fake"),
        provider=_local_provider(proj),
    )
    assert rc == 1
    assert res.error_count == 1
    assert res.warning_count == 1

    text_out = format_check_result(res, fmt="text")
    assert "rtl/axi_slv.sv:120:14: error: use of undeclared identifier 'addr_nxt'" in text_out
    assert "tb/pkg/axi_env_pkg.sv:45:3: warning: unused variable 'cnt'" in text_out
    assert "slang: 1 error, 1 warning in " in text_out
    assert "(4 files)" in text_out

    gcc_out = format_check_result(res, fmt="gcc")
    assert gcc_out == (
        "rtl/axi_slv.sv:120:14: error: use of undeclared identifier 'addr_nxt'\n"
        "tb/pkg/axi_env_pkg.sv:45:3: warning: unused variable 'cnt'"
    )

    json_out = json.loads(format_check_result(res, fmt="json"))
    assert json_out["schema"] == 1
    assert json_out["ok"] is False
    assert json_out["error_count"] == 1
    assert json_out["warning_count"] == 1
    assert json_out["diagnostics"][0]["file"] == "rtl/axi_slv.sv"

    with pytest.raises(ConfigError) as bad_fmt:
        format_check_result(res, fmt="xml")
    assert bad_fmt.value.code == "CFG_INVALID"


# ------------------------------------------------------------------ where & CHECK_UNAVAILABLE


def test_where_local_missing_slang_raises_check_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj"
    files = _make_stub_project(proj)
    monkeypatch.setenv("PATH", "")

    for where_mode in ("local", "auto"):
        cfg = _make_config(proj, check=CheckConfig(slang="nonexistent_slang", where=where_mode))
        with pytest.raises(InfraError) as exc_info:
            run_check(
                config=cfg,
                files=files,
                backend=get_backend("fake"),
                provider=_local_provider(proj),
            )
        assert exc_info.value.code == "CHECK_UNAVAILABLE"
        assert exc_info.value.exit_code == 2


def test_where_remote_and_auto_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = tmp_path / "proj"
    remote_root = tmp_path / "remote"
    files = _make_stub_project(proj)

    def remote_handler(argv: list[str], cwd: str, rroot: Path) -> int:
        # Handle wrapped bash -c "... > slang.stdout 2> slang.stderr"
        assert cwd == ".solouvm/check"
        assert argv[0] == "bash" and argv[1] == "-c"
        check_dir = rroot / ".solouvm" / "check"
        (check_dir / "slang.stdout").write_text("[]\n", encoding="utf-8")
        (check_dir / "slang.stderr").write_text("", encoding="utf-8")
        return 0

    transport = _StubRemoteTransport(remote_root, handler=remote_handler)
    sync = _StubRemoteSync(remote_root)
    remote_prov = Provider(name="farm", transport=transport, launcher=MakeLauncher(), sync=sync)

    # 1. auto with missing slang on A -> falls back to remote
    monkeypatch.setenv("PATH", "")
    cfg_auto = _make_config(proj, check=CheckConfig(slang="slang", where="auto", faketop=False))
    res, rc = run_check(
        config=cfg_auto,
        files=files,
        backend=get_backend("fake"),
        provider=remote_prov,
    )
    assert rc == 0
    assert res.where == "remote"
    assert ".solouvm/check/check.f" in sync.pushed_manifests[0]

    # 2. auto with local slang present AND outside_root -> switches to remote with notice
    stub_local = _write_exec(
        tmp_path / "bin" / "slang",
        "#!/usr/bin/env bash\necho '[]'\nexit 0\n",
    )
    files.entries.append(
        FileEntry("/opt/remote_only/ip.sv", "rtl", "/opt/remote_only/ip.sv", outside_root=True)
    )
    files.groups["rtl"].append("/opt/remote_only/ip.sv")
    cfg_outside = _make_config(
        proj, check=CheckConfig(slang=str(stub_local), where="auto", faketop=False)
    )
    res_out, rc_out = run_check(
        config=cfg_outside,
        files=files,
        backend=get_backend("fake"),
        provider=remote_prov,
    )
    assert rc_out == 0
    assert res_out.where == "remote"
    assert any("outside-root" in w for w in res_out.warnings)

    # 3. auto with outside_root missing on A and local provider -> CHECK_UNAVAILABLE
    with pytest.raises(InfraError) as exc_out:
        run_check(
            config=cfg_outside,
            files=files,
            backend=get_backend("fake"),
            provider=_local_provider(proj),
        )
    assert exc_out.value.code == "CHECK_UNAVAILABLE"

    # 4. remote requested with local provider -> CHECK_UNAVAILABLE
    with pytest.raises(InfraError) as exc_rem_local:
        run_check(
            config=cfg_outside,
            files=files,
            backend=get_backend("fake"),
            provider=_local_provider(proj),
            where="remote",
        )
    assert exc_rem_local.value.code == "CHECK_UNAVAILABLE"


def test_remote_missing_slang_raises_check_unavailable(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    remote_root = tmp_path / "remote"
    files = _make_stub_project(proj)

    def missing_handler(argv: list[str], cwd: str, rroot: Path) -> int:
        _ = argv, cwd
        check_dir = rroot / ".solouvm" / "check"
        (check_dir / "slang.stdout").write_text("", encoding="utf-8")
        (check_dir / "slang.stderr").write_text(
            "bash: slang: command not found\n", encoding="utf-8"
        )
        return 127

    transport = _StubRemoteTransport(remote_root, handler=missing_handler)
    sync = _StubRemoteSync(remote_root)
    prov = Provider(name="farm", transport=transport, launcher=MakeLauncher(), sync=sync)
    cfg = _make_config(proj, check=CheckConfig(slang="slang", where="remote", faketop=False))
    with pytest.raises(InfraError) as exc_info:
        run_check(config=cfg, files=files, backend=get_backend("fake"), provider=prov)
    assert exc_info.value.code == "CHECK_UNAVAILABLE"


# ------------------------------------------------------------------ fake_top & E-05


def test_extract_tb_packages_and_faketop_pass_and_e05_hint(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / "tb" / "pkg").mkdir(parents=True)
    (proj / "tb" / "pkg" / "a_pkg.sv").write_text(
        "// package commented_out_pkg;\n"
        "/* package block_commented_pkg; */\n"
        "package automatic axi_mst_pkg;\n"
        "endpackage\n",
        encoding="utf-8",
    )
    (proj / "tb" / "pkg" / "b_pkg.sv").write_text(
        "package axi_env_pkg;\nendpackage\n",
        encoding="utf-8",
    )
    tb_files = ["tb/pkg/a_pkg.sv", "tb/pkg/b_pkg.sv"]
    assert extract_tb_packages(proj, tb_files) == ["axi_mst_pkg", "axi_env_pkg"]

    files = FileList(
        groups={"rtl": ["rtl/dut.sv"], "tb": tb_files, "top": ["tb/tb_top.sv"]},
        incdirs={"rtl": [], "tb": ["tb/pkg"], "top": []},
        defines={"rtl": [], "tb": ["TB_MODE"], "top": []},
        entries=[],
        libs={"rtl": [], "tb": [], "top": []},
    )
    (proj / "rtl").mkdir(parents=True)
    (proj / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    (proj / "tb" / "tb_top.sv").write_text("module tb_top; endmodule\n", encoding="utf-8")

    stub_slang = _write_exec(tmp_path / "bin" / "slang", "#!/usr/bin/env bash\necho '[]'\nexit 0\n")
    sim_args_log = tmp_path / "sim_args.txt"
    stub_sim_pass = _write_exec(
        tmp_path / "bin" / "stub_vcs",
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {sim_args_log}\nexit 0\n",
    )
    cfg = _make_config(
        proj,
        simulator="vcs",
        check=CheckConfig(slang=str(stub_slang)),
        sim={
            "compile_only": (
                f"{stub_sim_pass} -f {{filelist_tb}} {{fake_top}} -top solouvm_fake_top"
            )
        },
    )
    backend = get_backend("vcs", cfg.sim)
    res, rc = run_check(
        config=cfg,
        files=files,
        backend=backend,
        provider=_local_provider(proj),
    )
    assert rc == 0
    assert res.ok is True
    assert res.layers == ["slang", "faketop"]
    assert (proj / ".solouvm" / "gen" / "bin" / "run_job.sh").is_file()
    fake_top_sv = (proj / ".solouvm" / "check" / "fake_top.sv").read_text(encoding="utf-8")
    assert "module solouvm_fake_top;" in fake_top_sv
    assert "import uvm_pkg::*;" in fake_top_sv
    assert "import axi_mst_pkg::*;" in fake_top_sv
    assert "import axi_env_pkg::*;" in fake_top_sv
    assert sim_args_log.read_text(encoding="utf-8").splitlines() == [
        "-f",
        "filelist.tb.f",
        "fake_top.sv",
        "-top",
        "solouvm_fake_top",
    ]
    assert (proj / ".solouvm" / "check" / "check_faketop.log").is_file()

    # E-05 hierarchical reference error detection (VCS XMRE multi-line)
    stub_sim_xmre = _write_exec(
        tmp_path / "bin" / "stub_vcs_xmre",
        "#!/usr/bin/env bash\n"
        "echo 'Error-[XMRE] Cross-module reference resolution error'\n"
        "echo 'Error found while trying to resolve cross-module reference.'\n"
        "echo '\"../../tb/pkg/b_pkg.sv\", 88: tb_top.dut.u_core.state'\n"
        "exit 1\n",
    )
    cfg_xmre = _make_config(
        proj,
        simulator="vcs",
        check=CheckConfig(slang=str(stub_slang)),
        sim={"compile_only": f"{stub_sim_xmre} -f {{filelist_tb}} {{fake_top}}"},
    )
    res_xmre, rc_xmre = run_check(
        config=cfg_xmre,
        files=files,
        backend=get_backend("vcs", cfg_xmre.sim),
        provider=_local_provider(proj),
    )
    assert rc_xmre == 1
    assert res_xmre.ok is False
    assert res_xmre.error_count == 1
    diag = res_xmre.diagnostics[0]
    assert diag.file == "tb/pkg/b_pkg.sv"
    assert diag.line == 88
    assert diag.hint == HIER_REF_HINT
    formatted = format_check_result(res_xmre, fmt="text")
    assert f"hint: {HIER_REF_HINT}" in formatted


def test_faketop_empty_tb_warns_and_skips(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / "rtl").mkdir(parents=True)
    (proj / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    files = FileList(
        groups={"rtl": ["rtl/dut.sv"], "tb": [], "top": []},
        incdirs={"rtl": [], "tb": [], "top": []},
        defines={"rtl": [], "tb": [], "top": []},
        entries=[FileEntry("rtl/dut.sv", "rtl", "rtl/dut.sv")],
        libs={"rtl": [], "tb": [], "top": []},
    )
    stub_slang = _write_exec(tmp_path / "bin" / "slang", "#!/usr/bin/env bash\necho '[]'\nexit 0\n")
    cfg = _make_config(proj, check=CheckConfig(slang=str(stub_slang)))
    res, rc = run_check(
        config=cfg,
        files=files,
        backend=get_backend("vcs"),
        provider=_local_provider(proj),
    )
    assert rc == 0
    assert res.ok is True
    assert res.layers == ["slang"]
    assert any("tb file group is empty" in w for w in res.warnings)
    assert not (proj / ".solouvm" / "check" / "fake_top.sv").exists()


def test_faketop_missing_sim_raises_check_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj"
    files = _make_stub_project(proj)
    stub_slang = _write_exec(tmp_path / "bin" / "slang", "#!/usr/bin/env bash\necho '[]'\nexit 0\n")
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    cfg = _make_config(
        proj,
        simulator="vcs",
        check=CheckConfig(slang=str(stub_slang)),
        sim={"compile_only": "definitely_missing_sim_bin -f {filelist_tb} {fake_top}"},
    )
    with pytest.raises(InfraError) as exc_info:
        run_check(
            config=cfg,
            files=files,
            backend=get_backend("vcs", cfg.sim),
            provider=_local_provider(proj),
        )
    assert exc_info.value.code == "CHECK_UNAVAILABLE"


# ------------------------------------------------------------------ incremental RTL


def test_incremental_rtl_propagation_fallback_and_state_update(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj"
    (proj / "inc").mkdir(parents=True)
    (proj / "inc" / "types.svh").write_text("typedef logic [7:0] u8_t;\n", encoding="utf-8")
    (proj / "rtl").mkdir(parents=True)
    (proj / "rtl" / "pkg_a.sv").write_text(
        "package pkg_a;\n  localparam int A = 1;\nendpackage\n", encoding="utf-8"
    )
    (proj / "rtl" / "pkg_b.sv").write_text(
        "package pkg_b;\n  import pkg_a::*;\n  localparam int B = A + 1;\nendpackage\n",
        encoding="utf-8",
    )
    (proj / "rtl" / "mod_x.sv").write_text(
        "module mod_x;\n  import pkg_b::*;\nendmodule\n", encoding="utf-8"
    )
    (proj / "rtl" / "mod_y.sv").write_text(
        'module mod_y;\n  `include "types.svh"\nendmodule\n', encoding="utf-8"
    )
    files = FileList(
        groups={
            "rtl": ["rtl/pkg_a.sv", "rtl/pkg_b.sv", "rtl/mod_x.sv", "rtl/mod_y.sv"],
            "tb": [],
            "top": [],
        },
        incdirs={"rtl": ["inc"], "tb": [], "top": []},
        defines={"rtl": [], "tb": [], "top": []},
        entries=[],
        libs={"rtl": [], "tb": [], "top": []},
    )

    stub_bin = tmp_path / "bin"
    stub_slang = _write_exec(stub_bin / "slang", "#!/usr/bin/env bash\necho '[]'\nexit 0\n")
    vlogan_flag = tmp_path / "vlogan_fail.flag"
    _write_exec(
        stub_bin / "vlogan",
        "#!/usr/bin/env bash\n"
        f"if [ -f {vlogan_flag} ]; then\n"
        "  echo '../../rtl/pkg_a.sv:1:1: error: syntax error'\n"
        "  exit 1\n"
        "fi\n"
        "exit 0\n",
    )
    monkeypatch.setenv("PATH", f"{stub_bin}:/usr/bin:/bin")

    cfg = _make_config(
        proj,
        simulator="vcs",
        check=CheckConfig(slang=str(stub_slang), faketop=False),
    )
    backend: Backend = get_backend("vcs")
    prov = _local_provider(proj)

    # 1. Missing baseline -> full compile and writes fast_state.json
    res1, rc1 = run_check(
        config=cfg, files=files, backend=backend, provider=prov, incremental_rtl=True
    )
    assert rc1 == 0
    assert res1.layers == ["slang", "incremental"]
    assert res1.incremental_files == [
        "rtl/pkg_a.sv",
        "rtl/pkg_b.sv",
        "rtl/mod_x.sv",
        "rtl/mod_y.sv",
    ]
    state_path = proj / FAST_STATE_REL
    state1 = read_fast_state(state_path)
    assert state1 is not None
    assert state1["schema"] == 1
    assert "rtl/pkg_a.sv" in state1["files"]  # type: ignore[operator]
    assert "inc/types.svh" in state1["files"]  # type: ignore[operator]

    # 2. Unchanged -> 0 files compiled
    res2, rc2 = run_check(
        config=cfg, files=files, backend=backend, provider=prov, incremental_rtl=True
    )
    assert rc2 == 0
    assert res2.incremental_files == []

    # 3. Edit pkg_a -> propagates transitively to pkg_b and mod_x (not mod_y!)
    (proj / "rtl" / "pkg_a.sv").write_text(
        "package pkg_a;\n  localparam int A = 2;\nendpackage\n", encoding="utf-8"
    )
    plan_pkg = plan_rtl_incremental(cfg, files, backend, prov)
    assert plan_pkg.full_fallback is False
    assert plan_pkg.files_to_compile == ["rtl/pkg_a.sv", "rtl/pkg_b.sv", "rtl/mod_x.sv"]

    # 3b. Failed compile does NOT update fast_state.json!
    vlogan_flag.write_text("1", encoding="utf-8")
    res_fail, rc_fail = run_check(
        config=cfg, files=files, backend=backend, provider=prov, incremental_rtl=True
    )
    assert rc_fail == 1
    assert res_fail.ok is False
    assert read_fast_state(state_path) == state1  # unchanged!

    # 3c. Subsequent success updates fast_state.json
    vlogan_flag.unlink()
    res3, rc3 = run_check(
        config=cfg, files=files, backend=backend, provider=prov, incremental_rtl=True
    )
    assert rc3 == 0
    assert res3.incremental_files == ["rtl/pkg_a.sv", "rtl/pkg_b.sv", "rtl/mod_x.sv"]
    assert read_fast_state(state_path) != state1

    # 4. Edit non-macro header -> only mod_y compiled
    (proj / "inc" / "types.svh").write_text("typedef logic [15:0] u8_t;\n", encoding="utf-8")
    res4, rc4 = run_check(
        config=cfg, files=files, backend=backend, provider=prov, incremental_rtl=True
    )
    assert rc4 == 0
    assert res4.incremental_files == ["rtl/mod_y.sv"]

    # 5. Macro addition in header -> full fallback
    (proj / "inc" / "types.svh").write_text(
        "`define WIDTH 16\ntypedef logic [15:0] u8_t;\n", encoding="utf-8"
    )
    plan_macro = plan_rtl_incremental(cfg, files, backend, prov)
    assert plan_macro.full_fallback is True
    assert "macro" in (plan_macro.fallback_reason or "")
    assert plan_macro.files_to_compile == [
        "rtl/pkg_a.sv",
        "rtl/pkg_b.sv",
        "rtl/mod_x.sv",
        "rtl/mod_y.sv",
    ]

    # 6. Options hash change -> full fallback
    cfg_opt = _make_config(
        proj,
        simulator="vcs",
        defines=["NEW_DEF"],
        check=CheckConfig(slang=str(stub_slang), faketop=False),
    )
    plan_opt = plan_rtl_incremental(cfg_opt, files, backend, prov)
    assert plan_opt.full_fallback is True
    assert "options" in (plan_opt.fallback_reason or "")

    # 7. Ambiguous header across 2 incdirs -> full fallback
    (proj / "inc" / "types.svh").write_text("typedef logic [15:0] u8_t;\n", encoding="utf-8")
    (proj / "inc2").mkdir()
    (proj / "inc2" / "types.svh").write_text("typedef logic [31:0] u8_t;\n", encoding="utf-8")
    files_ambig = FileList(
        groups=files.groups,
        incdirs={"rtl": ["inc", "inc2"], "tb": [], "top": []},
        defines=files.defines,
        entries=files.entries,
        libs=files.libs,
    )
    # Seed baseline with matching options_hash first, then test ambiguous header detection
    cfg_ambig = _make_config(proj, simulator="vcs", check=CheckConfig(slang=str(stub_slang)))
    res_ambig, rc_ambig = run_check(
        config=cfg_ambig,
        files=files_ambig,
        backend=backend,
        provider=prov,
        incremental_rtl=True,
    )
    assert rc_ambig == 0
    assert res_ambig.ok is True
    (proj / "inc" / "types.svh").write_text("typedef logic [7:0] u8_t;\n", encoding="utf-8")
    plan_ambig = plan_rtl_incremental(cfg_ambig, files_ambig, backend, prov)
    assert plan_ambig.full_fallback is True
    assert "ambiguous" in (plan_ambig.fallback_reason or "")
    assert len(plan_ambig.files_to_compile) == 4

    # 8. Unsupported fast_state schema raises ConfigError (N-08)
    bad_state = json.loads(state_path.read_text(encoding="utf-8"))
    bad_state["schema"] = 99
    state_path.write_text(json.dumps(bad_state), encoding="utf-8")
    with pytest.raises(ConfigError) as exc_schema:
        read_fast_state(state_path)
    assert exc_schema.value.code == "CFG_INVALID"
    assert isinstance(os.fspath(state_path), str)
    _ = CheckResult(ok=True)


def test_xrun_incremental_questa_compound_and_layer_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = tmp_path / "proj"
    files = _make_stub_project(proj)
    stub_bin = tmp_path / "bin"
    stub_slang = _write_exec(stub_bin / "slang", "#!/usr/bin/env bash\necho '[]'\nexit 0\n")
    xrun_args = tmp_path / "xrun_args.txt"
    _write_exec(
        stub_bin / "xrun",
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" > {xrun_args}\nexit 0\n",
    )
    _write_exec(stub_bin / "vlib", '#!/usr/bin/env bash\nmkdir -p "$1"\nexit 0\n')
    _write_exec(stub_bin / "vlog", "#!/usr/bin/env bash\nexit 0\n")
    _write_exec(stub_bin / "vopt", "#!/usr/bin/env bash\nexit 0\n")
    _write_exec(stub_bin / "qrun", "#!/usr/bin/env bash\nexit 0\n")
    monkeypatch.setenv("PATH", f"{stub_bin}:/usr/bin:/bin")

    # 1. slang_only skips faketop and incremental
    cfg_xrun = _make_config(proj, simulator="xrun", check=CheckConfig(slang=str(stub_slang)))
    prov = _local_provider(proj)
    res_only, rc_only = run_check(
        config=cfg_xrun,
        files=files,
        backend=get_backend("xrun"),
        provider=prov,
        slang_only=True,
        incremental_rtl=True,
    )
    assert rc_only == 0
    assert res_only.layers == ["slang"]

    # 2. no_faketop skips faketop and runs xrun incremental
    res_xrun, rc_xrun = run_check(
        config=cfg_xrun,
        files=files,
        backend=get_backend("xrun"),
        provider=prov,
        no_faketop=True,
        incremental_rtl=True,
    )
    assert rc_xrun == 0
    assert res_xrun.layers == ["slang", "incremental"]
    assert res_xrun.incremental_files == ["rtl/axi_pkg.sv", "rtl/axi_slv.sv"]
    assert xrun_args.read_text(encoding="utf-8").splitlines() == [
        "-64bit",
        "-sv",
        "-compile",
        "-f",
        "filelist.rtl.inc.f",
        "-xmlibdirpath",
        ".",
    ]

    # 3. questa compound && compile_only + incremental warns and skips cleanly
    cfg_questa = _make_config(proj, simulator="questa", check=CheckConfig(slang=str(stub_slang)))
    res_q, rc_q = run_check(
        config=cfg_questa,
        files=files,
        backend=get_backend("questa"),
        provider=prov,
        incremental_rtl=True,
    )
    assert rc_q == 0
    assert res_q.layers == ["slang", "faketop"]
    assert any("only supported for vcs/xrun" in w for w in res_q.warnings)


def test_faketop_remote_and_untrackable_macro_include(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    remote_root = tmp_path / "remote"
    files = _make_stub_project(proj)

    def remote_handler(argv: list[str], cwd: str, rroot: Path) -> int:
        if cwd == ".solouvm/check":
            check_dir = rroot / ".solouvm" / "check"
            (check_dir / "slang.stdout").write_text("[]\n", encoding="utf-8")
            (check_dir / "slang.stderr").write_text("", encoding="utf-8")
            return 0
        # faketop run_job.sh invocation at cwd="."
        assert argv[:2] == ["bash", ".solouvm/gen/bin/run_job.sh"]
        check_dir = rroot / ".solouvm" / "check"
        (check_dir / "check_faketop.log").write_text("compile ok\n", encoding="utf-8")
        return 0

    transport = _StubRemoteTransport(remote_root, handler=remote_handler)
    sync = _StubRemoteSync(remote_root)
    prov = Provider(name="farm", transport=transport, launcher=MakeLauncher(), sync=sync)
    cfg = _make_config(proj, simulator="vcs", check=CheckConfig(slang="slang", where="remote"))
    res, rc = run_check(config=cfg, files=files, backend=get_backend("vcs"), provider=prov)
    assert rc == 0
    assert res.where == "remote"
    assert res.layers == ["slang", "faketop"]
    assert (remote_root / ".solouvm" / "check" / "fake_top.sv").is_file()
    assert (remote_root / ".solouvm" / "check" / "filelist.tb.f").is_file()

    # Macro `include triggers untrackable full-compile fallback
    proj2 = tmp_path / "proj2"
    files2 = _make_stub_project(proj2)
    (proj2 / "rtl" / "axi_slv.sv").write_text(
        '`define HDR "defs.svh"\nmodule axi_slv;\n  `include `HDR\nendmodule\n',
        encoding="utf-8",
    )
    stub_slang = _write_exec(tmp_path / "bin" / "slang", "#!/usr/bin/env bash\necho '[]'\nexit 0\n")
    stub_vlogan = _write_exec(tmp_path / "bin" / "vlogan", "#!/usr/bin/env bash\nexit 0\n")
    _ = stub_vlogan
    cfg2 = _make_config(
        proj2, simulator="vcs", check=CheckConfig(slang=str(stub_slang), faketop=False)
    )
    # Seed baseline state first
    prov2 = _local_provider(proj2)
    backend_vcs = get_backend("vcs")
    plan_init = plan_rtl_incremental(cfg2, files2, backend_vcs, prov2)
    write_fast_state(proj2 / FAST_STATE_REL, plan_init.new_state)
    plan_untrack = plan_rtl_incremental(cfg2, files2, backend_vcs, prov2)
    assert plan_untrack.full_fallback is True
    assert "untrackable" in (plan_untrack.fallback_reason or "")


def test_incremental_detects_same_size_and_mtime_change(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    (proj / "rtl").mkdir(parents=True)
    dut = proj / "rtl" / "dut.sv"
    dut.write_text("module dut; wire a; endmodule\n", encoding="utf-8")
    files = FileList(
        groups={"rtl": ["rtl/dut.sv"], "tb": [], "top": []},
        incdirs={"rtl": [], "tb": [], "top": []},
        defines={"rtl": [], "tb": [], "top": []},
        entries=[FileEntry("rtl/dut.sv", "rtl", "rtl/dut.sv")],
        libs={"rtl": [], "tb": [], "top": []},
    )
    cfg = _make_config(proj, simulator="vcs")
    prov = _local_provider(proj)
    backend = get_backend("vcs")

    plan1 = plan_rtl_incremental(cfg, files, backend, prov)
    write_fast_state(proj / FAST_STATE_REL, plan1.new_state)

    orig_st = dut.stat()
    dut.write_text("module dut; wire b; endmodule\n", encoding="utf-8")
    os.utime(dut, ns=(orig_st.st_atime_ns, orig_st.st_mtime_ns))

    plan2 = plan_rtl_incremental(cfg, files, backend, prov)
    assert plan2.files_to_compile == ["rtl/dut.sv"]
    assert plan2.changed_files == ["rtl/dut.sv"]


def test_check_path_helpers_handle_sibling_rtl(tmp_path: Path) -> None:
    from solouvm.check.slang import format_check_path, normalize_diag_path

    repo = tmp_path / "repo"
    rtl_dir = repo / "rtl"
    dv_dir = repo / "dv"
    rtl_dir.mkdir(parents=True)
    dv_dir.mkdir(parents=True)
    dut = rtl_dir / "dut.sv"
    dut.write_text("module dut; endmodule\n", encoding="utf-8")

    formatted = format_check_path(
        dv_dir,
        dv_dir.resolve(),
        "../rtl/dut.sv",
        expand_local=False,
    )
    assert formatted == "../../../rtl/dut.sv"
    assert normalize_diag_path("../../../rtl/dut.sv", dv_dir) == "../rtl/dut.sv"
    assert normalize_diag_path(str(dut), dv_dir) == Path(os.path.normpath(str(dut))).as_posix()
