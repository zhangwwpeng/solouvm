from __future__ import annotations

import textwrap


def test_pre_and_post_hooks_run_in_order(make_project, capsys):
    project = make_project()
    (project.root / "pre_hook_run.py").write_text(
        textwrap.dedent(
            """
            import pathlib

            def hook(ctx):
                path = pathlib.Path(ctx["root"]) / "hook_trace.txt"
                with path.open("a") as handle:
                    handle.write(f"pre-{ctx['stage']}-{ctx['command']}\\n")
            """
        )
    )
    (project.root / "post_hook_run.py").write_text(
        textwrap.dedent(
            """
            import pathlib

            def hook(ctx):
                path = pathlib.Path(ctx["root"]) / "hook_trace.txt"
                with path.open("a") as handle:
                    handle.write(f"post-{ctx['stage']}-{'plan' if 'plan' in ctx else 'noplan'}\\n")
            """
        )
    )

    assert project.run("gen") == 0
    trace = (project.root / "hook_trace.txt").read_text().splitlines()
    assert trace == ["pre-run-gen", "post-run-plan"]


def test_main_stage_hook_wraps_every_command(make_project):
    project = make_project()
    (project.root / "pre_hook_main.py").write_text(
        textwrap.dedent(
            """
            import pathlib

            def hook(ctx):
                path = pathlib.Path(ctx["root"]) / "main_trace.txt"
                with path.open("a") as handle:
                    handle.write(ctx["command"] + "\\n")
            """
        )
    )
    assert project.run("gen") == 0
    assert project.run("end", "--allow-missing") == 0
    assert (project.root / "main_trace.txt").read_text().splitlines() == ["gen", "end"]


def test_hook_exception_aborts_with_exit_2(make_project, capsys):
    project = make_project()
    (project.root / "pre_hook_com.py").write_text(
        "def hook(ctx):\n    raise RuntimeError('boom')\n"
    )
    assert project.run("gen") == 2
    assert "pre_hook_com.py raised RuntimeError" in capsys.readouterr().err
    assert not (project.workdir / "plan.json").exists()


def test_hook_without_callable_is_rejected(make_project, capsys):
    project = make_project()
    (project.root / "pre_hook_com.py").write_text("VALUE = 1\n")
    assert project.run("gen") == 2
    assert "must define a callable named hook" in capsys.readouterr().err


def test_no_hooks_flag_skips_hooks(make_project):
    project = make_project()
    (project.root / "pre_hook_run.py").write_text(
        "def hook(ctx):\n    raise RuntimeError('should not run')\n"
    )
    assert project.run("gen", "--no-hooks") == 0


def test_hooks_see_plan_on_post_run(make_project):
    project = make_project()
    (project.root / "post_hook_run.py").write_text(
        textwrap.dedent(
            """
            import pathlib

            def hook(ctx):
                plan = ctx["plan"]
                path = pathlib.Path(ctx["root"]) / "count.txt"
                path.write_text(str(len(plan["cases"])))
            """
        )
    )
    assert project.run("gen", "--seeds", "1-3") == 0
    assert (project.root / "count.txt").read_text() == "3"
