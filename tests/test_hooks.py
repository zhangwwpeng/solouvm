"""Tests for A-side lifecycle hooks, 5-stage transitions, and --no-hooks (W-01..W-04)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from solouvm import hooks
from solouvm.cli import main
from solouvm.errors import HookError
from solouvm.init import init_project


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    return path


def _setup_proj(
    root: Path,
    *,
    with_b_hooks: bool = False,
) -> Path:
    init_project(root=root, simulator="fake", bare=False, agents=None)
    _write(
        root / "case.py",
        'from solouvm import Case\nCASES = [Case(name="smoke", expect=[])]\n',
    )
    job_block = ""
    if with_b_hooks:
        job_block = """
            "job": {
                "pre": 'echo "B_SIDE_PRE_HOOK"',
                "post": 'echo "B_SIDE_POST_HOOK"',
            },"""
    _write(
        root / "solouvm.conf.py",
        f"""\
        CFG = {{
            "simulator": "fake",
            "top": "tb_top",{job_block}
        }}
        """,
    )
    return root


def test_find_hook_precedence_and_validation(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()

    assert hooks.find_hook(proj, "pre", "gen") is None

    in_hooks_dir = _write(proj / "hooks" / "pre_hook_gen.py", "def hook(ctx):\n    pass\n")
    assert hooks.find_hook(proj, "pre", "gen") == in_hooks_dir

    in_root = _write(proj / "pre_hook_gen.py", "def hook(ctx):\n    pass\n")
    assert hooks.find_hook(proj, "pre", "gen") == in_root

    with pytest.raises(ValueError, match="phase"):
        hooks.find_hook(proj, "during", "gen")
    with pytest.raises(ValueError, match="stage"):
        hooks.find_hook(proj, "pre", "unknown")


def test_full_five_stage_and_hook_order_with_b_hooks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proj = _setup_proj(tmp_path / "proj", with_b_hooks=True)
    trace = proj / "hook_trace.txt"

    for stage in ("main", "gen", "push", "build", "run", "judge"):
        for phase in ("pre", "post"):
            _write(
                proj / "hooks" / f"{phase}_hook_{stage}.py",
                f"""\
                from pathlib import Path

                def hook(ctx):
                    with Path({str(trace)!r}).open("a", encoding="utf-8") as fh:
                        fh.write(f"{{ctx.phase}}:{{ctx.stage}}\\n")
                    ctx.log(f"hooked {{ctx.phase}} {{ctx.stage}}")
                """,
            )

    rc = main(["-c", str(proj), "regress", "--rebuild"])
    assert rc == 0

    order = trace.read_text(encoding="utf-8").splitlines()
    assert order == [
        "pre:main",
        "pre:gen",
        "post:gen",
        "pre:push",
        "post:push",
        "pre:build",
        "post:build",
        "pre:run",
        "post:run",
        "pre:judge",
        "post:judge",
        "post:main",
    ]

    out = capsys.readouterr().out
    for st in ("Generate", "Sync", "Compile", "Simulate", "Judge"):
        assert f"[info]  {st}" in out
    assert "[1/5]" not in out
    assert "[ok]" not in out
    assert "[hook] pre:gen (hooks/pre_hook_gen.py)" in out
    assert "[hook] [pre:gen] hooked pre gen" in out
    assert "[hook] post:judge ok" in out

    # B-side job.pre / job.post executed inside sim.log
    sim_logs = list((proj / ".solouvm" / "runs" / "latest").glob("smoke__*/try1/sim.log"))
    assert sim_logs
    sim_text = sim_logs[0].read_text(encoding="utf-8")
    assert "B_SIDE_PRE_HOOK" in sim_text
    assert "B_SIDE_POST_HOOK" in sim_text


def test_no_hooks_skips_all_a_side_hooks_and_resets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proj = _setup_proj(tmp_path / "proj")
    marker = proj / "hook_ran.txt"
    for stage in ("main", "gen", "push", "build", "run", "judge"):
        for phase in ("pre", "post"):
            _write(
                proj / f"{phase}_hook_{stage}.py",
                f"""\
                from pathlib import Path

                def hook(ctx):
                    Path({str(marker)!r}).write_text("ran", encoding="utf-8")
                """,
            )

    rc = main(["-c", str(proj), "--no-hooks", "regress"])
    assert rc == 0
    assert not marker.exists()
    out = capsys.readouterr().out
    assert "[hook]" not in out
    assert "[info]  Generate" in out
    assert "[info]  Judge" in out

    # Next invocation without --no-hooks re-enables hooks cleanly
    rc2 = main(["-c", str(proj), "gen"])
    assert rc2 == 0
    assert marker.exists()
    _ = capsys.readouterr()


@pytest.mark.parametrize(
    ("case_name", "body", "substr"),
    [
        ("exc", "def hook(ctx):\n    raise RuntimeError('boom')\n", "boom"),
        ("missing_fn", "X = 1\n", "callable hook"),
        ("syntax", "def hook(:\n", "failed to load hook"),
    ],
)
def test_hook_failures_exit_2_in_human_and_json_modes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    case_name: str,
    body: str,
    substr: str,
) -> None:
    proj = _setup_proj(tmp_path / case_name)
    _write(proj / "pre_hook_gen.py", body)

    rc_human = main(["-c", str(proj), "gen"])
    assert rc_human == 2
    cap_h = capsys.readouterr()
    assert substr in cap_h.err
    assert "HOOK_FAILED" in repr(HookError.code)

    rc_json = main(["--json", "-c", str(proj), "gen"])
    assert rc_json == 2
    cap_j = capsys.readouterr()
    payload = json.loads(cap_j.out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "HOOK_FAILED"
    assert substr in payload["error"]["message"]


def test_hook_output_safety_in_json_and_quiet_modes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proj = _setup_proj(tmp_path / "proj")
    for stage in ("main", "gen", "push", "build", "run", "judge"):
        for phase in ("pre", "post"):
            _write(
                proj / "hooks" / f"{phase}_hook_{stage}.py",
                """\
                def hook(ctx):
                    print(f"RAW_PRINT_{ctx.phase}_{ctx.stage}")
                    ctx.log(f"CTX_LOG_{ctx.phase}_{ctx.stage}")
                """,
            )

    # 1. Single-envelope JSON commands (`gen --json`, `judge --json`) must stay valid single JSON
    assert main(["--json", "-c", str(proj), "gen"]) == 0
    gen_payload = json.loads(capsys.readouterr().out)
    assert gen_payload["ok"] is True
    assert gen_payload["command"] == "gen"

    # 2. Streaming JSON (`regress --json`) emits stage_*/hook_* JSONL events before final envelope
    assert main(["--json", "-c", str(proj), "regress", "--rebuild"]) == 0
    joined = capsys.readouterr().out
    dec = json.JSONDecoder()
    idx = 0
    objs: list[dict[str, object]] = []
    while idx < len(joined):
        while idx < len(joined) and joined[idx].isspace():
            idx += 1
        if idx >= len(joined):
            break
        obj, idx = dec.raw_decode(joined, idx)
        objs.append(obj)

    events = objs[:-1]
    final_env = objs[-1]
    assert "event" not in final_env
    assert final_env["command"] == "regress"
    assert final_env["ok"] is True

    stage_pairs = [(e["event"], e["stage"]) for e in events if str(e["event"]).startswith("stage_")]
    assert stage_pairs == [
        ("stage_start", "gen"),
        ("stage_done", "gen"),
        ("stage_start", "push"),
        ("stage_done", "push"),
        ("stage_start", "build"),
        ("stage_done", "build"),
        ("stage_start", "run"),
        ("stage_done", "run"),
        ("stage_start", "judge"),
        ("stage_done", "judge"),
    ]
    hook_pairs = [
        (e["event"], e["phase"], e["stage"])
        for e in events
        if str(e["event"]).startswith("hook_")
    ]
    assert ("hook_start", "pre", "main") in hook_pairs
    assert ("hook_done", "pre", "main") in hook_pairs
    assert ("hook_done", "post", "judge") in hook_pairs
    # post:main runs after emit_json and must NOT emit trailing events
    assert ("hook_start", "post", "main") not in hook_pairs

    # 3. `judge --json` stays single clean JSON even with hooks printing
    assert main(["--json", "-c", str(proj), "judge"]) == 0
    judge_payload = json.loads(capsys.readouterr().out)
    assert judge_payload["ok"] is True
    assert judge_payload["command"] == "judge"

    # 4. --quiet suppresses hook notices, hook logs, and stage banners while leaving summary
    assert main(["-q", "-c", str(proj), "regress"]) == 0
    q_out = capsys.readouterr().out
    assert "RAW_PRINT" not in q_out
    assert "CTX_LOG" not in q_out
    assert "Generate" not in q_out
    assert "[hook]" not in q_out


def test_hook_echo_persists_while_print_and_log_stay_stage_scoped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    proj = _setup_proj(tmp_path / "proj")
    _write(
        proj / "hooks" / "pre_hook_gen.py",
        """\
        from solouvm import echo, error, info, warn

        def hook(ctx):
            print("RAW_STAGE_PRINT")
            ctx.log("CTX_STAGE_LOG")
            ctx.echo("PERSIST_FROM_CTX_ECHO [ok]")
            echo("PERSIST_FROM_TOP_ECHO")
            ctx.info("INFO_FROM_CTX")
            info("INFO_FROM_TOP")
            ctx.warn("WARN_FROM_CTX")
            warn("WARN_FROM_TOP")
            ctx.error("ERR_FROM_CTX")
            error("ERR_FROM_TOP")
        """,
    )

    # 1. Human mode: both echo & unified logging tag source script path without buf capture
    assert main(["-c", str(proj), "gen"]) == 0
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert "[info] [hooks/pre_hook_gen.py] PERSIST_FROM_CTX_ECHO [ok]\n" in out
    assert "[info] [hooks/pre_hook_gen.py] PERSIST_FROM_TOP_ECHO\n" in out
    assert "[info] [hooks/pre_hook_gen.py] INFO_FROM_CTX\n" in out
    assert "[info] [hooks/pre_hook_gen.py] INFO_FROM_TOP\n" in out
    assert "[warn] [hooks/pre_hook_gen.py] WARN_FROM_CTX\n" in err
    assert "[warn] [hooks/pre_hook_gen.py] WARN_FROM_TOP\n" in err
    assert "[error] [hooks/pre_hook_gen.py] ERR_FROM_CTX\n" in err
    assert "[error] [hooks/pre_hook_gen.py] ERR_FROM_TOP\n" in err
    assert "[pre:gen] PERSIST_FROM" not in out
    assert "[pre:gen] INFO_FROM" not in out
    assert "[hook] [pre:gen] RAW_STAGE_PRINT" in out
    assert "[hook] [pre:gen] CTX_STAGE_LOG" in out

    # 2. JSON single-envelope and streaming modes silence echo & info cleanly
    assert main(["--json", "-c", str(proj), "gen"]) == 0
    json_cap = capsys.readouterr()
    gen_payload = json.loads(json_cap.out)
    assert gen_payload["ok"] is True

    assert main(["--json", "-c", str(proj), "regress", "--rebuild"]) == 0
    reg_out = capsys.readouterr().out
    assert "PERSIST_FROM_" not in reg_out
    assert "INFO_FROM_" not in reg_out
    assert "RAW_STAGE_PRINT" not in reg_out
    assert "CTX_STAGE_LOG" not in reg_out

    # 3. --quiet silences echo/print/log cleanly while keeping the final generated summary line
    assert main(["-q", "-c", str(proj), "gen"]) == 0
    q_gen = capsys.readouterr().out
    assert "generated run" in q_gen
    assert "PERSIST_FROM_" not in q_gen
    assert "RAW_STAGE_PRINT" not in q_gen
    assert "CTX_STAGE_LOG" not in q_gen
