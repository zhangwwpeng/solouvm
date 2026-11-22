"""Argument parser definitions for ``solouvm.cli`` (cli.md §1-§7)."""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from solouvm.update import DEFAULT_UPDATE_REPO

Handler = Callable[[argparse.Namespace], int]


def add_global_flags(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Register global flags on root parser or ``common`` subparser parent."""
    d = argparse.SUPPRESS if suppress else None
    parser.add_argument("-c", "--config", default=d, help="Config file or project directory")
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=argparse.SUPPRESS if suppress else 0,
        help="Verbose output (-v stream logs, -vv trace external commands)",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="Only print errors and final summary",
    )
    parser.add_argument(
        "--no-hooks",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="Skip A-side pre/post hook scripts",
    )
    parser.add_argument("--provider", default=d, help="Override configured provider")
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="Emit machine-readable JSON output",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=d,
        help="Output row/line limit (0 disables truncation)",
    )
    parser.add_argument(
        "--role",
        default=argparse.SUPPRESS if suppress else "main",
        help="Execution role (main or triage)",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="Print solouvm version and exit",
    )


def add_selection_flags(parser: argparse.ArgumentParser, *, include_full: bool = True) -> None:
    """Register shared case/job selection options (cli.md §1.3)."""
    parser.add_argument("--case", action="append", default=[], help="Case name glob pattern")
    parser.add_argument("--tag", action="append", default=[], help="Select cases with tag")
    parser.add_argument("--exclude-tag", action="append", default=[], help="Exclude cases with tag")
    if include_full:
        parser.add_argument("--stage", default=None, help="Comma-separated stages")
        parser.add_argument("--seed", type=int, default=None, help="Single seed override")
        parser.add_argument("--seeds", default=None, help="Seed range/random spec override")
        parser.add_argument("--build", default=None, help="Select cases using build mode")
        parser.add_argument("--shard", default=None, help="Shard selection I/N")
        parser.add_argument(
            "--shard-seed",
            default=None,
            help="Shared RNG key for deterministic random:N across shards",
        )
        parser.add_argument("--allow-empty", action="store_true", help="Return 0 when empty")
        parser.add_argument(
            "--order",
            choices=("name", "duration"),
            default="name",
            help="Job scheduling order",
        )


def add_exec_flags(parser: argparse.ArgumentParser) -> None:
    """Register shared execution flags for build/run/regress/rerun (cli.md §1.6)."""
    parser.add_argument("-j", "--jobs", type=int, default=None, help="Parallel job count")
    parser.add_argument("--force", action="store_true", help="Bypass limits.max_jobs")
    parser.add_argument("--detach", action="store_true", help="Launch in background and exit")
    parser.add_argument("--wave", action="store_true", help="Enable waveform capture")
    parser.add_argument("--retry", type=int, default=None, help="Override regress.retry")
    parser.add_argument("--max-fail", type=int, default=None, help="Stop after N failures")
    parser.add_argument("--budget", default=None, help="Total regression time budget")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without running")
    parser.add_argument("--rebuild", action="store_true", help="Force clean rebuild")


def _add_sub(
    subparsers: Any,
    name: str,
    canon: str,
    handlers: dict[str, Handler],
    common: argparse.ArgumentParser,
    help_text: str,
) -> argparse.ArgumentParser:
    p = subparsers.add_parser(name, parents=[common], help=help_text)
    p.set_defaults(canon_cmd=canon, func=handlers[canon])
    return p


def build_parser(handlers: dict[str, Handler]) -> argparse.ArgumentParser:
    """Build the complete CLI parser wiring every command from ``cli.md``."""
    parser = argparse.ArgumentParser(
        prog="solouvm",
        description="SystemVerilog/UVM regression runner and scaffolding toolkit",
    )
    add_global_flags(parser, suppress=False)

    common = argparse.ArgumentParser(add_help=False)
    add_global_flags(common, suppress=True)

    sub = parser.add_subparsers(dest="command", required=False)

    # Preparation & inspection
    p = _add_sub(sub, "init", "init", handlers, common, "Initialize a starter project")
    p.add_argument("dir", nargs="?", default=None, help="Target project directory (default: cwd)")
    p.add_argument("--simulator", default="fake", help="Simulator backend (default: fake)")
    p.add_argument("--agents", default=None, help="Generate subagent templates (e.g. claude)")
    p.add_argument("--bare", action="store_true", help="Only create config files")

    p = _add_sub(sub, "config", "config", handlers, common, "Show merged config and sources")
    p.add_argument("key", nargs="?", default=None, help="Optional dotted key path")

    _add_sub(sub, "files", "files", handlers, common, "List collected compile files")
    _add_sub(sub, "validate", "validate", handlers, common, "Validate config, files and cases")

    p = _add_sub(sub, "schema", "schema", handlers, common, "Show JSON schemas")
    p.add_argument("name", nargs="?", default=None, help="Schema name")

    p = _add_sub(sub, "list", "list", handlers, common, "List planned jobs without running")
    add_selection_flags(p)

    _add_sub(sub, "doctor", "doctor", handlers, common, "Check A and B environment readiness")

    # UVM scaffolding & style
    p = _add_sub(sub, "new", "new", handlers, common, "Generate UVM component skeleton")
    p.add_argument("kind", choices=("agent", "env", "test", "seq", "vseq"))
    p.add_argument("name")
    p.add_argument("--env", dest="env_name", default=None)
    p.add_argument("--agent", dest="agent_name", default=None)
    p.add_argument("--agents", default=None, help="Comma-separated agent list for env")

    uvm_grp = sub.add_parser("uvm", parents=[common], help="UVM sync/diff commands")
    uvm_sub = uvm_grp.add_subparsers(dest="uvm_command", required=True)
    p = _add_sub(uvm_sub, "sync", "uvm sync", handlers, common, "Sync UVM files with tbspec.py")
    p.add_argument("--env", dest="env_name", default=None)
    p.add_argument("--agent", dest="agent_name", default=None)
    p.add_argument("--dry-run", action="store_true")
    p = _add_sub(uvm_sub, "diff", "uvm diff", handlers, common, "Preview uvm sync diff")
    p.add_argument("--env", dest="env_name", default=None)
    p.add_argument("--agent", dest="agent_name", default=None)

    _add_sub(sub, "lint-style", "lint-style", handlers, common, "Lint UVM style rules")

    # Generation, build, execution
    p = _add_sub(sub, "gen", "gen", handlers, common, "Generate build keys, filelists, run.mk")
    add_selection_flags(p)

    p = _add_sub(sub, "build", "build", handlers, common, "Compile selected build modes")
    p.add_argument("--case", action="append", default=[])
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--exclude-tag", action="append", default=[])
    p.add_argument("--stage", default=None)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--seeds", default=None)
    p.add_argument("--shard", default=None)
    p.add_argument("--shard-seed", default=None)
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--order", choices=("name", "duration"), default="name")
    p.add_argument("--build", dest="build_modes", action="append", default=[])
    p.add_argument("--rebuild", action="store_true")
    p.add_argument("-j", "--jobs", type=int, default=None)
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true")

    for cmd_name in ("run", "regress"):
        msg = "Run selected cases" if cmd_name == "run" else "Run full regression"
        p = _add_sub(sub, cmd_name, cmd_name, handlers, common, msg)
        add_selection_flags(p)
        add_exec_flags(p)

    p = _add_sub(sub, "rerun", "rerun", handlers, common, "Rerun jobs preserving seeds")
    p.add_argument("--failed", action="store_true")
    p.add_argument("--status", action="append", default=[])
    p.add_argument("--run", dest="run_id", default=None)
    p.add_argument("--note", dest="note_ref", default=None)
    add_exec_flags(p)

    p = _add_sub(sub, "check", "check", handlers, common, "Run fast compile checks")
    p.add_argument("--slang-only", action="store_true")
    p.add_argument("--no-faketop", action="store_true")
    p.add_argument("--where", choices=("local", "remote"), default=None)
    p.add_argument("--format", dest="fmt", choices=("text", "gcc", "json"), default="text")
    p.add_argument("--incremental", action="store_true")

    # Monitoring, logs, post-run
    p = _add_sub(sub, "status", "status", handlers, common, "Show run progress")
    p.add_argument("--run", dest="run_id", default=None)
    p.add_argument("--watch", action="store_true")

    p = _add_sub(sub, "log", "log", handlers, common, "Read job or build log")
    p.add_argument("job_id", nargs="?", default=None)
    p.add_argument("--run", dest="run_id", default=None)
    p.add_argument("--try", dest="try_num", default=None)
    p.add_argument("--tail", type=int, default=None)
    p.add_argument("--lines", default=None, help="Line range A-B")
    p.add_argument("--around", type=int, default=None)
    p.add_argument("--context", type=int, default=30)
    p.add_argument("--grep", default=None)
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--build", dest="build_mode", default=None)

    p = _add_sub(sub, "explain", "explain", handlers, common, "Explain a job outcome")
    p.add_argument("job_id")
    p.add_argument("--run", dest="run_id", default=None)
    p.add_argument("--try", dest="try_num", type=int, default=None)

    p = _add_sub(sub, "wave", "wave", handlers, common, "Query waveform signal values")
    p.add_argument("job_id")
    p.add_argument("--signals", required=True)
    p.add_argument("--from", dest="time_from", default=None)
    p.add_argument("--to", dest="time_to", default=None)
    p.add_argument("--changes", action="store_true", default=True)
    p.add_argument("--all", dest="changes", action="store_false")
    p.add_argument("--run", dest="run_id", default=None)
    p.add_argument("--try", dest="try_num", default=None)

    p = _add_sub(sub, "stop", "stop", handlers, common, "Stop an active run on B")
    p.add_argument("--run", dest="run_id", default=None)

    p = _add_sub(sub, "judge", "judge", handlers, common, "Judge completed jobs on A")
    p.add_argument("--run", dest="run_id", default=None)
    p.add_argument("--allow-missing", action="store_true")
    p.add_argument("--partial", action="store_true")

    p = _add_sub(sub, "report", "report", handlers, common, "Render report from results.json")
    p.add_argument("--run", dest="run_id", default=None)
    p.add_argument(
        "--format",
        dest="formats",
        action="append",
        choices=("text", "json", "junit", "html"),
        default=[],
    )
    p.add_argument("--compare", default=None)
    p.add_argument(
        "--compare-mode",
        choices=("status", "judged"),
        default="status",
        help="Status field for --compare (default: status)",
    )
    p.add_argument("-o", "--output", dest="output_path", default=None)

    p = _add_sub(sub, "history", "history", handlers, common, "Show run or case history")
    p.add_argument("case", nargs="?", default=None)

    # Case records & triage
    p = _add_sub(sub, "notes", "notes", handlers, common, "Query bugs/todos/logs/notes")
    p.add_argument("--type", dest="types", action="append", default=[])
    p.add_argument("--status", dest="statuses", action="append", default=[])
    p.add_argument("--owner", default=None)
    p.add_argument("--since", default=None)
    p.add_argument("--until", default=None)
    p.add_argument("--overdue", action="store_true")
    p.add_argument("--next-id", default=None)
    add_selection_flags(p, include_full=False)

    bug_grp = sub.add_parser("bug", parents=[common], help="Manage Bug records")
    bug_sub = bug_grp.add_subparsers(dest="bug_command", required=True)
    p = _add_sub(bug_sub, "add", "bug add", handlers, common, "Add a Bug record")
    p.add_argument("case")
    p.add_argument("--title", required=True)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--run", dest="run_id", default=None)
    p.add_argument("--job", dest="job_id", default=None)
    p.add_argument("--owner", default=None)
    p.add_argument("--link", default=None)
    p.add_argument("--body", default="")
    p.add_argument("--author", default=None)
    p.add_argument("--id", dest="bug_id", type=int, default=None)

    p = _add_sub(bug_sub, "update", "bug update", handlers, common, "Update a Bug record")
    p.add_argument("ref")
    p.add_argument("--status", default=None)
    p.add_argument("--owner", default=None)
    p.add_argument("--link", default=None)
    p.add_argument("--append-body", default=None)
    p.add_argument("--author", default=None)

    note_grp = sub.add_parser("note", parents=[common], help="Manage Log/Note records")
    note_sub = note_grp.add_subparsers(dest="note_command", required=True)
    p = _add_sub(note_sub, "add", "note add", handlers, common, "Append Log or Note")
    p.add_argument("case")
    p.add_argument("--text", required=True)
    p.add_argument("--kind", choices=("log", "note"), default="log")
    p.add_argument("--date", dest="date_str", default=None)
    p.add_argument("--author", default=None)

    todo_grp = sub.add_parser("todo", parents=[common], help="Manage Todo records")
    todo_sub = todo_grp.add_subparsers(dest="todo_command", required=True)
    p = _add_sub(todo_sub, "add", "todo add", handlers, common, "Add a Todo record")
    p.add_argument("case")
    p.add_argument("--title", required=True)
    p.add_argument("--owner", default=None)
    p.add_argument("--due", default=None)
    p.add_argument("--author", default=None)
    p = _add_sub(todo_sub, "done", "todo done", handlers, common, "Mark a Todo completed")
    p.add_argument("ref")

    tri_grp = sub.add_parser("triage", parents=[common], help="AI triage workflow commands")
    tri_sub = tri_grp.add_subparsers(dest="triage_command", required=True)
    p = _add_sub(tri_sub, "plan", "triage plan", handlers, common, "Plan triage tasks")
    p.add_argument("--run", dest="run_id", default=None)
    p.add_argument("--pass-policy", choices=("none", "changed", "all"), default=None)
    p.add_argument("--group-by", choices=("signature", "case", "job"), default=None)

    p = _add_sub(tri_sub, "packet", "triage packet", handlers, common, "Render triage packet")
    p.add_argument("task")
    p.add_argument("--run", dest="run_id", default=None)

    p = _add_sub(tri_sub, "write", "triage write", handlers, common, "Write triage result")
    p.add_argument("task")
    p.add_argument("--run", dest="run_id", default=None)
    p.add_argument("--verdict", required=True)
    p.add_argument("--confidence", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--evidence", action="append", default=[], required=True)
    p.add_argument("--bug", dest="bugs", action="append", default=[])
    p.add_argument("--doc", required=True)
    p.add_argument("--author", default=None)

    p = _add_sub(tri_sub, "review", "triage review", handlers, common, "Review triage task")
    p.add_argument("task")
    p.add_argument("--run", dest="run_id", default=None)
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--accept", action="store_true")
    grp.add_argument("--reject", default=None)
    p.add_argument("--author", default="main")

    p = _add_sub(tri_sub, "status", "triage status", handlers, common, "Show triage review status")
    p.add_argument("--run", dest="run_id", default=None)

    p = _add_sub(sub, "doc", "doc", handlers, common, "Export markdown case documentation")
    p.add_argument("-o", "--output", dest="out_dir", default=None)
    p.add_argument("--lang", choices=("zh", "en"), default=None)
    p.add_argument("--with-results", action="store_true", default=True)
    p.add_argument("--no-results", dest="with_results", action="store_false")

    _add_sub(sub, "progress", "progress", handlers, common, "Show verification progress overview")

    cov_grp = sub.add_parser("cov", parents=[common], help="Coverage merge and reporting")
    cov_sub = cov_grp.add_subparsers(dest="cov_command", required=True)
    p = _add_sub(cov_sub, "merge", "cov merge", handlers, common, "Merge coverage databases")
    p.add_argument("--run", dest="run_id", default=None)
    p.add_argument("-o", "--output", dest="out_dir", default=None)
    p = _add_sub(cov_sub, "report", "cov report", handlers, common, "Generate coverage summary")
    p.add_argument("--run", dest="run_id", default=None)
    p.add_argument("--merged-dir", default=None)

    p = _add_sub(sub, "clean", "clean", handlers, common, "Clean generated runs and artifacts")
    p.add_argument("--run", dest="run_id", default=None)
    p.add_argument("--all", dest="all_files", action="store_true")
    p.add_argument("--locks", action="store_true")
    p.add_argument("--local-only", action="store_true")

    p = _add_sub(sub, "help", "help", handlers, common, "Read offline design topics")
    p.add_argument("topic", nargs="?", default=None)

    p = _add_sub(sub, "update", "update", handlers, common, "Update solouvm from a git repo")
    p.add_argument(
        "--repo",
        default=DEFAULT_UPDATE_REPO,
        help=f"Git repository URL (default: {DEFAULT_UPDATE_REPO})",
    )
    p.add_argument("--ref", default=None, help="Branch, tag, or commit to install")
    grp_mode = p.add_mutually_exclusive_group()
    grp_mode.add_argument("--user", dest="install_mode", action="store_const", const="user")
    grp_mode.add_argument("--system", dest="install_mode", action="store_const", const="system")
    p.set_defaults(install_mode=None)
    p.add_argument("--prefix", default=None, help="Custom installation prefix directory")
    p.add_argument("--python", dest="python_bin", default=None, help="Python >= 3.10 binary path")
    p.add_argument("--dry-run", action="store_true", help="Print planned git and install actions")

    p = _add_sub(
        sub, "completion", "completion", handlers, common, "Generate shell completion script"
    )
    p.add_argument("shell", choices=("bash", "zsh"), help="Target shell (bash or zsh)")

    pkg_grp = sub.add_parser("pkg", parents=[common], help="Manage external git/local packages")
    pkg_sub = pkg_grp.add_subparsers(dest="pkg_command", required=True)
    _add_sub(
        pkg_sub, "list", "pkg list", handlers, common, "List configured packages and local status"
    )
    p = _add_sub(
        pkg_sub,
        "sync",
        "pkg sync",
        handlers,
        common,
        "Clone missing git packages (skip existing)",
    )
    p.add_argument("names", nargs="*", help="Optional package names to sync")
    p = _add_sub(
        pkg_sub,
        "update",
        "pkg update",
        handlers,
        common,
        "Re-fetch git packages from upstream",
    )
    p.add_argument("names", nargs="*", help="Optional package names to update")

    return parser
