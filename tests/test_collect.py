"""Tests for solouvm.collect (task 11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from solouvm.collect import FileEntry, collect_files, load_filelist, parse_dotf
from solouvm.errors import ConfigError


def touch(root: Path, *names: str) -> None:
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// x\n", encoding="utf-8")


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def raises(code: str, func, *args, **kwargs) -> ConfigError:
    with pytest.raises(ConfigError) as info:
        func(*args, **kwargs)
    assert info.value.code == code, str(info.value)
    return info.value


@pytest.fixture
def root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    return root


def test_three_groups_in_order(root: Path) -> None:
    touch(root, "rtl/a.sv", "verif/pkg.sv", "verif/tb/tb_top.sv")
    fl = collect_files(
        root, {"top": ["verif/tb/tb_top.sv"], "tb": ["verif/pkg.sv"], "rtl": ["rtl/a.sv"]}
    )
    assert list(fl.groups) == ["rtl", "tb", "top"]
    assert fl.groups == {
        "rtl": ["rtl/a.sv"],
        "tb": ["verif/pkg.sv"],
        "top": ["verif/tb/tb_top.sv"],
    }
    assert fl.files == ["rtl/a.sv", "verif/pkg.sv", "verif/tb/tb_top.sv"]


def test_omitted_groups_are_empty(root: Path) -> None:
    touch(root, "verif/pkg.sv")
    fl = collect_files(root, {"tb": ["verif/pkg.sv"]})
    assert fl.groups == {"rtl": [], "tb": ["verif/pkg.sv"], "top": []}


def test_recursive_glob_sorted_by_full_path(root: Path) -> None:
    touch(root, "rtl/z/same.sv", "rtl/a/same.sv", "rtl/top.sv", "rtl/a/b.v")
    fl = collect_files(root, {"rtl": ["rtl/**/*.sv"]})
    assert fl.groups["rtl"] == ["rtl/a/same.sv", "rtl/top.sv", "rtl/z/same.sv"]


def test_entry_order_kept_and_duplicates_dropped(root: Path) -> None:
    touch(root, "rtl/pkg.sv", "rtl/a.sv", "rtl/b.sv")
    fl = collect_files(root, {"rtl": ["rtl/pkg.sv", "rtl/*.sv", "rtl/a.sv"]})
    assert fl.groups["rtl"] == ["rtl/pkg.sv", "rtl/a.sv", "rtl/b.sv"]
    assert [e.source for e in fl.entries] == ["rtl/pkg.sv", "rtl/*.sv", "rtl/*.sv"]


def test_same_file_twice(root: Path) -> None:
    touch(root, "rtl/a.sv")
    fl = collect_files(root, {"rtl": ["rtl/a.sv", "rtl/a.sv"], "tb": ["rtl/a.sv"]})
    assert fl.groups["rtl"] == ["rtl/a.sv"]
    assert fl.groups["tb"] == []
    assert len(fl.entries) == 1


def test_missing_entries_listed_together(root: Path) -> None:
    touch(root, "rtl/ok.sv")
    err = raises(
        "FILES_MISSING",
        collect_files,
        root,
        {"rtl": ["nope.sv", "rtl/ok.sv", "also_nope.sv"], "tb": ["verif/*.sv"]},
    )
    lines = err.message.splitlines()
    assert "nope.sv" in err.message
    assert "also_nope.sv" in err.message
    assert "tb: verif/*.sv" in err.message
    assert len(lines) == 4  # 标题 + 三条缺失


def test_nested_dotf(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(root)
    touch(root, "ip/x.sv", "ip/y.sv")
    write(root / "a.f", "ip/x.sv\n-f b.f\n")
    write(root / "b.f", "// comment\nip/y.sv  // trailing\n")
    fl = collect_files(root, {"rtl": ["-f a.f"]})
    assert fl.groups["rtl"] == ["ip/x.sv", "ip/y.sv"]
    assert [e.source for e in fl.entries] == ["a.f", "b.f"]


def test_dash_f_relative_to_cwd(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # .f 在子目录里，内容相对 cwd（= 项目根）
    touch(root, "rtl/a.sv")
    write(root / "ip/list.f", "rtl/a.sv\n")
    monkeypatch.chdir(root)
    fl = collect_files(root, {"rtl": ["-f ip/list.f"]})
    assert fl.groups["rtl"] == ["rtl/a.sv"]
    # 换一个 cwd，同一个 .f 就找不到了
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)
    raises("FILES_MISSING", collect_files, root, {"rtl": ["-f ip/list.f"]})


def test_dash_capital_f_relative_to_dotf(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    touch(root, "ip/vendor/src/core.sv")
    write(root / "ip/vendor/vendor.f", "src/core.sv\n+incdir+inc\n")
    monkeypatch.chdir(tmp_path)
    fl = collect_files(root, {"rtl": ["-F ip/vendor/vendor.f"]})
    assert fl.groups["rtl"] == ["ip/vendor/src/core.sv"]
    assert fl.incdirs["rtl"] == ["ip/vendor/inc"]
    assert fl.entries[0].source == "ip/vendor/vendor.f"


def test_incdir_and_define(root: Path) -> None:
    touch(root, "verif/pkg.sv")
    fl = collect_files(root, {"tb": ["+incdir+verif", "+define+SIM=1", "verif/pkg.sv"]})
    assert fl.incdirs["tb"] == ["verif"]
    assert fl.defines["tb"] == ["SIM=1"]
    assert fl.groups["tb"] == ["verif/pkg.sv"]
    assert fl.incdirs["rtl"] == []


def test_library_options(root: Path) -> None:
    touch(root, "lib.v", "rtl/a.sv")
    (root / "libdir").mkdir()
    fl = collect_files(root, {"rtl": ["-v lib.v", "-y libdir", "+libext+.v+.sv", "rtl/a.sv"]})
    assert fl.groups["rtl"] == ["rtl/a.sv"]
    assert fl.libs["rtl"] == ["-v lib.v", "-y libdir", "+libext+.v+.sv"]


def test_library_options_in_dotf(root: Path) -> None:
    touch(root, "ip/a.sv")
    write(root / "ip/ip.f", "-v cells.v -y lib\n+libext+.v\n+define+A+B=2\na.sv\n")
    fl = collect_files(root, {"rtl": ["-F ip/ip.f"]})
    assert fl.groups["rtl"] == ["ip/a.sv"]
    assert fl.libs["rtl"] == ["-v ip/cells.v", "-y ip/lib", "+libext+.v"]
    assert fl.defines["rtl"] == ["A", "B=2"]


def test_absolute_path_inside_root(root: Path) -> None:
    touch(root, "rtl/a.sv")
    fl = collect_files(root, {"rtl": [str(root / "rtl" / "a.sv")]})
    assert fl.groups["rtl"] == ["rtl/a.sv"]
    assert fl.entries[0].outside_root is False


def test_absolute_path_outside_root(root: Path) -> None:
    fl = collect_files(root, {"rtl": ["/opt/ip/x.sv"]})
    assert fl.groups["rtl"] == ["/opt/ip/x.sv"]
    assert fl.entries == [FileEntry("/opt/ip/x.sv", "rtl", "/opt/ip/x.sv", outside_root=True)]
    assert fl.outside_root == fl.entries


def test_absolute_paths_in_dotf(root: Path, tmp_path: Path) -> None:
    touch(root, "rtl/a.sv")
    write(root / "ip.f", f"{root / 'rtl' / 'a.sv'}\n/opt/vendor/v.sv\n")
    fl = collect_files(root, {"rtl": ["-F ip.f"]})
    assert fl.groups["rtl"] == ["rtl/a.sv", "/opt/vendor/v.sv"]
    assert [e.outside_root for e in fl.entries] == [False, True]


def test_env_vars_kept_verbatim(root: Path) -> None:
    write(root / "ip.f", "${IP_HOME}/x.sv\n+incdir+$IP_HOME/inc\n")
    fl = collect_files(root, {"rtl": ["$VENDOR/a.sv", "-F ip.f"], "tb": ["+incdir+${UVM}/src"]})
    assert fl.groups["rtl"] == ["$VENDOR/a.sv", "${IP_HOME}/x.sv"]
    assert fl.incdirs["rtl"] == ["$IP_HOME/inc"]
    assert fl.incdirs["tb"] == ["${UVM}/src"]


def test_list_form_goes_to_rtl(root: Path) -> None:
    touch(root, "rtl/a.sv")
    fl = collect_files(root, ["rtl/a.sv"])
    assert fl.groups == {"rtl": ["rtl/a.sv"], "tb": [], "top": []}


def test_bad_group_name(root: Path) -> None:
    err = raises("CFG_INVALID", collect_files, root, {"bogus": ["a.sv"]})
    assert "rtl" in err.message and "tb" in err.message and "top" in err.message


@pytest.mark.parametrize("spec", ["rtl/a.sv", 3, None])
def test_bad_spec_type(root: Path, spec: object) -> None:
    raises("CFG_INVALID", collect_files, root, spec)


def test_bad_entry_type(root: Path) -> None:
    raises("CFG_INVALID", collect_files, root, {"rtl": [3]})
    raises("CFG_INVALID", collect_files, root, {"rtl": "rtl/a.sv"})


def test_sources_point_back(root: Path) -> None:
    touch(root, "rtl/x/a.sv", "rtl/b.sv", "ip/v.sv")
    write(root / "ip/vendor_ip.f", "v.sv\n")
    fl = collect_files(root, {"rtl": ["rtl/**/*.sv", "-F ip/vendor_ip.f"]})
    by_path = {e.path: e.source for e in fl.entries}
    assert by_path == {
        "rtl/b.sv": "rtl/**/*.sv",
        "rtl/x/a.sv": "rtl/**/*.sv",
        "ip/v.sv": "ip/vendor_ip.f",
    }
    assert all(e.group == "rtl" for e in fl.entries)


def test_dotf_cycle_reports_error(root: Path) -> None:
    write(root / "a.f", "-F b.f\n")
    write(root / "b.f", "-F a.f\n")
    err = raises("CFG_INVALID", collect_files, root, {"rtl": ["-F a.f"]})
    assert "a.f" in err.message


def test_dotf_depth_limit(root: Path) -> None:
    for i in range(40):
        write(root / f"d{i}.f", f"-F d{i + 1}.f\n")
    write(root / "d40.f", "")
    err = raises("CFG_INVALID", collect_files, root, {"rtl": ["-F d0.f"]})
    assert "32" in err.message
    assert ".f" in err.message


def test_missing_dotf(root: Path) -> None:
    err = raises("FILES_MISSING", collect_files, root, {"rtl": ["-f nope.f", "x.sv"]})
    assert "nope.f" in err.message and "x.sv" in err.message
    raises("FILES_MISSING", parse_dotf, root / "nope.f", relative_to_dotf=True)


def test_parse_dotf_tokens(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(root)
    write(root / "sub/a.f", '"with space.sv" b.sv\n-F inner.f\n+incdir+inc\n-v lib.v\n')
    write(root / "sub/inner.f", "c.sv\n")
    assert parse_dotf(root / "sub/a.f", relative_to_dotf=True) == [
        str(root / "sub/with space.sv"),
        str(root / "sub/b.sv"),
        str(root / "sub/c.sv"),
        "+incdir+" + str(root / "sub/inc"),
        "-v",
        str(root / "sub/lib.v"),
    ]
    assert parse_dotf(root / "sub/inner.f", relative_to_dotf=False) == [str(root / "c.sv")]


def test_unsupported_dotf_option(root: Path) -> None:
    write(root / "a.f", "-sverilog\n")
    raises("CFG_INVALID", parse_dotf, root / "a.f", relative_to_dotf=True)


def test_load_filelist(root: Path) -> None:
    raises("FILES_MISSING", load_filelist, root)
    write(root / "filelist.py", 'FILES = {"rtl": ["a.sv"]}\n')
    assert load_filelist(root) == {"rtl": ["a.sv"]}
    write(root / "filelist.py", 'def files():\n    return ["b.sv"]\n')
    assert load_filelist(root) == ["b.sv"]
    write(root / "filelist.py", "X = 1\n")
    raises("CFG_LOAD_FAILED", load_filelist, root)
    write(root / "filelist.py", "raise ValueError('kaput')\n")
    err = raises("CFG_LOAD_FAILED", load_filelist, root)
    assert "kaput" in err.message
