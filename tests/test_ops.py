"""Tests for solouvm.ops: validate, config view, schema, doctor and help."""

from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from solouvm.config import load_config
from solouvm.errors import ConfigError
from solouvm.init import init_project
from solouvm.ops import (
    SCHEMAS,
    format_doctor,
    format_show_config,
    get_schema,
    run_doctor,
    show_config,
    show_help,
    validate_project,
)
from solouvm.providers import get_provider


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def _minimal_project(root: Path) -> Path:
    init_project(root=root, simulator="fake", bare=False, agents=None)
    return root


# ---------------------------------------------------------------- validate (Z-10)


def test_validate_initialized_project_clean(tmp_path: Path) -> None:
    root = _minimal_project(tmp_path / "proj")
    report = validate_project(root)
    assert report.ok
    assert report.errors == []
    assert report.warnings == []
    assert report.format_text().endswith("0 errors, 0 warnings")


def test_validate_ready_missing_expect_and_expect_fail_true(tmp_path: Path) -> None:
    root = _minimal_project(tmp_path / "proj")
    _write(
        root / "case.py",
        """
        from solouvm import Case

        CASES = [
            Case(name="incomplete_ready", stage="ready"),
            Case(
                name="broad_xfail",
                stage="ready",
                intent="neg",
                method="drive bad",
                spec_refs=["1.1"],
                expect_fail=True,
            ),
        ]
        """,
    )
    report = validate_project(root)
    assert not report.ok
    text = report.format_text()
    assert "AI-02" in text
    assert "incomplete_ready" in text
    assert "J-20" in text
    assert "broad_xfail" in text
    assert len(report.warnings) >= 1


def test_validate_detects_dotf_bash_default_syntax(tmp_path: Path) -> None:
    root = _minimal_project(tmp_path / "proj")
    _write(root / "ip.f", "${VIP_ROOT:-/fallback}/a.sv\n")
    _write(root / "a.sv", "module a; endmodule\n")
    _write(root / "filelist.py", 'FILES = {"rtl": ["-f ip.f", "rtl/*.sv"]}\n')
    report = validate_project(root)
    assert not report.ok
    assert any("${VAR:-default}" in err.message for err in report.errors)


def test_validate_warns_duplicate_sv_modules(tmp_path: Path) -> None:
    root = _minimal_project(tmp_path / "proj")
    _write(root / "rtl" / "dup1.sv", "module dup_mod; endmodule\n")
    _write(root / "rtl" / "dup2.sv", "module dup_mod; endmodule\n")
    report = validate_project(root)
    assert report.ok  # warning only (F-08)
    assert any("dup_mod" in w.message and w.req == "F-08" for w in report.warnings)


def test_validate_reports_svh_inconsistency(tmp_path: Path) -> None:
    root = _minimal_project(tmp_path / "proj")
    _write(
        root / "verif" / "agents" / "axi_mst" / "axi_mst_orphan.sv",
        "class axi_mst_orphan; endclass\n",
    )
    report = validate_project(root)
    assert not report.ok
    assert any("U-11" in (issue.req or "") for issue in report.errors)


def test_validate_multi_error_aggregation_across_case_files(tmp_path: Path) -> None:
    root = _minimal_project(tmp_path / "proj")
    _write(
        root / "solouvm.conf.py",
        'CFG = {"simulator": "fake", "top": "tb_top", "case_dirs": ["cases"]}\n',
    )
    _write(root / "case.py", "CASES = []\n")
    _write(root / "cases" / "bad1.py", "raise RuntimeError('first broken file')\n")
    _write(
        root / "cases" / "bad2.py",
        "from solouvm import Case\nCASE = Case(name='r', stage='ready')\n",
    )
    report = validate_project(root)
    assert len(report.errors) >= 2
    files = {err.file for err in report.errors}
    assert "cases/bad1.py" in files
    assert "cases/bad2.py" in files


# ---------------------------------------------------------------- config view (C-08)


def test_show_config_sources_and_filtering(tmp_path: Path) -> None:
    root = _minimal_project(tmp_path / "proj")
    _write(root / "solouvm.local.py", 'LOCAL = {"regress": {"jobs": 32}}\n')
    cfg = load_config(root, cli={"regress": {"retry": 2}})
    entries = show_config(cfg)
    by_key = {item["key"]: item for item in entries}
    assert by_key["simulator"]["source"] == "conf"
    assert by_key["regress.jobs"]["value"] == 32
    assert by_key["regress.jobs"]["source"] == "local"
    assert by_key["regress.retry"]["value"] == 2
    assert by_key["regress.retry"]["source"] == "cli"
    assert by_key["limits.max_jobs"]["source"] == "default"

    filtered = show_config(cfg, key="regress")
    assert all(str(item["key"]).startswith("regress.") for item in filtered)
    rendered = format_show_config(filtered)
    assert "regress.jobs" in rendered
    assert "[local]" in rendered

    with pytest.raises(ConfigError) as exc:
        show_config(cfg, key="regress.nonexistent")
    assert exc.value.code == "CFG_UNKNOWN_KEY"


# ---------------------------------------------------------------- schema (Z-11)


def test_get_schema_all_names_and_errors() -> None:
    listing = get_schema(None)
    assert listing["available"] == sorted(SCHEMAS)
    assert len(listing["available"]) == 8
    for name in listing["available"]:
        schema = get_schema(name)
        assert schema["$schema"].startswith("https://json-schema.org/")
        assert "title" in schema

    with pytest.raises(ConfigError) as exc:
        get_schema("unknown_schema")
    assert exc.value.code == "CFG_INVALID"


# ---------------------------------------------------------------- doctor (Z-02)


def test_run_doctor_passes_on_fake_project(tmp_path: Path) -> None:
    root = _minimal_project(tmp_path / "proj")
    cfg = load_config(root)
    provider = get_provider(cfg)
    checks, ok = run_doctor(cfg, provider)
    assert ok
    names = {c.name for c in checks}
    assert {"python", "config", "make", "simulator", "b_tools_and_workdir"} <= names
    text = format_doctor(checks)
    assert "[info] (A) python:" in text
    assert "[ok]" not in text


def test_run_doctor_fails_when_simulator_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _minimal_project(tmp_path / "proj")
    _write(root / "solouvm.conf.py", 'CFG = {"simulator": "vcs", "top": "tb_top"}\n')
    cfg = load_config(root)
    provider = get_provider(cfg)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.path.dirname(os.sys.executable)}:/usr/bin:/bin")
    checks, ok = run_doctor(cfg, provider)
    assert not ok
    sim = next(c for c in checks if c.name == "simulator")
    assert not sim.ok
    assert sim.hint


# ---------------------------------------------------------------- help (Z-12)


def test_show_help_topics_and_fallback(tmp_path: Path) -> None:
    listing = show_help(None)
    assert "verdict" in listing["topics"]
    doc = show_help("verdict")
    assert doc["topic"] == "verdict"
    assert len(str(doc["content"])) > 100

    # Fallback works even when doc/ directory is absent
    empty_root = tmp_path / "nodoc"
    empty_root.mkdir()
    doc_fallback = show_help("cli", root=empty_root)
    assert doc_fallback["topic"] == "cli"
    assert doc_fallback["content"]

    with pytest.raises(ConfigError) as exc:
        show_help("nonexistent_topic")
    assert exc.value.code == "CFG_INVALID"
