"""Tests for ``solouvm.ui``, ``solouvm.output``, ``solouvm.roles``, and ``solouvm.__main__``."""

from __future__ import annotations

import importlib
import io
import json
import sys
import types
from pathlib import Path

import pytest

from solouvm import output, roles, ui
from solouvm.errors import BuildError, ConfigError, RoleError, SolouvmError


@pytest.fixture(autouse=True)
def _reset_ui_state():
    ui.reset()
    yield
    ui.reset()


class _TtyBuffer(io.StringIO):
    def isatty(self) -> bool:
        return True


# ---------- ui.py: vendor loading & fallback ----------


def test_vendor_rich_loaded_by_default():
    assert ui.Console is not None
    assert ui.Table is not None
    assert str(Path(ui.__file__).parent / "_vendor") in sys.path
    assert not (Path(ui.__file__).parent / "_vendor" / "__init__.py").exists()


def test_missing_vendor_dir_falls_back_cleanly(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ui, "_VENDOR", tmp_path / "missing_vendor")
    try:
        ui.reload_vendor()
        assert ui.Console is None
        assert ui.Table is None
        assert ui.use_rich() is False

        ui.setup(json_mode=False, quiet=False)
        ui.info("plain info")
        shown, omitted = ui.table(["COL"], [["val"]])
        assert shown == [["val"]]
        assert omitted == 0

        out = capsys.readouterr().out
        assert "plain info" in out
        assert "COL" in out
        assert "val" in out
        assert "\x1b[" not in out
    finally:
        monkeypatch.undo()
        ui.reload_vendor()

    assert ui.Console is not None
    assert ui.Table is not None


def test_import_error_during_module_reload_falls_back_cleanly(monkeypatch):
    monkeypatch.setitem(sys.modules, "rich.console", None)
    try:
        reloaded = importlib.reload(ui)
        assert reloaded.Console is None
        assert reloaded.Table is None
        assert reloaded.use_rich() is False
    finally:
        monkeypatch.undo()
        importlib.reload(ui)

    assert ui.Console is not None
    assert ui.Table is not None


# ---------- ui.py: state & modes ----------


def test_setup_and_accessors(tmp_path):
    log_path = tmp_path / ".solouvm" / "solouvm.log"
    ui.setup(json_mode=True, quiet=True, verbose=2, log_file=log_path, limit=25)
    assert ui.is_json_mode() is True
    assert ui.is_quiet() is True
    assert ui.verbosity() == 2
    assert ui.get_limit() == 25
    assert ui.get_log_file() == log_path

    other = tmp_path / "other.log"
    ui.set_log_file(other)
    assert ui.get_log_file() == other

    ui.reset()
    assert ui.is_json_mode() is False
    assert ui.is_quiet() is False
    assert ui.verbosity() == 0
    assert ui.get_limit() == 50
    assert ui.get_log_file() is None


def test_setup_rejects_negative_values():
    with pytest.raises(ValueError, match="limit"):
        ui.setup(limit=-1)
    with pytest.raises(ValueError, match="verbose"):
        ui.setup(verbose=-1)


def test_non_tty_has_no_ansi_escapes(capsys):
    assert ui.is_tty() is False
    assert ui.use_rich() is False

    ui.setup(json_mode=False, quiet=False)
    ui.info("hello [red]world[/red]")
    ui.status_line("1/2 done")
    ui.summary("2 jobs from 2 cases")
    ui.table(["JOB", "CASE"], [["burst__1", "burst"], ["smoke__2", "smoke"]])

    captured = capsys.readouterr()
    assert "\x1b[" not in captured.out
    assert "\x1b[" not in captured.err
    assert "hello [red]world[/red]" in captured.out
    assert "1/2 done" in captured.out
    assert "2 jobs from 2 cases" in captured.out
    assert "JOB       CASE" in captured.out


def test_tty_uses_rich_when_not_json(monkeypatch):
    buf = _TtyBuffer()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setenv("TERM", "xterm-256color")

    ui.setup(json_mode=False, quiet=False)
    assert ui.is_tty() is True
    assert ui.use_rich() is True

    ui.info("UVM_ERROR [AXI_SCB] [b]literal")
    ui.table(["JOB", "STAGE"], [["burst__1", "wip"]])
    out = buf.getvalue()
    assert "UVM_ERROR [AXI_SCB] [b]literal" in out
    assert "burst__1" in out
    assert "┏" in out  # rendered via Rich Table box drawing


def test_json_mode_suppresses_human_stdout_and_disables_rich(monkeypatch, capsys):
    buf = _TtyBuffer()
    monkeypatch.setattr(sys, "stdout", buf)
    ui.setup(json_mode=True)
    assert ui.use_rich() is False
    ui.info("should not appear")
    ui.status_line("should not appear")
    ui.summary("should not appear")
    shown, omitted = ui.table(["A"], [["1"]])
    assert shown == [["1"]]
    assert omitted == 0
    assert buf.getvalue() == ""

    # Undo stdout monkeypatch before testing capsys stderr capture
    monkeypatch.undo()
    ui.setup(json_mode=True)
    ui.info("x")
    ui.warning("careful")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "warning: careful\n"


def test_quiet_mode_suppresses_info_status_and_table_but_keeps_summary_and_errors(capsys):
    ui.setup(quiet=True)
    ui.info("hidden info")
    ui.status_line("hidden status")
    ui.table(["A"], [["1"]])
    ui.summary("final summary")
    ui.warning("deprecated case")
    ui.error("bad thing")

    captured = capsys.readouterr()
    assert captured.out == "final summary\n"
    assert "warning: deprecated case\n" in captured.err
    assert "solouvm: error: bad thing\n" in captured.err


def test_error_does_not_double_prefix(capsys):
    ui.error("solouvm: error: already formatted")
    ui.error("error: short prefix")
    err = capsys.readouterr().err.splitlines()
    assert err == [
        "solouvm: error: already formatted",
        "error: short prefix",
    ]


# ---------- ui.py: -vv command tracing (Z-07) ----------


def test_command_log_only_traces_when_verbose_at_least_two(tmp_path, capsys):
    log_file = tmp_path / ".solouvm" / "solouvm.log"
    ui.setup(verbose=1, log_file=log_file)
    ui.command_log(["echo", "ignored"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert not log_file.exists()

    ui.setup(verbose=2, log_file=log_file, json_mode=True)
    ui.command_log(["ssh", "simbox01", "make build"])
    ui.command_log("rsync -a src dst")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ssh simbox01 'make build'" in captured.err
    assert "rsync -a src dst" in captured.err

    lines = log_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "ssh simbox01 'make build'" in lines[0]
    assert "rsync -a src dst" in lines[1]
    # ISO 8601 timestamp prefix with timezone offset
    assert "T" in lines[0].split(" ", 1)[0]


# ---------- ui.py: table & format_table ----------


def test_format_table_and_table_truncation(capsys):
    assert ui.format_table([], []) == ""
    assert ui.format_table(["JOB", "CASE"], []) == "JOB  CASE"

    rows = [[f"job_{i}", f"case_{i}"] for i in range(10)]
    ui.setup(limit=5)
    shown, omitted = ui.table(["JOB", "CASE"], rows)
    assert len(shown) == 5
    assert omitted == 5
    out = capsys.readouterr().out
    assert "job_0" in out
    assert "job_4" in out
    assert "job_5" not in out

    shown_all, omitted_none = ui.table(["JOB", "CASE"], rows, limit=0)
    assert len(shown_all) == 10
    assert omitted_none == 0
    assert "job_9" in capsys.readouterr().out


# ---------- output.py ----------


def test_truncate_items():
    items = list(range(10))
    kept, trunc = output.truncate_items(items, 5, more="solouvm list --limit 0")
    assert kept == [0, 1, 2, 3, 4]
    assert trunc == {"omitted": 5, "more": "solouvm list --limit 0"}

    kept_all, trunc_none = output.truncate_items(items, 0, more="solouvm list --limit 0")
    assert kept_all == items
    assert trunc_none is None

    kept_exact, trunc_exact = output.truncate_items(items, 10, more="x")
    assert kept_exact == items
    assert trunc_exact is None

    with pytest.raises(ValueError, match="limit"):
        output.truncate_items(items, -1, more="x")


def test_emit_json_key_order_and_utf8(capsys):
    output.emit_json(
        "list",
        {"msg": "中文测试", "jobs": []},
        truncated={"omitted": 5, "more": "solouvm list --limit 0"},
        next_=["solouvm gen --tag nightly"],
    )
    raw = capsys.readouterr().out
    assert raw.endswith("\n")
    assert "中文测试" in raw  # ensure_ascii=False

    parsed = json.loads(raw)
    assert list(parsed.keys()) == ["schema", "command", "ok", "data", "truncated", "next"]
    assert parsed == {
        "schema": 1,
        "command": "list",
        "ok": True,
        "data": {"msg": "中文测试", "jobs": []},
        "truncated": {"omitted": 5, "more": "solouvm list --limit 0"},
        "next": ["solouvm gen --tag nightly"],
    }


def test_emit_event_jsonl(capsys):
    output.emit_event("job_done", job_id="burst__4417", status="pass", msg="成功")
    out = capsys.readouterr().out
    lines = out.splitlines()
    assert len(lines) == 1
    assert "成功" in lines[0]
    parsed = json.loads(lines[0])
    assert list(parsed.keys()) == ["schema", "event", "job_id", "status", "msg"]
    assert parsed["schema"] == 1
    assert parsed["event"] == "job_done"

    with pytest.raises(ValueError, match="reserved"):
        output.emit_event("job_done", schema=2)


def test_emit_next_modes(capsys):
    ui.setup(json_mode=False, quiet=False)
    output.emit_next(["solouvm gen --tag nightly", "solouvm validate"])
    assert capsys.readouterr().out == ("next: solouvm gen --tag nightly\nnext: solouvm validate\n")

    ui.setup(quiet=True)
    output.emit_next(["solouvm gen"])
    assert capsys.readouterr().out == ""

    ui.setup(json_mode=True)
    output.emit_next(["solouvm gen"])
    assert capsys.readouterr().out == ""


def test_fail_json_and_human_modes(capsys):
    err = ConfigError(
        "unknown key 'regres' in CFG",
        code="CFG_UNKNOWN_KEY",
        file="solouvm.conf.py",
        line=12,
        hint="did you mean 'regress'?",
    )

    ui.setup(json_mode=True)
    code = output.fail(err, command="gen")
    assert code == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    parsed = json.loads(captured.out)
    assert list(parsed.keys()) == ["schema", "command", "ok", "error"]
    assert parsed["schema"] == 1
    assert parsed["command"] == "gen"
    assert parsed["ok"] is False
    assert list(parsed["error"].keys()) == ["code", "message", "file", "line", "hint"]
    assert parsed["error"] == err.to_dict()

    ui.setup(json_mode=False)
    code = output.fail(err, command="gen")
    assert code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "solouvm: error: solouvm.conf.py:12: unknown key 'regres' in CFG\n"
        "hint: did you mean 'regress'?\n"
    )


def test_fail_exit_codes():
    ui.setup(json_mode=False)
    assert output.fail(SolouvmError("failed", code="TESTS_FAILED")) == 1
    assert output.fail(SolouvmError("stopped", code="INTERRUPTED")) == 130
    assert output.fail(BuildError("compile failed")) == 2


# ---------- roles.py ----------


@pytest.mark.parametrize(
    "command", sorted(roles.TRIAGE_READONLY_COMMANDS | roles.TRIAGE_WRITE_COMMANDS)
)
def test_triage_allowed_commands(command):
    assert command in roles.ROLE_ALLOWED["triage"]
    assert command in roles.ROLE_ALLOWED["main"]
    roles.check_role("triage", command)
    roles.check_role("main", command)


@pytest.mark.parametrize(
    "command",
    [
        "gen",
        "build",
        "run",
        "regress",
        "rerun",
        "stop",
        "clean",
        "judge",
        "report",
        "check",
        "init",
        "doctor",
        "new",
        "uvm sync",
        "triage plan",
        "triage review",
        "todo done",
        "doc",
        "cov merge",
        "cov report",
        "bisect",
    ],
)
def test_triage_denied_commands(command):
    assert command not in roles.ROLE_ALLOWED["triage"]
    roles.check_role("main", command)
    with pytest.raises(RoleError) as exc_info:
        roles.check_role("triage", command)
    assert exc_info.value.code == "ROLE_DENIED"
    assert exc_info.value.exit_code == 2
    assert exc_info.value.hint


def test_unknown_role_rejected():
    with pytest.raises(RoleError) as exc_info:
        roles.check_role("reviewer", "list")
    assert exc_info.value.code == "ROLE_DENIED"
    assert exc_info.value.exit_code == 2


def test_main_role_allows_custom_or_future_commands():
    roles.check_role("main", "future-subcommand")


# ---------- __main__.py ----------


def test_dunder_main_imports_without_cli_and_delegates(monkeypatch):
    import solouvm.__main__ as entry

    fake_cli = types.ModuleType("solouvm.cli")
    seen: list[list[str] | None] = []

    def fake_cli_main(argv=None):
        seen.append(argv)
        return 7

    fake_cli.main = fake_cli_main
    monkeypatch.setitem(sys.modules, "solouvm.cli", fake_cli)
    assert entry.main(["--version"]) == 7
    assert seen == [["--version"]]
