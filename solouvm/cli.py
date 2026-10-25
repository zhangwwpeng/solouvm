"""Command line interface.

Stages mirror the README, with the execution boundary made explicit:

===========  ======================================================
``gen``      collect sources + cases, write filelist.f/plan/Makefile
``build``    ``gen`` then compile
``run``      ``gen`` then execute the selected cases
``regress``  ``gen`` then execute every planned case
``end``      aggregate status.json files into results.json (+ exit code)
``_exec``    internal: run one case (called by the generated Makefile)
===========  ======================================================
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import collect, executor, hooks, makefile, report
from . import plan as plan_mod
from . import status as status_mod
from .config import Config, load_config, resolve_config_path
from .errors import (
    EXIT_INTERRUPT,
    EXIT_OK,
    ConfigError,
    SolouvmError,
)
from .util import parse_seeds, relpath, write_text_atomic

PROG = "solouvm"


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    # SUPPRESS keeps subparser defaults from clobbering the top-level flags.
    common.add_argument(
        "-c", "--config", default=argparse.SUPPRESS, help="path to solouvm.py (default: search upwards)"
    )
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS,
        help="stream simulator output",
    )
    common.add_argument(
        "-q", "--quiet", action="store_true", default=argparse.SUPPRESS, help="only print errors"
    )
    common.add_argument(
        "--no-hooks", action="store_true", default=argparse.SUPPRESS, help="skip pre/post hooks"
    )

    parser = argparse.ArgumentParser(
        prog=PROG,
        description="Generate UVM frameworks and runners from Python configuration.",
    )
    parser.add_argument("-c", "--config", default=argparse.SUPPRESS, help="path to solouvm.py")
    parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("-q", "--quiet", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--version", action="store_true", help="print version and exit")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    gen = sub.add_parser("gen", parents=[common], help="generate filelist.f, plan.json and Makefile")
    _add_seed_args(gen)

    sub.add_parser("build", parents=[common], help="generate and compile")

    run = sub.add_parser("run", parents=[common], help="generate and execute selected cases")
    _add_seed_args(run)
    run.add_argument("--case", action="append", metavar="NAME", help="case to run (repeatable)")
    run.add_argument("-j", "--jobs", type=int, help="parallel jobs (make -jN)")

    regress = sub.add_parser("regress", parents=[common], help="generate and execute everything")
    _add_seed_args(regress)
    regress.add_argument("-j", "--jobs", type=int, help="parallel jobs (make -jN)")

    end = sub.add_parser("end", parents=[common], help="aggregate status.json into results.json")
    end.add_argument("--plan", help="plan.json to aggregate (default: <workdir>/plan.json)")
    end.add_argument(
        "--allow-missing",
        action="store_true",
        help="treat never-run cases as skip instead of error (sharded runs)",
    )

    sub.add_parser("clean", parents=[common], help="remove generated run directories")

    internal = sub.add_parser("_exec", add_help=False)
    internal.add_argument("--plan", required=True)
    internal.add_argument("--case-id", required=True)
    internal.add_argument("-v", "--verbose", action="store_true")

    return parser


def _add_seed_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--seed", action="append", type=int, metavar="N", help="seed (repeatable)")
    parser.add_argument("--seeds", metavar="SPEC", help='seed spec, e.g. "1-5" or "1,7,42"')


def main(argv: Sequence[str] | None = None) -> int:
    _line_buffered_stdout()
    args = build_parser().parse_args(argv)
    _normalise(args)
    try:
        return _dispatch(args)
    except SolouvmError as exc:
        print(f"{PROG}: error: {exc.render()}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print(f"{PROG}: interrupted", file=sys.stderr)
        return EXIT_INTERRUPT


def _line_buffered_stdout() -> None:
    """Keep our own prints ordered with subprocess output (make, _exec)."""
    stream = sys.stdout
    if hasattr(stream, "reconfigure"):
        try:
            stream.reconfigure(line_buffering=True)
        except (ValueError, OSError):  # pragma: no cover - exotic streams
            pass


def _normalise(args: argparse.Namespace) -> None:
    """Fill defaults for flags declared with ``SUPPRESS``."""
    for flag in ("verbose", "quiet", "no_hooks"):
        setattr(args, flag, bool(getattr(args, flag, False)))
    if not hasattr(args, "config"):
        args.config = None


def _dispatch(args: argparse.Namespace) -> int:
    from . import __version__

    if args.version or args.command is None:
        print(f"{PROG} {__version__}")
        return EXIT_OK if args.version else 0

    if args.command == "_exec":
        # No config load here: the plan is the single source of truth.
        return executor.run(Path(args.plan).resolve(), args.case_id, verbose=args.verbose)

    cfg = load_config(resolve_config_path(args.config, Path.cwd()))
    ctx: dict[str, Any] = {
        "command": args.command,
        "root": cfg.root,
        "workdir": str(cfg.workdir),
        "project": cfg.project,
        "verbose": args.verbose,
    }

    _fire(args, "main", "pre", ctx)
    try:
        code = _run_command(args, cfg, ctx)
    finally:
        _fire(args, "main", "post", ctx)
    return code


def _run_command(args: argparse.Namespace, cfg: Config, ctx: dict[str, Any]) -> int:
    command = args.command

    if command == "clean":
        return _clean(cfg)

    if command == "end":
        return _end(args, cfg, ctx)

    plan = _gen(args, cfg, ctx)
    if command == "gen":
        return EXIT_OK

    if command == "build":
        return _make(cfg, ["build"], jobs=None, verbose=args.verbose)

    jobs = getattr(args, "jobs", None)
    end_plan = plan
    if command == "run":
        selected = _selected_case_ids(plan, getattr(args, "case", None))
        targets = [f"run/{case_id}" for case_id in selected] or ["regress"]
        if selected:
            wanted = set(selected)
            end_plan = {
                **plan,
                "cases": [item for item in plan["cases"] if item["case_id"] in wanted],
            }
    else:
        targets = ["regress"]

    make_rc = _make(cfg, targets, jobs=jobs, verbose=args.verbose)
    # `make` returns per-target status; the aggregate verdict comes from `end`,
    # which is idempotent and also runs when make aborted early.
    code = _end(args, cfg, ctx, plan=end_plan)
    if code == EXIT_OK and make_rc != EXIT_OK and not _has_results(cfg):
        return make_rc
    return code


def _gen(args: argparse.Namespace, cfg: Config, ctx: dict[str, Any]) -> dict[str, Any]:
    _fire(args, "com", "pre", ctx)
    files = collect.load_filelist(cfg)
    filelist = makefile.write_filelist(cfg, files)
    _fire(args, "com", "post", ctx)
    if not args.quiet:
        print(f"{PROG}: wrote {relpath(filelist, cfg.root)} ({len(files)} files)")

    _fire(args, "run", "pre", ctx)
    cases = collect.load_cases(cfg)
    seeds = _resolve_seeds(args, cfg)
    plan = plan_mod.build(cfg, cases, seeds=seeds)
    plan_file = plan_mod.write(cfg, plan)
    written = makefile.write_make(cfg, plan)
    ctx["plan"] = plan
    _fire(args, "run", "post", ctx)

    if not args.quiet:
        print(
            f"{PROG}: planned {len(plan['cases'])} case(s) from {len(cases)} case(s)"
            f" x {len(seeds)} seed(s) -> {relpath(plan_file, cfg.root)}"
        )
        print(f"{PROG}: wrote {', '.join(relpath(path, cfg.root) for path in written)}")
    return plan


def _end(
    args: argparse.Namespace,
    cfg: Config,
    ctx: dict[str, Any],
    *,
    plan: dict[str, Any] | None = None,
) -> int:
    plan_path = Path(args.plan) if getattr(args, "plan", None) else plan_mod.plan_path(cfg)
    plan = plan if plan is not None else plan_mod.load(plan_path)
    root = Path(plan["root"])
    workdir = root / str(plan["workdir"])

    _fire(args, "end", "pre", ctx)
    specs = plan_mod.specs(plan)
    statuses = status_mod.collect(
        plan,
        specs,
        root,
        allow_missing=bool(getattr(args, "allow_missing", False)),
    )
    results = status_mod.aggregate(plan, statuses)

    results_file = workdir / "results.json"
    status_mod.save(results_file, results)
    write_text_atomic(results_file.with_suffix(".txt"), report.render_text(results))
    _fire(args, "end", "post", ctx)

    if not args.quiet:
        print(report.render_text(results, verbose=args.verbose))
        print(f"{PROG}: results in {relpath(results_file, root)}")
    return status_mod.exit_code(results)


def _make(cfg: Config, targets: list[str], *, jobs: int | None, verbose: bool = False) -> int:
    makefile_path = cfg.workdir / "Makefile"
    if not makefile_path.is_file():
        raise ConfigError(f"missing {makefile_path}", hint="run `solouvm gen` first")
    count = jobs or cfg.jobs
    cmd = [
        "make",
        "-C",
        str(cfg.root),
        "-f",
        relpath(makefile_path, cfg.root),
        "--no-print-directory",
        f"-j{count}",
        *targets,
    ]
    print(f"{PROG}: {' '.join(shlex.quote(part) for part in cmd)}")
    try:
        return subprocess.run(
            cmd,
            cwd=str(cfg.root),
            check=False,
            env={**os.environ, "SOLOUVM_EXEC_FLAGS": "-v" if verbose else ""},
        ).returncode
    except FileNotFoundError:
        raise ConfigError(
            "`make` is not available on PATH",
            hint="install make, or run the generated Makefile with your build tool",
        ) from None


def _clean(cfg: Config) -> int:
    import shutil

    out = cfg.out_root
    if out.exists():
        shutil.rmtree(out)
    for name in ("results.json", "results.txt"):
        (cfg.workdir / name).unlink(missing_ok=True)
    print(f"{PROG}: removed {relpath(out, cfg.root)}")
    return EXIT_OK


def _resolve_seeds(args: argparse.Namespace, cfg: Config) -> list[int]:
    explicit = getattr(args, "seed", None)
    spec = getattr(args, "seeds", None)
    if explicit and spec:
        raise ConfigError("use either --seed or --seeds, not both")
    if spec:
        return parse_seeds(spec)
    if explicit:
        return parse_seeds(list(explicit))
    return list(cfg.seeds)


def _selected_case_ids(plan: dict[str, Any], names: list[str] | None) -> list[str]:
    if not names:
        return []
    wanted = set(names)
    ids = [item["case_id"] for item in plan["cases"] if item["case"] in wanted]
    missing = wanted - {item["case"] for item in plan["cases"]}
    if missing:
        raise ConfigError(
            f"unknown case(s): {', '.join(sorted(missing))}",
            hint="check case.py, or run `solouvm gen` to refresh the plan",
        )
    return ids


def _has_results(cfg: Config) -> bool:
    return (cfg.workdir / "results.json").is_file()


def _fire(args: argparse.Namespace, stage: str, when: str, ctx: dict[str, Any]) -> None:
    if getattr(args, "no_hooks", False):
        return
    ctx = {**ctx, "stage": stage, "when": when}
    if hooks.run(stage, when, ctx) and args.verbose:
        print(f"{PROG}: ran {when}_hook_{stage}.py")



if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
