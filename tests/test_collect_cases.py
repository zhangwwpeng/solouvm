"""Tests for Suite model and solouvm.collect.cases."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from solouvm import Bug, Case, Result, Stage, Suite
from solouvm.collect import CaseSource, case_file_for, collect_cases, collect_cases_with_sources
from solouvm.collect.cases import find_case_line
from solouvm.config import Config, load_config
from solouvm.errors import ConfigError


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def _raises(code: str, func, *args, **kwargs) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        func(*args, **kwargs)
    assert info.value.code == code, str(info.value)
    return info.value


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    _write(
        root / "solouvm.conf.py",
        """
        CFG = {
            "simulator": "fake",
            "top": "tb_top",
            "builds": {"default": {}, "gate": {}, "cov": {}},
        }
        """,
    )
    return root


def _load(root: Path) -> Config:
    return load_config(root / "solouvm.conf.py")


# ---------------------------------------------------------------- Suite model & expansion


def test_suite_validation() -> None:
    _raises("CASE_INVALID", Suite, name="bad__suite")
    _raises("CASE_INVALID", Suite, name="bad suite")
    _raises("CASE_INVALID", Suite, name="")
    _raises("CASE_INVALID", Suite, name="ok", stage="bogus")
    _raises("CASE_INVALID", Suite, name="ok", timeout="5d")
    _raises("CASE_INVALID", Suite, name="ok", hang="nope")
    _raises("CASE_INVALID", Suite, name="ok", retry=-1)
    _raises("CASE_INVALID", Suite, name="ok", retry=True)
    _raises("CASE_INVALID", Suite, name="ok", params={"1BAD": 1})
    _raises("CASE_INVALID", Suite, name="ok", params={"X": [1]})
    _raises("CASE_INVALID", Suite, name="ok", cases=["not_a_case"])  # type: ignore[list-item]
    _raises("CASE_INVALID", Suite, name="ok", cases="nope")  # type: ignore[arg-type]
    _raises("CASE_INVALID", Suite, name="ok", tags="nightly")  # type: ignore[arg-type]
    _raises("CASE_INVALID", Suite, name="ok", opts="--bad")  # type: ignore[arg-type]
    _raises("CASE_INVALID", Suite, name="ok", env={"A": 1})  # type: ignore[dict-item]
    _raises("CASE_INVALID", Suite, name="ok", build=" ")


def test_suite_expand_merges_defaults_and_preserves_overrides() -> None:
    c_default = Case(
        name="axi_basic",
        params={"A": 1},
        opts=["+C"],
        env={"E": "case"},
        tags=("axi", "fast"),
    )
    c_override = Case(
        name="axi_cov",
        build="cov",
        stage=Stage.SIGNOFF,
        timeout="2h",
        hang="15m",
        retry=3,
        seeds=[99],
        owner="alice",
        tags=("cov",),
    )
    suite = Suite(
        name="axi",
        cases=[c_default, c_override],
        tags=("nightly", "axi"),
        build="gate",
        timeout="1h",
        hang="10m",
        retry=1,
        seeds="1-3",
        params={"A": 0, "B": 2},
        opts=["+S"],
        env={"E": "suite", "S": "1"},
        stage="ready",
        owner="wp",
    )

    expanded = suite.expand()
    assert [c.name for c in expanded] == ["axi_basic", "axi_cov"]

    first, second = expanded
    assert first.tags == ("nightly", "axi", "fast")
    assert first.params == {"A": 1, "B": 2}
    assert first.opts == ["+S", "+C"]
    assert first.env == {"E": "case", "S": "1"}
    assert first.build == "gate"
    assert first.stage == "ready"
    assert first.timeout == "1h"
    assert first.hang == "10m"
    assert first.retry == 1
    assert first.seeds == "1-3"
    assert first.owner == "wp"

    assert second.tags == ("nightly", "axi", "cov")
    assert second.build == "cov"
    assert second.stage == "signoff"
    assert second.timeout == "2h"
    assert second.hang == "15m"
    assert second.retry == 3
    assert second.seeds == [99]
    assert second.owner == "alice"

    # Original Case objects remain untouched
    assert c_default.build == "default"
    assert c_default.stage == "wip"
    assert c_default.tags == ("axi", "fast")


def test_result_export_and_defaults() -> None:
    res = Result(
        job_id="smoke__1",
        case="smoke",
        seed=1,
        status="pass",
        judged_status="pass",
        raw_status="pass",
        category=None,
        reason="ok",
    )
    assert res.first_error is None
    assert res.waivers == []
    assert res.flaky is False


# ---------------------------------------------------------------- Case collection


def test_collect_cases_from_case_py_variants(project: Path) -> None:
    _write(
        project / "case.py",
        """
        from solouvm import Case, Suite

        CASES = [
            Case(name="smoke", tags=("fast",)),
            Suite(
                name="axi",
                build="gate",
                timeout="1h",
                cases=[Case(name="burst")],
            ),
        ]
        """,
    )
    cfg = _load(project)
    sources = collect_cases_with_sources(cfg)
    assert [s.case.name for s in sources] == ["smoke", "burst"]
    assert all(isinstance(s, CaseSource) for s in sources)
    assert sources[0].file == project / "case.py"
    assert sources[0].line == 5
    assert sources[1].line == 10
    assert sources[1].case.build == "gate"
    assert sources[1].case.timeout == "1h"
    assert [c.name for c in collect_cases(cfg)] == ["smoke", "burst"]


def test_collect_cases_callable_and_suites_export(project: Path) -> None:
    _write(
        project / "case.py",
        """
        from solouvm import Case, Suite

        def cases():
            return [Case(name="from_fn")]

        SUITES = [Suite(name="grp", cases=[Case(name="in_suite")], owner="bob")]
        """,
    )
    loaded = collect_cases(_load(project))
    assert [(c.name, c.owner) for c in loaded] == [("from_fn", None), ("in_suite", "bob")]


def test_collect_cases_from_case_dirs_without_case_py(project: Path) -> None:
    _write(
        project / "solouvm.conf.py",
        """
        CFG = {
            "simulator": "fake",
            "top": "tb_top",
            "builds": {"default": {}, "gate": {}},
            "case_dirs": ["cases"],
        }
        """,
    )
    _write(
        project / "cases" / "b_case.py",
        """
        from solouvm import Case
        CASE = Case(name="beta", build="gate")
        """,
    )
    _write(
        project / "cases" / "a_cases.py",
        """
        from solouvm import Case
        CASES = [Case(name="alpha")]
        """,
    )
    cfg = _load(project)
    sources = collect_cases_with_sources(cfg)
    # sorted(dir_path.glob("*.py")) -> a_cases.py before b_case.py
    assert [(s.case.name, s.file.name, s.line) for s in sources] == [
        ("alpha", "a_cases.py", 3),
        ("beta", "b_case.py", 3),
    ]
    assert case_file_for(cfg, "beta") == project / "cases" / "b_case.py"


def test_collect_cases_merges_case_py_and_case_dirs(project: Path) -> None:
    _write(
        project / "solouvm.conf.py",
        'CFG = {"simulator": "fake", "top": "tb_top", "case_dirs": ["cases"]}\n',
    )
    _write(project / "case.py", 'from solouvm import Case\nCASES = [Case(name="root_case")]\n')
    _write(project / "cases" / "sub.py", 'from solouvm import Case\nCASE = Case(name="sub_case")\n')
    cfg = _load(project)
    assert [c.name for c in collect_cases(cfg)] == ["root_case", "sub_case"]
    assert case_file_for(cfg, "root_case") == project / "case.py"
    assert case_file_for(cfg, "sub_case") == project / "cases" / "sub.py"


def test_collect_cases_allows_importing_project_modules(project: Path) -> None:
    _write(
        project / "solouvm.conf.py",
        'CFG = {"simulator": "fake", "top": "tb_top", "case_dirs": ["cases"]}\n',
    )
    _write(project / "case_helpers_t1a.py", 'SHARED_OWNER = "team-axi"\n')
    _write(
        project / "cases" / "c1.py",
        """
        from solouvm import Case
        from case_helpers_t1a import SHARED_OWNER
        CASE = Case(name="c1", owner=SHARED_OWNER)
        """,
    )
    before = list(sys.path)
    loaded = collect_cases(_load(project))
    assert loaded[0].owner == "team-axi"
    assert sys.path == before


def test_collect_cases_missing_files_raises_cfg_not_found(project: Path) -> None:
    cfg = _load(project)
    _raises("CFG_NOT_FOUND", collect_cases, cfg)

    _write(
        project / "solouvm.conf.py",
        'CFG = {"simulator": "fake", "top": "tb_top", "case_dirs": ["cases"]}\n',
    )
    (project / "cases").mkdir()
    _raises("CFG_NOT_FOUND", collect_cases, _load(project))


def test_collect_cases_load_errors_and_line_numbers(project: Path) -> None:
    cfg = _load(project)

    _write(project / "case.py", "x = 1\nraise RuntimeError('kaboom')\n")
    err = _raises("CFG_LOAD_FAILED", collect_cases, cfg)
    assert "kaboom" in err.message
    assert err.file == str(project / "case.py")
    assert err.line == 2

    _write(project / "case.py", "CASES = [\n")
    err = _raises("CFG_LOAD_FAILED", collect_cases, cfg)
    assert err.file == str(project / "case.py")
    assert err.line == 1

    _write(project / "case.py", "OTHER = 1\n")
    err = _raises("CFG_LOAD_FAILED", collect_cases, cfg)
    assert err.file == str(project / "case.py")


def test_collect_cases_validates_duplicates_and_builds(project: Path) -> None:
    _write(
        project / "solouvm.conf.py",
        'CFG = {"simulator": "fake", "top": "tb_top", "case_dirs": ["cases"]}\n',
    )
    _write(
        project / "case.py",
        """
        from solouvm import Case
        CASES = [Case(name="dup")]
        """,
    )
    _write(
        project / "cases" / "dup.py",
        """
        from solouvm import Case
        CASE = Case(name="dup")
        """,
    )
    cfg = _load(project)
    err = _raises("CASE_INVALID", collect_cases, cfg)
    assert "dup" in err.message
    assert err.file == str(project / "cases" / "dup.py")
    assert err.line == 3

    (project / "cases" / "dup.py").unlink()
    _write(
        project / "case.py",
        """
        from solouvm import Case
        CASES = [
            Case(name="ok"),
            Case(name="bad_build", build="missing_mode"),
        ]
        """,
    )
    err = _raises("CASE_INVALID", collect_cases, cfg)
    assert "missing_mode" in err.message
    assert err.file == str(project / "case.py")
    assert err.line == 5


def test_collect_cases_preserves_case_and_record_errors_with_location(project: Path) -> None:
    cfg = _load(project)
    _write(
        project / "case.py",
        """
        from solouvm import Case
        CASES = [Case(name="bad__name")]
        """,
    )
    err = _raises("CASE_INVALID", collect_cases, cfg)
    assert err.file == str(project / "case.py")
    assert err.line == 3

    _write(
        project / "case.py",
        """
        from solouvm import Bug, Case
        CASES = [
            Case(
                name="b",
                notes=[
                    Bug(id=1, title="a", found="2026-11-01"),
                    Bug(id=1, title="b", found="2026-11-02"),
                ],
            )
        ]
        """,
    )
    err = _raises("RECORD_INVALID", collect_cases, cfg)
    assert err.file == str(project / "case.py")


def test_find_case_line_and_case_file_for_missing(project: Path) -> None:
    src = textwrap.dedent(
        """
        import solouvm
        from solouvm import Case
        CASES = [
            Case("pos_case"),
            solouvm.Case(name="attr_case"),
        ]
        """
    )
    assert find_case_line(src, "pos_case") == 5
    assert find_case_line(src, "attr_case") == 6
    assert find_case_line(src, "nope") is None
    assert find_case_line("def broken(:", "pos_case") is None

    _write(project / "case.py", 'from solouvm import Case\nCASES = [Case(name="burst")]\n')
    err = _raises("CASE_INVALID", case_file_for, _load(project), "burts")
    assert err.hint and "burst" in err.hint
    assert Bug.__name__ == "Bug"
