"""CLI dispatch, global flags, JSON envelopes, and role matrix tests."""

from __future__ import annotations

import json
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from solouvm.cli import VERSION, main
from solouvm.init import init_project
from solouvm.model import Job, Plan
from solouvm.plan import write_plan


def _make_project(tmp_path: Path, extra_cases: str = "") -> Path:
    init_project(root=tmp_path, simulator="fake")
    if extra_cases:
        cfile = tmp_path / "case.py"
        cfile.write_text(cfile.read_text(encoding="utf-8") + "\n" + extra_cases, encoding="utf-8")
    return tmp_path


def test_version_text_and_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"solouvm {VERSION}"

    assert main(["--json", "--version"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert list(payload.keys()) == ["schema", "command", "ok", "data", "truncated", "next"]
    assert payload["ok"] is True
    assert payload["command"] == "version"
    assert payload["data"] == {"version": VERSION}


def test_empty_args_and_unknown_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 2
    _ = capsys.readouterr()
    with pytest.raises(SystemExit) as exc:
        main(["bogus"])
    assert exc.value.code == 2


@pytest.mark.parametrize(
    "make_argv",
    [
        lambda p: ["--json", "-c", str(p), "list"],
        lambda p: ["list", "--json", "-c", str(p)],
    ],
)
def test_list_global_flags_before_and_after(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    make_argv,
) -> None:
    proj = _make_project(tmp_path)
    assert main(make_argv(proj)) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert list(payload.keys()) == ["schema", "command", "ok", "data", "truncated", "next"]
    assert payload["schema"] == 1
    assert payload["command"] == "list"
    assert payload["ok"] is True
    assert payload["truncated"] is None
    assert payload["data"]["total_jobs"] >= 1


def test_list_text_columns_no_ansi(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    proj = _make_project(tmp_path)
    monkeypatch.chdir(proj)
    assert main(["list"]) == 0
    out = capsys.readouterr().out
    assert "\x1b[" not in out
    for col in ("JOB", "CASE", "SEED", "BUILD", "TAGS", "STAGE"):
        assert col in out


def test_list_limit_truncation_and_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    extra = 'CASES.extend([Case(name=f"c{i}", uvm_test="axi_smoke_test") for i in range(6)])\n'
    proj = _make_project(tmp_path, extra)

    assert main(["-c", str(proj), "list", "--limit", "2", "--json"]) == 0
    p1 = json.loads(capsys.readouterr().out)
    assert len(p1["data"]["jobs"]) == 2
    assert p1["truncated"]["omitted"] == p1["data"]["total_jobs"] - 2

    assert main(["-c", str(proj), "list", "--limit", "0", "--json"]) == 0
    p2 = json.loads(capsys.readouterr().out)
    assert p2["truncated"] is None
    assert len(p2["data"]["jobs"]) == p2["data"]["total_jobs"]


def test_files_marks_outside_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ext = tmp_path / "external_rtl.sv"
    ext.write_text("module ext; endmodule\n", encoding="utf-8")
    proj = tmp_path / "proj"
    proj.mkdir()
    _make_project(proj)
    fpath = proj / "filelist.py"
    text = fpath.read_text(encoding="utf-8").replace(
        '"rtl/**/*.sv",',
        f'"rtl/**/*.sv",\n        "{ext}",',
    )
    fpath.write_text(text, encoding="utf-8")

    assert main(["-c", str(proj), "files"]) == 0
    txt_out = capsys.readouterr().out
    assert "[OUTSIDE ROOT]" in txt_out

    assert main(["-c", str(proj), "files", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(e["outside_root"] for e in payload["data"]["entries"])


def test_validate_and_json_error_envelope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    proj = _make_project(tmp_path)
    assert main(["-c", str(proj), "validate"]) == 0
    _ = capsys.readouterr()

    conf = proj / "solouvm.conf.py"
    conf.write_text(conf.read_text(encoding="utf-8") + '\nCFG["bogus_key"] = 1\n', encoding="utf-8")

    assert main(["-c", str(proj), "validate"]) == 2
    val_out = capsys.readouterr().out
    assert "solouvm.conf.py" in val_out
    assert "error" in val_out.lower()

    assert main(["--json", "-c", str(proj), "list"]) == 2
    err_payload = json.loads(capsys.readouterr().out)
    assert err_payload["ok"] is False
    assert list(err_payload["error"].keys()) == ["code", "message", "file", "line", "hint"]
    assert err_payload["error"]["code"] == "CFG_UNKNOWN_KEY"


@pytest.mark.parametrize(
    "argv",
    [
        ["list"],
        ["notes"],
        ["help"],
        ["schema"],
    ],
)
def test_role_triage_allows_readonly(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    proj = _make_project(tmp_path)
    assert main(["--role", "triage", "-c", str(proj), *argv]) == 0
    _ = capsys.readouterr()


@pytest.mark.parametrize(
    "argv",
    [
        ["gen"],
        ["regress"],
        ["todo", "done", "smoke#1"],
        ["clean"],
    ],
)
def test_role_triage_denies_restricted_commands(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    proj = _make_project(tmp_path)
    assert main(["--json", "--role", "triage", "-c", str(proj), *argv]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "ROLE_DENIED"


def test_invalid_role_rejected_as_role_denied(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    proj = _make_project(tmp_path)
    assert main(["--json", "--role", "bogus", "-c", str(proj), "list"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "ROLE_DENIED"


def test_ops_and_records_cli_smoke(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    proj = _make_project(tmp_path)

    for cmd in (
        ["-c", str(proj), "config", "--json"],
        ["-c", str(proj), "schema", "results", "--json"],
        ["-c", str(proj), "doctor", "--json"],
        ["-c", str(proj), "help", "verdict", "--json"],
        ["-c", str(proj), "uvm", "diff", "--json"],
        ["-c", str(proj), "lint-style", "--json"],
        ["-c", str(proj), "bug", "add", "smoke", "--title", "clk glitch", "--json"],
        ["-c", str(proj), "bug", "update", "smoke#1", "--status", "fixed", "--json"],
        ["-c", str(proj), "note", "add", "smoke", "--text", "investigated waveform", "--json"],
        ["-c", str(proj), "todo", "add", "smoke", "--title", "add corner seed", "--json"],
        ["-c", str(proj), "todo", "done", "smoke#2", "--json"],
        ["-c", str(proj), "notes", "--json"],
        ["-c", str(proj), "notes", "--next-id", "smoke", "--json"],
        ["-c", str(proj), "progress", "--json"],
        ["-c", str(proj), "doc", "-o", str(proj / "out_docs"), "--json"],
    ):
        rc = main(cmd)
        out = capsys.readouterr().out
        assert rc == 0, f"cmd {cmd} failed: {out}"
        payload = json.loads(out)
        assert payload["ok"] is True

    assert (proj / "out_docs" / "index.md").is_file()
    assert (proj / "out_docs" / "smoke.md").is_file()


def test_cli_report_text_html_output_and_compare(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    proj = _make_project(tmp_path)
    for rid, cur_status in (("r_base", "pass"), ("r_cur", "false_pass")):
        rdir = proj / ".solouvm" / "runs" / rid
        rdir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": 1,
            "run_id": rid,
            "judged_at": "2026-11-18T12:00:00+08:00",
            "partial": False,
            "summary": {
                "total": 1,
                "passed": 1 if cur_status == "pass" else 0,
                "by_status": {cur_status: 1},
                "by_category": {"design": 1} if cur_status == "false_pass" else {},
                "duration_s": 1.0,
            },
            "jobs": [
                {
                    "job_id": "smoke__1",
                    "case": "smoke",
                    "seed": 1,
                    "status": cur_status,
                    "judged_status": "pass",
                    "raw_status": "pass",
                    "category": "design" if cur_status == "false_pass" else None,
                    "reason": "clean",
                    "attempts": [{"try": 1, "status": "pass", "duration_s": 1.0, "wave": False}],
                }
            ],
            "signatures": [],
            "waivers": {"used": [], "unused": [], "expired": []},
            "hints": [],
        }
        (rdir / "results.json").write_text(json.dumps(payload), encoding="utf-8")

    # Text report without --compare
    assert main(["-c", str(proj), "report", "--run", "r_cur"]) == 0
    txt_out = capsys.readouterr().out
    assert "Run: r_cur" in txt_out

    # Text report with --compare (default status mode)
    assert main(["-c", str(proj), "report", "--run", "r_cur", "--compare", "r_base"]) == 0
    cmp_out = capsys.readouterr().out
    assert "Compare vs r_base:" in cmp_out
    assert "New failures (1):" in cmp_out
    assert "smoke__1: pass -> false_pass" in cmp_out

    # Judged mode ignores triage false_pass
    assert (
        main(
            [
                "-c",
                str(proj),
                "report",
                "--run",
                "r_cur",
                "--compare",
                "r_base",
                "--compare-mode",
                "judged",
            ]
        )
        == 0
    )
    judged_out = capsys.readouterr().out
    assert "New failures (0):" in judged_out

    # HTML output via -o
    html_out = tmp_path / "rep.html"
    assert (
        main(
            [
                "-c",
                str(proj),
                "report",
                "--run",
                "r_cur",
                "--format",
                "html",
                "--compare",
                "r_base",
                "-o",
                str(html_out),
            ]
        )
        == 0
    )
    _ = capsys.readouterr()
    assert "Comparison vs r_base" in html_out.read_text(encoding="utf-8")

    # JSON mode with --compare includes compare object
    assert (
        main(["-c", str(proj), "report", "--run", "r_cur", "--compare", "r_base", "--json"]) == 0
    )
    json_out = json.loads(capsys.readouterr().out)
    assert json_out["ok"] is True
    assert json_out["data"]["compare"]["baseline_run_id"] == "r_base"
    assert json_out["data"]["compare"]["new_failures"][0]["job_id"] == "smoke__1"


def test_cli_shard_seed_reconstructs_random_plan(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    extra = 'CASES.append(Case(name="extra", uvm_test="axi_smoke_test"))\n'
    proj = _make_project(tmp_path, extra)

    assert (
        main(["--json", "-c", str(proj), "list", "--seeds", "random:4", "--shard-seed", "s1"])
        == 0
    )
    full = json.loads(capsys.readouterr().out)
    assert main(
        [
            "--json",
            "-c",
            str(proj),
            "list",
            "--seeds",
            "random:4",
            "--shard-seed",
            "s1",
            "--shard",
            "1/2",
        ]
    ) == 0
    s1 = json.loads(capsys.readouterr().out)
    assert main(
        [
            "--json",
            "-c",
            str(proj),
            "list",
            "--seeds",
            "random:4",
            "--shard-seed",
            "s1",
            "--shard",
            "2/2",
        ]
    ) == 0
    s2 = json.loads(capsys.readouterr().out)

    full_ids = [j["job_id"] for j in full["data"]["jobs"]]
    ids1 = [j["job_id"] for j in s1["data"]["jobs"]]
    ids2 = [j["job_id"] for j in s2["data"]["jobs"]]
    assert len(full_ids) == 8
    assert len(ids1) == 4 and len(ids2) == 4
    assert set(ids1).isdisjoint(set(ids2))
    assert set(ids1) | set(ids2) == set(full_ids)


def test_cli_invalid_budget_and_max_fail_return_structured_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    proj = _make_project(tmp_path)

    rc_budget = main(["--json", "-c", str(proj), "regress", "--budget", "bogus"])
    captured = capsys.readouterr()
    assert rc_budget == 2
    assert "Traceback" not in captured.err
    err_b = json.loads(captured.out)
    assert err_b["ok"] is False
    assert err_b["error"]["code"] == "CFG_INVALID"
    assert "--budget" in err_b["error"]["message"]

    rc_mf = main(["--json", "-c", str(proj), "regress", "--max-fail", "0"])
    captured2 = capsys.readouterr()
    assert rc_mf == 2
    err_m = json.loads(captured2.out)
    assert err_m["ok"] is False
    assert err_m["error"]["code"] == "CFG_INVALID"
    assert "--max-fail" in err_m["error"]["message"]


def test_cli_post_run_commands_follow_plan_provider(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    proj = _make_project(tmp_path / "proj")
    alt_root = tmp_path / "alt_remote"
    alt_root.mkdir()
    (proj / "alt_prov.py").write_text(
        textwrap.dedent(
            """\
            from solouvm.providers.transport_local import LocalTransport


            class AltTransport(LocalTransport):
                kind = "alt_local"

                def __init__(self, cfg):
                    super().__init__(root=cfg["workdir"])
            """
        ),
        encoding="utf-8",
    )
    (proj / "solouvm.conf.py").write_text(
        textwrap.dedent(
            f"""\
            CFG = {{
                "simulator": "fake",
                "top": "tb_top",
                "plugins": ["alt_prov.AltTransport"],
                "provider": "local",
                "providers": {{
                    "local": {{}},
                    "alt": {{"transport": {{"kind": "alt_local", "workdir": {str(alt_root)!r}}}}},
                }},
            }}
            """
        ),
        encoding="utf-8",
    )

    run_id = "r_alt"
    plan = Plan(
        run_id=run_id,
        created_at=datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc),
        parent_run=None,
        provider="alt",
        selection={},
        builds={"default": "b1"},
        jobs=[
            Job(
                job_id="smoke__1",
                case="smoke",
                seed=1,
                build="default",
                uvm_test="axi_smoke_test",
                params={},
                opts=[],
                env={},
                timeout_s=60.0,
                hang_s=None,
                retry=0,
                expect_fail=False,
                tags=(),
                dir=f".solouvm/runs/{run_id}/smoke__1",
            )
        ],
    )
    write_plan(proj / ".solouvm" / "runs" / run_id / "plan.json", plan)

    ok_file = alt_root / ".solouvm" / "build" / "default" / "b1" / ".ok"
    ok_file.parent.mkdir(parents=True, exist_ok=True)
    ok_file.write_text("", encoding="utf-8")

    try_dir = alt_root / ".solouvm" / "runs" / run_id / "smoke__1" / "try1"
    try_dir.mkdir(parents=True, exist_ok=True)
    (try_dir / "job.facts").write_text(
        "schema=1\n"
        "generator=solouvm 0.1.0\n"
        "kind=job\n"
        f"run_id={run_id}\n"
        "job_id=smoke__1\n"
        "try=1\n"
        "host=altbox\n"
        "pid=123\n"
        "timeout_impl=timeout\n"
        "started_at=2026-11-20T12:00:00+00:00\n"
        "state=done\n"
        "rc=0\n"
        "signal=\n"
        "timed_out=0\n"
        "hung=0\n"
        "finished_at=2026-11-20T12:00:01+00:00\n"
        "duration_s=1.0\n",
        encoding="utf-8",
    )
    (try_dir / "sim.log").write_text("SOLOUVM_DONE status=pass\n", encoding="utf-8")

    assert main(["--json", "-c", str(proj), "status", "--run", run_id]) == 0
    st = json.loads(capsys.readouterr().out)
    assert st["data"]["summary"]["done"] == 1

    assert main(["--json", "-c", str(proj), "log", "smoke__1", "--run", run_id]) == 0
    lg = json.loads(capsys.readouterr().out)
    assert "SOLOUVM_DONE" in lg["data"]["lines"][0]["text"]

    # Case expect in init_project expects AXI_SCB/sim_time; test with explicit empty expect
    (proj / "case.py").write_text(
        'from solouvm import Case\nCASES = [Case(name="smoke", expect=[])]\n',
        encoding="utf-8",
    )
    assert main(["--json", "-c", str(proj), "judge", "--run", run_id]) == 0
    jg = json.loads(capsys.readouterr().out)
    assert jg["data"]["summary"]["passed"] == 1

    # Explicit --provider local overrides plan.provider and sees missing remote artifacts
    assert main(["--json", "-c", str(proj), "--provider", "local", "judge", "--run", run_id]) == 2
    _ = capsys.readouterr()
