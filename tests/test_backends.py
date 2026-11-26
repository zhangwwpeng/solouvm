"""Tests for solouvm.backends (task 6)."""

from __future__ import annotations

import dataclasses
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from solouvm.backends import BACKENDS, REQUIRED_FIELDS, Backend, fake_sim_source, get_backend
from solouvm.collect import collect_cases, collect_files, load_filelist
from solouvm.config import load_config
from solouvm.errors import ConfigError
from solouvm.pipeline import run_regression
from solouvm.plan import build_plan
from solouvm.providers import get_provider

FAKE_SIM = Path(__file__).resolve().parent.parent / "solouvm" / "backends" / "fake_sim.sh"
SUMMARY_RE = re.compile(r"^\s*UVM_(INFO|WARNING|ERROR|FATAL)\s*:\s*(\d+)\s*$")

CUSTOM = {
    "build": "qverilog -f {filelist} -top {top}",
    "run": "vsim -c {top}",
    "simv": "work",
    "seed": "-sv_seed {seed}",
    "wave": "-do wave.do",
    "compile_only": "vlog -sv -f {filelist_tb} {fake_top}",
}


# ---------- registry / get_backend ----------


def test_registry_keys():
    assert set(BACKENDS) == {"vcs", "xrun", "questa", "xezim", "verilator", "fake"}
    for name, backend in BACKENDS.items():
        assert backend.name == name


def test_vcs_error_regex():
    assert r"^Error-\[" in get_backend("vcs").error_regex


def test_builtin_without_overrides_is_builtin():
    assert get_backend("xrun") is BACKENDS["xrun"]
    assert get_backend("fake", {}) is BACKENDS["fake"]


def test_backend_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        BACKENDS["vcs"].simv = "x"  # type: ignore[misc]


def test_override_returns_new_object():
    b = get_backend("vcs", {"simv": "simv_debug"})
    assert b.simv == "simv_debug"
    assert b.name == "vcs"
    assert BACKENDS["vcs"].simv == "simv"
    assert b.build == BACKENDS["vcs"].build


def test_unknown_key():
    with pytest.raises(ConfigError) as exc:
        get_backend("vcs", {"bogus": 1})
    assert exc.value.code == "CFG_INVALID"
    assert "bogus" in str(exc.value)


def test_name_is_not_overridable():
    with pytest.raises(ConfigError) as exc:
        get_backend("vcs", {"name": "other"})
    assert exc.value.code == "CFG_INVALID"


def test_unknown_backend_lists_missing():
    with pytest.raises(ConfigError) as exc:
        get_backend("custom_sim")
    assert exc.value.code == "CFG_INVALID"
    msg = str(exc.value)
    assert "custom_sim" in msg
    for key in REQUIRED_FIELDS:
        assert key in msg


def test_unknown_backend_partial_lists_only_missing():
    partial = {k: v for k, v in CUSTOM.items() if k not in ("seed", "wave")}
    with pytest.raises(ConfigError) as exc:
        get_backend("custom_sim", partial)
    msg = str(exc.value)
    assert "seed" in msg and "wave" in msg
    assert "compile_only" not in msg


def test_custom_backend():
    b = get_backend("custom_sim", CUSTOM)
    assert isinstance(b, Backend)
    assert b.name == "custom_sim"
    assert b.run == "vsim -c {top}"
    assert b.cov_build == "" and b.cov_run == ""
    assert b.error_regex == () and b.license_regex == ()
    assert b.time_regex is None and b.version is None
    assert "custom_sim" not in BACKENDS


def test_custom_backend_with_optional_fields():
    b = get_backend(
        "custom_sim",
        {**CUSTOM, "error_regex": [r"^\*\* Error"], "version": "vsim -version"},
    )
    assert b.error_regex == (r"^\*\* Error",)
    assert b.version == "vsim -version"


def test_regex_list_converted_to_tuple():
    b = get_backend("fake", {"error_regex": ["^X"]})
    assert b.error_regex == ("^X",)
    assert BACKENDS["fake"].error_regex == (r"^FAKE-ERROR:",)
    b = get_backend("fake", {"license_regex": ["lic", "LIC"]})
    assert b.license_regex == ("lic", "LIC")


@pytest.mark.parametrize("value", ["^X", [1], ["^X", None], 3, None])
def test_error_regex_must_be_list_of_str(value):
    with pytest.raises(ConfigError) as exc:
        get_backend("fake", {"error_regex": value})
    assert exc.value.code == "CFG_INVALID"


def test_invalid_regex_rejected():
    with pytest.raises(ConfigError) as exc:
        get_backend("fake", {"error_regex": ["(unclosed"]})
    assert exc.value.code == "CFG_INVALID"
    with pytest.raises(ConfigError):
        get_backend("fake", {"time_regex": "[bad"})


def test_command_field_type_checked():
    with pytest.raises(ConfigError) as exc:
        get_backend("vcs", {"simv": 3})
    assert exc.value.code == "CFG_INVALID"


def test_command_must_be_string():
    with pytest.raises(ConfigError):
        get_backend("vcs", {"run": ["{simv}", "+arg"]})


def test_optional_fields_accept_none():
    b = get_backend("vcs", {"time_regex": None, "version": None})
    assert b.time_regex is None and b.version is None


def test_builtin_regexes_compile():
    for backend in BACKENDS.values():
        for pattern in (*backend.error_regex, *backend.license_regex):
            re.compile(pattern)
        if backend.time_regex:
            re.compile(backend.time_regex)


def test_builtin_values():
    vcs, xrun, xezim, fake = BACKENDS["vcs"], BACKENDS["xrun"], BACKENDS["xezim"], BACKENDS["fake"]
    assert vcs.seed == "+ntb_random_seed={seed}"
    assert "-ntb_opts uvm-1.2" in vcs.build
    assert xrun.seed == "-svseed {seed}"
    assert "-uvm" in xrun.build
    assert xezim.build == "xezim --compile -F {filelist} -s {top}"
    assert xezim.run == "xezim -F {filelist} -s {top}"
    assert xezim.seed == "-svseed {seed}"
    assert xezim.wave == "--fst wave.fst"
    assert xezim.cov_build == "--code-coverage"
    assert xezim.cov_run == "--code-coverage"
    assert xezim.compile_only == "xezim --compile -F {filelist_tb} {fake_top} -s solouvm_fake_top"
    assert xezim.version == "xezim -V"
    assert fake.build == "bash {root}/.solouvm/gen/bin/fake_sim.sh"
    assert fake.version.endswith("+SOLOUVM_FAKE_MODE=version")
    assert "--Mdir {build_dir}" in BACKENDS["verilator"].build
    assert re.search(xrun.time_regex, "Simulation complete via $finish(1) at time 52340 NS + 0")
    assert re.search(vcs.time_regex, "$finish called from file x.sv, line 3 at time 1200")
    assert re.search(xezim.time_regex, "$finish called at time 50000ns")
    with pytest.raises(ConfigError) as exc:
        get_backend("iverilog")
    assert exc.value.code == "CFG_INVALID"


def test_fake_sim_source():
    text = fake_sim_source()
    assert "SOLOUVM_FAKE_MODE" in text
    assert text == FAKE_SIM.read_text(encoding="utf-8")


# ---------- fake_sim.sh ----------


def run_fake(mode: str | None, timeout: float = 30) -> subprocess.CompletedProcess[str]:
    args = ["bash", str(FAKE_SIM), "+ntb_random_seed=1"]
    if mode is not None:
        args.append(f"+SOLOUVM_FAKE_MODE={mode}")
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def summary_counts(out: str) -> dict[str, int]:
    _, sep, tail = out.partition("Report counts by severity")
    assert sep
    counts = {}
    for line in tail.splitlines():
        m = SUMMARY_RE.match(line)
        if m:
            counts[m.group(1)] = int(m.group(2))
    return counts


@pytest.mark.parametrize("mode", ["pass", None])
def test_fake_pass(mode):
    r = run_fake(mode)
    assert r.returncode == 0
    assert "Report Summary" in r.stdout
    assert "[TEST_DONE]" in r.stdout and "@ 50000ns" in r.stdout
    counts = summary_counts(r.stdout)
    assert counts["ERROR"] == 0 and counts["FATAL"] == 0 and counts["WARNING"] == 0
    assert re.search(BACKENDS["fake"].time_regex, r.stdout).group(1) == "50000"


def test_fake_uvm_error():
    r = run_fake("uvm_error")
    assert r.returncode == 0
    assert re.search(r"^UVM_ERROR \S+ @ ", r.stdout, re.M)
    counts = summary_counts(r.stdout)
    assert counts["ERROR"] == 1 and counts["FATAL"] == 0


def test_fake_uvm_fatal():
    r = run_fake("uvm_fatal")
    assert r.returncode == 0
    assert re.search(r"^UVM_FATAL \S+ @ ", r.stdout, re.M)
    counts = summary_counts(r.stdout)
    assert counts["FATAL"] == 1 and counts["ERROR"] == 0


def test_fake_no_summary():
    r = run_fake("no_summary")
    assert r.returncode == 0
    assert "UVM_INFO" in r.stdout
    assert "Report Summary" not in r.stdout


def test_fake_rc3():
    r = run_fake("rc3")
    assert r.returncode == 3
    assert "UVM_INFO" in r.stdout


def test_fake_hang():
    with pytest.raises(subprocess.TimeoutExpired):
        run_fake("hang", timeout=2)


def test_fake_segv():
    r = run_fake("segv")
    assert r.returncode == -11


def test_fake_license():
    r = run_fake("license")
    assert r.returncode == 1
    assert "Failed to obtain license" in r.stdout
    assert re.search(BACKENDS["fake"].license_regex[0], r.stdout)


def test_fake_nonuvm_pass():
    r = run_fake("nonuvm_pass")
    assert r.returncode == 0
    assert "SOLOUVM_DONE status=pass" in r.stdout
    # "SOLOUVM_DONE" 本身含子串 UVM_，所以按词首匹配
    assert not re.search(r"\bUVM_", r.stdout)


def test_fake_version():
    r = run_fake("version")
    assert r.returncode == 0
    assert r.stdout.strip() == "FAKE-SIM 1.0"


def test_fake_unknown_mode():
    r = run_fake("bogus")
    assert r.returncode == 2
    assert "FAKE-ERROR:" in r.stdout
    assert re.search(BACKENDS["fake"].error_regex[0], r.stdout, re.M)


@pytest.mark.skipif(shutil.which("verilator") is None, reason="verilator not installed")
def test_verilator_minimal_compile_and_run(tmp_path: Path) -> None:
    proj = tmp_path / "vproj"
    proj.mkdir()
    (proj / "tb_top.sv").write_text(
        "module tb_top;\n"
        "  initial begin\n"
        '    $display("SOLOUVM_DONE status=pass");\n'
        "    $finish;\n"
        "  end\n"
        "endmodule\n",
        encoding="utf-8",
    )
    (proj / "filelist.py").write_text(
        'FILES = {"rtl": ["tb_top.sv"], "tb": [], "top": []}\n',
        encoding="utf-8",
    )
    (proj / "solouvm.conf.py").write_text(
        'CFG = {"project": "vdemo", "simulator": "verilator", "top": "tb_top"}\n',
        encoding="utf-8",
    )
    (proj / "case.py").write_text(
        'from solouvm import Case\nCASES = [Case(name="smoke", expect=[])]\n',
        encoding="utf-8",
    )

    cfg = load_config(proj)
    files = collect_files(cfg.root, load_filelist(cfg.root))
    plan = build_plan(
        cases=collect_cases(cfg),
        config=cfg,
        selection={"seeds": "1"},
        run_id="r-verilator",
    )
    results, payload, code = run_regression(
        config=cfg,
        plan=plan,
        provider=get_provider(cfg),
        files=files,
        jobs=1,
    )
    assert code == 0, results
    assert payload is not None
    assert results[0].status == "pass"


def test_xezim_stub_compile_and_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    invocations_log = tmp_path / "xezim_calls.log"
    stub_xezim = bin_dir / "xezim"
    stub_xezim.write_text(
        f"""#!/usr/bin/env python3
import os, sys
from pathlib import Path

args = sys.argv[1:]
with open({str(invocations_log)!r}, "a", encoding="utf-8") as fh:
    fh.write(f"cwd={{os.getcwd()}} args={{' '.join(args)}}\\n")

if "-F" in args:
    f_path = Path(args[args.index("-F") + 1])
    assert f_path.is_file(), f"missing -F file: {{f_path}}"
    entries = [line.strip() for line in f_path.read_text(encoding="utf-8").splitlines()
               if line.strip() and not line.startswith("+")]
    for entry in entries:
        assert (f_path.parent / entry).is_file(), f"missing target: {{f_path.parent / entry}}"

if "--compile" in args:
    sys.exit(0)

print("UVM_INFO tb_top.sv(10) @ 0ns: reporter [RNTST] Running test smoke_test...")
print("UVM_INFO @ 50000ns: reporter [TEST_DONE] 'run' phase done")
print("--- UVM Report Summary ---")
print("** Report counts by severity")
print("UVM_INFO :    2")
print("UVM_WARNING :    0")
print("UVM_ERROR :    0")
print("UVM_FATAL :    0")
""",
        encoding="utf-8",
    )
    stub_xezim.chmod(stub_xezim.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

    proj = tmp_path / "xproj"
    proj.mkdir()
    (proj / "tb_top.sv").write_text("module tb_top; endmodule\n", encoding="utf-8")
    (proj / "filelist.py").write_text(
        'FILES = {"rtl": ["tb_top.sv"], "tb": [], "top": []}\n',
        encoding="utf-8",
    )
    (proj / "solouvm.conf.py").write_text(
        'CFG = {"project": "xdemo", "simulator": "xezim", "top": "tb_top", '
        '"uvm_test": "smoke_test"}\n',
        encoding="utf-8",
    )
    (proj / "case.py").write_text(
        (
            "from solouvm import Case\n"
            'CASES = [Case(name="smoke", uvm_test="smoke_test", expect=[])]\n'
        ),
        encoding="utf-8",
    )

    cfg = load_config(proj)
    files = collect_files(cfg.root, load_filelist(cfg.root))
    plan = build_plan(
        cases=collect_cases(cfg),
        config=cfg,
        selection={"seeds": "1"},
        run_id="r-xezim",
    )
    results, payload, code = run_regression(
        config=cfg,
        plan=plan,
        provider=get_provider(cfg),
        files=files,
        jobs=1,
    )
    assert code == 0, results
    assert payload is not None
    assert results[0].status == "pass"

    calls = invocations_log.read_text(encoding="utf-8").splitlines()
    assert any("--compile -F filelist.f -s tb_top" in line for line in calls)
    assert any(
        re.search(r"-F \.\./\.\./\.\./\.\./build/default/[^/]+/filelist\.f -s tb_top", line)
        and "-svseed 1" in line
        and "+UVM_TESTNAME=smoke_test" in line
        for line in calls
    )
