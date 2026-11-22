"""Tests for solouvm.buildkey (architecture.md §4.2, D-05, D-06)."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from solouvm import buildkey
from solouvm.backends import get_backend
from solouvm.buildkey import BUILDKEY_LEN, BuildKeyResult, compute_build_key, compute_build_keys
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
from solouvm.errors import ConfigError
from solouvm.model import Checks


def _make_config(root: Path, **overrides: object) -> Config:
    builds = {
        "default": BuildConfig(),
        "cov": BuildConfig(coverage=True),
    }
    defaults: dict[str, object] = {
        "root": root,
        "project": "demo",
        "simulator": "vcs",
        "top": "tb_top",
        "uvm_test": "smoke_test",
        "incdirs": [],
        "defines": [],
        "builds": builds,
        "regress": RegressConfig(),
        "run": RunConfig(),
        "checks": Checks(),
        "job": JobHooks(),
        "sim": {},
        "providers": {"local": ProviderConfig(name="local")},
        "provider": "local",
        "check": CheckConfig(),
        "case_dirs": [],
        "system_doc": [],
        "triage": TriageConfig(),
        "doc": DocConfig(),
        "history": HistoryConfig(),
        "limits": LimitsConfig(),
        "plugins": [],
        "uvm": UvmConfig(),
    }
    defaults.update(overrides)
    return Config(**defaults)  # type: ignore[arg-type]


def _make_files(root: Path) -> FileList:
    (root / "rtl").mkdir(parents=True, exist_ok=True)
    (root / "tb").mkdir(parents=True, exist_ok=True)
    (root / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    (root / "tb" / "tb_top.sv").write_text("module tb_top; endmodule\n", encoding="utf-8")
    return FileList(
        groups={"rtl": ["rtl/dut.sv"], "tb": [], "top": ["tb/tb_top.sv"]},
        incdirs={"rtl": [], "tb": [], "top": []},
        defines={"rtl": [], "tb": [], "top": []},
        entries=[
            FileEntry("rtl/dut.sv", "rtl", "rtl/dut.sv"),
            FileEntry("tb/tb_top.sv", "top", "tb/tb_top.sv"),
        ],
        libs={"rtl": [], "tb": [], "top": []},
    )


def test_buildkey_format_and_determinism(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    files = _make_files(tmp_path)
    backend = get_backend("vcs")

    r1 = compute_build_keys(
        config=cfg, files=files, backend=backend, modes=["default"], run_id="run-1"
    )
    r2 = compute_build_keys(
        config=cfg, files=files, backend=backend, modes=["default"], run_id="run-2"
    )

    assert isinstance(r1, BuildKeyResult)
    assert set(r1.keys) == {"default"}
    assert re.fullmatch(rf"[0-9a-f]{{{BUILDKEY_LEN}}}", r1.keys["default"])
    assert r1.keys == r2.keys
    assert not r1.non_reusable
    assert (
        compute_build_key(config=cfg, files=files, backend=backend, mode="default", run_id="run-3")
        == r1.keys["default"]
    )


def test_buildkey_changes_with_metadata_and_compile_inputs(tmp_path: Path) -> None:
    files = _make_files(tmp_path)
    backend = get_backend("vcs")
    base_cfg = _make_config(tmp_path)
    base = compute_build_key(
        config=base_cfg, files=files, backend=backend, mode="default", run_id="r1"
    )

    # Changing mode name (even with identical BuildConfig) changes key
    cfg_alt_mode = _make_config(tmp_path, builds={"default": BuildConfig(), "alt": BuildConfig()})
    assert (
        compute_build_key(
            config=cfg_alt_mode, files=files, backend=backend, mode="alt", run_id="r1"
        )
        != base
    )

    # top, defines, incdirs, compile_opts, coverage, backend, sim_version, solouvm_version, setup
    assert (
        compute_build_key(
            config=_make_config(tmp_path, top="other_top"),
            files=files,
            backend=backend,
            mode="default",
            run_id="r1",
        )
        != base
    )
    assert (
        compute_build_key(
            config=_make_config(tmp_path, defines=["SIM"]),
            files=files,
            backend=backend,
            mode="default",
            run_id="r1",
        )
        != base
    )
    (tmp_path / "inc").mkdir()
    assert (
        compute_build_key(
            config=_make_config(tmp_path, incdirs=["inc"]),
            files=files,
            backend=backend,
            mode="default",
            run_id="r1",
        )
        != base
    )
    assert (
        compute_build_key(
            config=_make_config(
                tmp_path, builds={"default": BuildConfig(compile_opts=["+define+X"])}
            ),
            files=files,
            backend=backend,
            mode="default",
            run_id="r1",
        )
        != base
    )
    assert (
        compute_build_key(
            config=_make_config(tmp_path, builds={"default": BuildConfig(defines=["D1"])}),
            files=files,
            backend=backend,
            mode="default",
            run_id="r1",
        )
        != base
    )
    assert (
        compute_build_key(
            config=_make_config(tmp_path, builds={"default": BuildConfig(coverage=True)}),
            files=files,
            backend=backend,
            mode="default",
            run_id="r1",
        )
        != base
    )
    assert (
        compute_build_key(
            config=base_cfg,
            files=files,
            backend=get_backend("vcs", {"build": "vcs -sverilog -f {filelist}"}),
            mode="default",
            run_id="r1",
        )
        != base
    )
    assert (
        compute_build_key(
            config=base_cfg,
            files=files,
            backend=get_backend("xrun"),
            mode="default",
            run_id="r1",
        )
        != base
    )
    assert (
        compute_build_key(
            config=base_cfg,
            files=files,
            backend=backend,
            mode="default",
            run_id="r1",
            sim_version="V-2025.06",
        )
        != base
    )
    assert (
        compute_build_key(
            config=base_cfg,
            files=files,
            backend=backend,
            mode="default",
            run_id="r1",
            solouvm_version="0.2.0",
        )
        != base
    )
    assert (
        compute_build_key(
            config=_make_config(
                tmp_path,
                providers={"local": ProviderConfig(name="local", setup="module load vcs")},
            ),
            files=files,
            backend=backend,
            mode="default",
            run_id="r1",
        )
        != base
    )


def test_buildkey_ignores_run_opts_and_timeout_and_cov_build_when_no_cov(tmp_path: Path) -> None:
    files = _make_files(tmp_path)
    cfg1 = _make_config(tmp_path)
    cfg2 = _make_config(
        tmp_path,
        builds={"default": BuildConfig(run_opts=["+UVM_VERBOSITY=UVM_HIGH"], timeout="4h")},
    )
    b1 = get_backend("vcs")
    b2 = get_backend("vcs", {"cov_build": "-cm line"})

    k1 = compute_build_key(config=cfg1, files=files, backend=b1, mode="default", run_id="r1")
    k2 = compute_build_key(config=cfg2, files=files, backend=b1, mode="default", run_id="r1")
    k3 = compute_build_key(config=cfg1, files=files, backend=b2, mode="default", run_id="r1")
    assert k1 == k2 == k3

    # When coverage=True, changing cov_build DOES change the key
    cov_cfg = _make_config(tmp_path, builds={"cov": BuildConfig(coverage=True)})
    kc1 = compute_build_key(config=cov_cfg, files=files, backend=b1, mode="cov", run_id="r1")
    kc2 = compute_build_key(config=cov_cfg, files=files, backend=b2, mode="cov", run_id="r1")
    assert kc1 != kc2


def test_buildkey_changes_with_file_incdir_and_lib_contents(tmp_path: Path) -> None:
    (tmp_path / "inc" / "sub").mkdir(parents=True)
    inc_hdr = tmp_path / "inc" / "sub" / "defs.svh"
    inc_hdr.write_text("`define WIDTH 8\n", encoding="utf-8")
    (tmp_path / "libs" / "cells").mkdir(parents=True)
    lib_v = tmp_path / "libs" / "prim.v"
    lib_v.write_text("module prim; endmodule\n", encoding="utf-8")
    lib_y_cell = tmp_path / "libs" / "cells" / "buf.v"
    lib_y_cell.write_text("module buf_cell; endmodule\n", encoding="utf-8")

    files = _make_files(tmp_path)
    files.incdirs["rtl"] = ["inc"]
    files.libs["rtl"] = ["-v libs/prim.v", "-y libs/cells"]
    cfg = _make_config(tmp_path)
    backend = get_backend("vcs")

    k0 = compute_build_key(config=cfg, files=files, backend=backend, mode="default", run_id="r1")

    # Edit source file
    (tmp_path / "rtl" / "dut.sv").write_text("module dut; wire a; endmodule\n", encoding="utf-8")
    k1 = compute_build_key(config=cfg, files=files, backend=backend, mode="default", run_id="r1")
    assert k1 != k0

    # Edit recursive incdir header
    inc_hdr.write_text("`define WIDTH 16\n", encoding="utf-8")
    k2 = compute_build_key(config=cfg, files=files, backend=backend, mode="default", run_id="r1")
    assert k2 != k1

    # Edit -v library file
    lib_v.write_text("module prim; wire w; endmodule\n", encoding="utf-8")
    k3 = compute_build_key(config=cfg, files=files, backend=backend, mode="default", run_id="r1")
    assert k3 != k2

    # Edit -y directory file
    lib_y_cell.write_text("module buf_cell; wire y; endmodule\n", encoding="utf-8")
    k4 = compute_build_key(config=cfg, files=files, backend=backend, mode="default", run_id="r1")
    assert k4 != k3

    # Reorder files in group
    (tmp_path / "rtl" / "other.sv").write_text("module other; endmodule\n", encoding="utf-8")
    files_a = FileList(
        groups={"rtl": ["rtl/dut.sv", "rtl/other.sv"], "tb": [], "top": ["tb/tb_top.sv"]},
        incdirs={"rtl": [], "tb": [], "top": []},
        defines={"rtl": [], "tb": [], "top": []},
        entries=[],
    )
    files_b = FileList(
        groups={"rtl": ["rtl/other.sv", "rtl/dut.sv"], "tb": [], "top": ["tb/tb_top.sv"]},
        incdirs={"rtl": [], "tb": [], "top": []},
        defines={"rtl": [], "tb": [], "top": []},
        entries=[],
    )
    assert compute_build_key(
        config=cfg, files=files_a, backend=backend, mode="default", run_id="r1"
    ) != compute_build_key(config=cfg, files=files_b, backend=backend, mode="default", run_id="r1")


def test_buildkey_hash_cache_hit_and_corrupt_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _make_config(tmp_path)
    files = _make_files(tmp_path)
    backend = get_backend("vcs")

    k1 = compute_build_key(config=cfg, files=files, backend=backend, mode="default", run_id="r1")
    cache_file = tmp_path / ".solouvm" / "cache" / "hash.json"
    assert cache_file.is_file()
    cache_data = json.loads(cache_file.read_text(encoding="utf-8"))
    assert cache_data["schema"] == 1
    assert "rtl/dut.sv" in cache_data["entries"]

    # Second run with unchanged files must NOT re-hash any files
    calls = 0
    real_sha1 = buildkey._sha1_file

    def counting_sha1(path: Path) -> str:
        nonlocal calls
        calls += 1
        return real_sha1(path)

    monkeypatch.setattr(buildkey, "_sha1_file", counting_sha1)
    k2 = compute_build_key(config=cfg, files=files, backend=backend, mode="default", run_id="r2")
    assert k2 == k1
    assert calls == 0

    # Corrupt cache is ignored and rebuilt cleanly
    cache_file.write_text("{corrupt json", encoding="utf-8")
    k3 = compute_build_key(config=cfg, files=files, backend=backend, mode="default", run_id="r3")
    assert k3 == k1
    assert calls == 2
    rebuilt = json.loads(cache_file.read_text(encoding="utf-8"))
    assert rebuilt["schema"] == 1
    assert "rtl/dut.sv" in rebuilt["entries"]


def test_buildkey_non_reusable_inputs_mix_run_id(tmp_path: Path) -> None:
    cfg = _make_config(
        tmp_path,
        builds={"default": BuildConfig(), "envmode": BuildConfig(incdirs=["${VIP_INC}"])},
    )
    files = _make_files(tmp_path)
    backend = get_backend("vcs")

    # env var incdir affects only envmode
    res1 = compute_build_keys(
        config=cfg, files=files, backend=backend, modes=["default", "envmode"], run_id="run-A"
    )
    res2 = compute_build_keys(
        config=cfg, files=files, backend=backend, modes=["default", "envmode"], run_id="run-B"
    )
    assert res1.keys["default"] == res2.keys["default"]
    assert "default" not in res1.non_reusable
    assert res1.keys["envmode"] != res2.keys["envmode"]
    assert "envmode" in res1.non_reusable

    # outside_root file and missing file make build non-reusable
    outside_files = FileList(
        groups={"rtl": ["rtl/dut.sv", "/opt/vip/ext.sv"], "tb": [], "top": ["rtl/missing.sv"]},
        incdirs={"rtl": [], "tb": [], "top": []},
        defines={"rtl": [], "tb": [], "top": []},
        entries=[
            FileEntry("rtl/dut.sv", "rtl", "rtl/dut.sv"),
            FileEntry("/opt/vip/ext.sv", "rtl", "/opt/vip/ext.sv", outside_root=True),
            FileEntry("rtl/missing.sv", "top", "rtl/missing.sv"),
        ],
        libs={"rtl": ["-v $LIB_V", "-y /opt/y_dir"], "tb": [], "top": []},
    )
    r_out_1 = compute_build_keys(
        config=cfg, files=outside_files, backend=backend, modes=["default"], run_id="run-A"
    )
    r_out_2 = compute_build_keys(
        config=cfg, files=outside_files, backend=backend, modes=["default"], run_id="run-B"
    )
    assert r_out_1.keys["default"] != r_out_2.keys["default"]
    reasons = "\n".join(r_out_1.non_reusable["default"])
    assert "/opt/vip/ext.sv" in reasons
    assert "rtl/missing.sv" in reasons
    assert "$LIB_V" in reasons
    assert "/opt/y_dir" in reasons


def test_buildkey_unknown_mode_raises(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    files = _make_files(tmp_path)
    with pytest.raises(ConfigError) as exc:
        compute_build_keys(
            config=cfg, files=files, backend=get_backend("vcs"), modes=["bogus"], run_id="r1"
        )
    assert exc.value.code == "CFG_INVALID"


def test_buildkey_detects_same_size_and_mtime_content_replacement(tmp_path: Path) -> None:
    cfg = _make_config(tmp_path)
    files = _make_files(tmp_path)
    backend = get_backend("vcs")
    dut = tmp_path / "rtl" / "dut.sv"
    dut.write_text("module dut; wire a; endmodule\n", encoding="utf-8")

    k1 = compute_build_key(config=cfg, files=files, backend=backend, mode="default", run_id="r1")
    orig_st = dut.stat()
    dut.write_text("module dut; wire b; endmodule\n", encoding="utf-8")
    os.utime(dut, ns=(orig_st.st_atime_ns, orig_st.st_mtime_ns))

    k2 = compute_build_key(config=cfg, files=files, backend=backend, mode="default", run_id="r2")
    assert k2 != k1


def test_buildkey_reusable_and_sensitive_for_sibling_rtl(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rtl_dir = repo / "rtl"
    dv_dir = repo / "dv"
    (rtl_dir / "inc").mkdir(parents=True)
    dv_dir.mkdir(parents=True)
    dut = rtl_dir / "dut.sv"
    hdr = rtl_dir / "inc" / "defs.svh"
    dut.write_text("module dut; endmodule\n", encoding="utf-8")
    hdr.write_text("`define W 8\n", encoding="utf-8")

    cfg = _make_config(dv_dir)
    files = FileList(
        groups={"rtl": ["../rtl/dut.sv"], "tb": [], "top": []},
        incdirs={"rtl": ["../rtl/inc"], "tb": [], "top": []},
        defines={"rtl": [], "tb": [], "top": []},
        entries=[FileEntry("../rtl/dut.sv", "rtl", "../rtl/filelist.py:dut.sv")],
        libs={"rtl": [], "tb": [], "top": []},
    )
    backend = get_backend("vcs")

    r1 = compute_build_keys(
        config=cfg, files=files, backend=backend, modes=["default"], run_id="r1"
    )
    r2 = compute_build_keys(
        config=cfg, files=files, backend=backend, modes=["default"], run_id="r2"
    )
    assert "default" not in r1.non_reusable
    assert r1.keys["default"] == r2.keys["default"]

    dut.write_text("module dut; logic x; endmodule\n", encoding="utf-8")
    r3 = compute_build_keys(
        config=cfg, files=files, backend=backend, modes=["default"], run_id="r3"
    )
    assert r3.keys["default"] != r1.keys["default"]

    hdr.write_text("`define W 16\n", encoding="utf-8")
    r4 = compute_build_keys(
        config=cfg, files=files, backend=backend, modes=["default"], run_id="r4"
    )
    assert r4.keys["default"] != r3.keys["default"]
