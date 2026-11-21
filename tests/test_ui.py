"""Tests for ``solouvm.ui``, ``solouvm.output``, ``solouvm.roles``, and ``solouvm.__main__``."""

from __future__ import annotations

import importlib
import io
import json
import re
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


def test_live_runner_tty_and_non_tty_modes(monkeypatch, capsys):
    # 1. Non-TTY mode: plain stdout, no ANSI, no live footer
    ui.setup(json_mode=False, quiet=False)
    with ui.live_runner("building default") as live:
        assert live.is_live is False
        live.log("[AXI_MON] plain line")
        live.update("building default (1 lines)")
    out = capsys.readouterr().out
    assert "[AXI_MON] plain line" in out
    assert "\x1b[" not in out

    # 2. --json and --quiet suppress live logs and Rich live footer
    for kwargs in ({"json_mode": True}, {"quiet": True}):
        buf = _TtyBuffer()
        monkeypatch.setattr(sys, "stdout", buf)
        ui.setup(**kwargs)
        with ui.live_runner("building") as live:
            assert live.is_live is False
            live.log("should stay silent")
        assert buf.getvalue() == ""

    # 3. TTY mode activates Rich Live with transient footer & literal brackets
    buf = _TtyBuffer()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "160")
    ui.setup(json_mode=False, quiet=False)
    with ui.live_runner("building default (0 lines)") as live:
        assert live.is_live is True
        live.log("[AXI_MON] compiling a.sv")
        ui.info("via active live [uvm_pkg]")
        live.update("building default (1 lines)")
    assert live.is_live is False
    assert ui._state.active_live is None
    tty_out = buf.getvalue()
    assert "[AXI_MON] compiling a.sv" in tty_out
    assert "via active live [uvm_pkg]" in tty_out


def test_live_runner_collapsible_stages_and_verbose_mode(monkeypatch):
    ansi_re = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

    # 1. Default TTY (verbose=0): collapsible viewport hides on success, keeps tail on failure
    buf = _TtyBuffer()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setenv("TERM", "xterm-256color")
    ui.setup(json_mode=False, quiet=False, verbose=0)
    with ui.live_runner("run r1") as live:
        assert live.is_live is True
        ui.stage_start("gen", "planning 1 job (run.mk, filelist)", index=1, total=5)
        assert live.is_collapsible_stage is True
        h_empty = len(live._render_footer().renderables)
        assert live._render_footer().renderables[-1].no_wrap is True
        ui.hook_notice("pre", "gen", "hooks/pre_hook_gen.py", status="start")
        live.log_batch(["line_one", "line_two"])
        assert [entry[2] for entry in live._stage_lines][-2:] == ["line_one", "line_two"]
        assert len(live._render_footer().renderables) == h_empty
        live.log_batch([f"extra_{i}" for i in range(20)])
        assert len(live._render_footer().renderables) == h_empty
        ui.stage_done(
            "gen",
            "planned 1 job (run.mk, filelist)",
            index=1,
            total=5,
            ok=True,
            duration_s=0.05,
        )
        assert live.is_collapsible_stage is False
        assert len(live._render_footer().renderables) == h_empty

        ui.stage_start("build", "compiling mode=default", index=3, total=5)
        live.log_batch(["FAIL_LINE_A", "FAIL_LINE_B"])
        ui.stage_done(
            "build",
            "failed mode=default (rc=2)",
            index=3,
            total=5,
            ok=False,
            duration_s=0.2,
        )
        assert live.is_collapsible_stage is False

    plain = ansi_re.sub("", buf.getvalue())
    gen_line = f"✓ [1/5] Generate  {'planned 1 job (run.mk, filelist)':<34}    0.05s"
    fail_line = f"✗ [3/5] Compile   {'failed mode=default (rc=2)':<34}    0.20s"
    assert gen_line in plain
    assert "▾ [3/5] Compile   compiling mode=default" in plain
    assert "FAIL_LINE_A" in plain
    assert "FAIL_LINE_B" in plain
    assert fail_line in plain
    assert gen_line.index("0.05s") == fail_line.index("0.20s")

    # 2. TTY with verbose=1 (-v): stages stay permanently expanded (is_collapsible_stage=False)
    buf_v = _TtyBuffer()
    monkeypatch.setattr(sys, "stdout", buf_v)
    ui.setup(json_mode=False, quiet=False, verbose=1)
    with ui.live_runner("run r2") as live_v:
        ui.stage_start("gen", "planning 1 job (run.mk, filelist)", index=1, total=5)
        assert live_v.is_collapsible_stage is False
        live_v.log_batch(["VERBOSE_LINE_1"])
        ui.stage_done(
            "gen",
            "planned 1 job (run.mk, filelist)",
            index=1,
            total=5,
            ok=True,
            duration_s=0.02,
        )

    plain_v = ansi_re.sub("", buf_v.getvalue())
    assert "==> [1/5] Generate  planning 1 job (run.mk, filelist)" in plain_v
    assert "VERBOSE_LINE_1" in plain_v
    assert f"✓ [1/5] Generate  {'planned 1 job (run.mk, filelist)':<34}    0.02s" in plain_v


def test_ui_echo_persists_above_collapsible_stage_and_respects_json_quiet(monkeypatch):
    ansi_re = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
    buf = _TtyBuffer()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setenv("TERM", "xterm-256color")
    ui.setup(json_mode=False, quiet=False, verbose=0)
    with ui.live_runner("run r1") as live:
        ui.stage_start("gen", "planning 1 job (run.mk, filelist)", index=1, total=5)
        live.log("EPHEMERAL_MSG")
        ui.echo("PERSIST_MSG [OK]", source="hooks/post_hook_judge.py")
        ui.echo("AUTO_SRC_MSG")
        assert "EPHEMERAL_MSG" in [entry[2] for entry in live._stage_lines]
        assert "PERSIST_MSG [OK]" not in [entry[2] for entry in live._stage_lines]
        assert "AUTO_SRC_MSG" not in [entry[2] for entry in live._stage_lines]
        ui.stage_done(
            "gen",
            "planned 1 job (run.mk, filelist)",
            index=1,
            total=5,
            ok=True,
            duration_s=0.03,
        )
        assert not live._stage_lines

    plain = ansi_re.sub("", buf.getvalue())
    assert "  [hooks/post_hook_judge.py] PERSIST_MSG [OK]" in plain
    assert "test_ui.py] AUTO_SRC_MSG" in plain
    assert f"✓ [1/5] Generate  {'planned 1 job (run.mk, filelist)':<34}    0.03s" in plain

    for kwargs in ({"json_mode": True}, {"quiet": True}):
        silent = _TtyBuffer()
        monkeypatch.setattr(sys, "stdout", silent)
        ui.setup(**kwargs)
        ui.echo("SHOULD_NOT_EMIT")
        assert silent.getvalue() == ""


def test_print_run_summary_rich_tables_and_non_tty_fallback(monkeypatch, capsys):
    ansi_re = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
    payload = {
        "schema": 1,
        "run_id": "r_tbl",
        "partial": False,
        "summary": {
            "total": 2,
            "passed": 1,
            "duration_s": 2.5,
            "by_status": {"pass": 1, "xfail": 0, "fail": 1},
            "by_category": {"design": 1, "execution": 0, "environment": 0},
        },
        "jobs": [
            {
                "job_id": "smoke__1",
                "case": "smoke",
                "seed": 1,
                "status": "pass",
                "weak_signals": ["no UVM messages"],
                "attempts": [{"try": 1, "duration_s": 1.25, "wave": False}],
            },
            {
                "job_id": "burst__2",
                "case": "burst",
                "seed": 2,
                "status": "fail",
                "category": "design",
                "reason": "UVM_ERROR=1",
                "attempts": [
                    {
                        "try": 1,
                        "duration_s": 1.25,
                        "wave": False,
                        "log": ".solouvm/runs/r_tbl/burst__2/try1/sim.log",
                    }
                ],
            },
        ],
        "signatures": [
            {"id": "sig1", "count": 1, "cases": ["burst"], "text": "scoreboard mismatch"}
        ],
    }
    compare = {
        "baseline_run_id": "r_base",
        "new_failures": [
            {"job_id": "burst__2", "prev_status": "pass", "status": "fail", "reason": "UVM_ERROR=1"}
        ],
    }

    # 1. TTY mode: one Rich table rendered, zero-count omitted, weak & failure details shown
    buf = _TtyBuffer()
    monkeypatch.setattr(sys, "stdout", buf)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLUMNS", "160")
    ui.setup(json_mode=False, quiet=False)
    ui.print_run_summary(payload, compare=compare, fallback_text="FALLBACK_TEXT")
    out = buf.getvalue()
    plain = ansi_re.sub("", out)
    assert plain.count("┏") == 1
    assert "FALLBACK_TEXT" not in plain
    assert "Run r_tbl" in plain
    assert "Total (1/2)" in plain
    assert "50.0%" in plain
    assert "2.500s" in plain
    assert "pass=1" in plain and "fail=1" in plain
    assert "xfail=0" not in plain
    assert "burst__2" in plain and "smoke__1" in plain
    assert "weak: no UVM messages" in plain
    assert "sig1" in plain
    assert "Compare vs r_base" in plain

    # Empty jobs payload still renders cleanly in a single table
    buf_empty = _TtyBuffer()
    monkeypatch.setattr(sys, "stdout", buf_empty)
    ui.print_run_summary(
        {
            "schema": 1,
            "run_id": "r_empty",
            "partial": False,
            "summary": {
                "total": 0,
                "passed": 0,
                "duration_s": 0.0,
                "by_status": {},
                "by_category": {},
            },
            "jobs": [],
        },
        fallback_text="FALLBACK_TEXT",
    )
    plain_empty = ansi_re.sub("", buf_empty.getvalue())
    assert plain_empty.count("┏") == 1
    assert "Total (0/0)" in plain_empty
    assert "(no jobs)" in plain_empty

    # 2. --quiet on TTY still renders summary tables; --json stays silent
    buf_q = _TtyBuffer()
    monkeypatch.setattr(sys, "stdout", buf_q)
    ui.setup(quiet=True)
    ui.print_run_summary(payload, fallback_text="FALLBACK_TEXT")
    assert "r_tbl" in buf_q.getvalue()

    buf_j = _TtyBuffer()
    monkeypatch.setattr(sys, "stdout", buf_j)
    ui.setup(json_mode=True)
    ui.print_run_summary(payload, fallback_text="FALLBACK_TEXT")
    assert buf_j.getvalue() == ""

    # 3. Non-TTY mode emits fallback_text cleanly without ANSI escapes
    monkeypatch.undo()
    ui.setup(json_mode=False, quiet=False)
    ui.print_run_summary(payload, fallback_text="FALLBACK_PLAIN\n")
    cap = capsys.readouterr().out
    assert "FALLBACK_PLAIN" in cap
    assert "\x1b[" not in cap


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
