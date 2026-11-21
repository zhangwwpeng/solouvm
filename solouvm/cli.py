"""Thin CLI entrypoint dispatching all ``solouvm`` subcommands (cli.md)."""

from __future__ import annotations

import argparse
import contextlib
import json
import shlex
import sys
from dataclasses import asdict
from pathlib import Path

from solouvm import __version__, output, pipeline, ui
from solouvm.backends import get_backend
from solouvm.backends.base import Backend
from solouvm.check import format_check_result, run_check
from solouvm.cli_parser import Handler, build_parser
from solouvm.collect import collect_cases, collect_files, load_filelist
from solouvm.config import Config, find_config, load_config
from solouvm.cov import cov_merge, cov_report
from solouvm.errors import ConfigError, SolouvmError
from solouvm.explain import explain, explain_data
from solouvm.history import case_history, list_history, resolve_run_id, run_dir
from solouvm.init import init_project
from solouvm.judge import judge_exit_code, judge_run, write_results
from solouvm.notes import (
    bug_add,
    bug_update,
    compute_progress,
    export_docs,
    format_notes_table,
    format_progress,
    next_bug_id,
    note_add,
    query_notes,
    todo_add,
    todo_done,
)
from solouvm.ops import (
    format_doctor,
    format_show_config,
    get_schema,
    run_doctor,
    show_config,
    show_help,
    validate_project,
)
from solouvm.plan import Plan, build_plan, read_plan
from solouvm.providers import Provider, get_provider
from solouvm.report import render_report, write_reports
from solouvm.report.json import compare_results, read_results_json
from solouvm.report.text import render_text
from solouvm.roles import check_role
from solouvm.triage import (
    plan_tasks,
    render_packet,
    review_task,
    triage_status,
    write_result,
)
from solouvm.units import parse_duration
from solouvm.update import run_update
from solouvm.uvmgen import create_component, lint_style, uvm_diff, uvm_sync
from solouvm.wave import format_wave_table, query_wave

__all__ = ["VERSION", "main"]

VERSION = __version__
_DEFAULT_LIMIT = 50


def _load_ctx(args: argparse.Namespace) -> tuple[Config, Provider]:
    start = Path(args.config) if args.config else Path.cwd()
    conf_path = find_config(start, search_parents=(args.config is None))
    cli_overrides: dict[str, object] = {}
    if getattr(args, "provider", None):
        cli_overrides["provider"] = args.provider
    config = load_config(conf_path, cli=cli_overrides or None)
    if args.verbose >= 2:
        ui.set_log_file(Path(config.root) / ".solouvm" / "solouvm.log")
    return config, get_provider(config)


def _backend_for(config: Config) -> Backend:
    overrides = {
        k: (
            shlex.join(v)
            if isinstance(v, list)
            and k not in ("error_regex", "license_regex")
            and all(isinstance(x, str) for x in v)
            else v
        )
        for k, v in config.sim.items()
    }
    return get_backend(config.simulator, overrides)


def _selection(args: argparse.Namespace) -> dict[str, object]:
    stage = [s.strip() for s in args.stage.split(",") if s.strip()] if args.stage else None
    seeds = (
        args.seeds if args.seeds is not None else ([args.seed] if args.seed is not None else None)
    )
    return {
        "case": list(args.case),
        "tag": list(args.tag),
        "exclude_tag": list(args.exclude_tag),
        "stage": stage,
        "build": getattr(args, "build", None),
        "seeds": seeds,
    }


def _limit(args: argparse.Namespace, default: int = _DEFAULT_LIMIT) -> int:
    return default if args.limit is None else args.limit


def _lines_range(raw: str | None) -> tuple[int, int] | None:
    if raw is None:
        return None
    a, sep, b = raw.partition("-")
    if not sep or not a.isdigit() or not b.isdigit():
        raise ConfigError(f"invalid --lines {raw!r} (expected START-END)", code="CFG_INVALID")
    return int(a), int(b)


def _parse_budget_arg(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return parse_duration(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid --budget {raw!r}: {exc}", code="CFG_INVALID") from None


def _validate_max_fail(value: int | None) -> int | None:
    if value is not None and value <= 0:
        raise ConfigError(
            f"invalid --max-fail {value!r}: must be >= 1",
            code="CFG_INVALID",
        )
    return value


def _resolve_run_provider(
    config: Config,
    default_provider: Provider,
    args: argparse.Namespace,
    actual_run: str,
) -> tuple[Provider, Plan]:
    plan = read_plan(run_dir(config.root, actual_run) / "plan.json")
    if getattr(args, "provider", None):
        return default_provider, plan
    if plan.provider and plan.provider != default_provider.name:
        return get_provider(config, plan.provider), plan
    return default_provider, plan


def _cmd_init(args: argparse.Namespace) -> int:
    created = init_project(
        root=Path.cwd(),
        simulator=args.simulator,
        bare=args.bare,
        agents=args.agents,
    )
    if ui.is_json_mode():
        output.emit_json(
            "init",
            {"simulator": args.simulator, "bare": args.bare, "created": created},
            next_=["solouvm validate", "solouvm list", "solouvm regress"],
        )
        return 0
    ui.summary(f"initialized {args.simulator} project ({len(created)} files)")
    output.emit_next(["solouvm validate", "solouvm regress"])
    return 0


def _cmd_config(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    entries = show_config(config, key=args.key)
    shown, trunc = output.truncate_items(entries, _limit(args), more="solouvm config --limit 0")
    if ui.is_json_mode():
        output.emit_json("config", {"entries": shown, "total": len(entries)}, truncated=trunc)
        return 0
    ui.summary(format_show_config(shown))
    return 0


def _cmd_files(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    flist = collect_files(config.root, load_filelist(config.root))
    rows = [
        [e.group, e.path + (" [OUTSIDE ROOT]" if e.outside_root else ""), e.source]
        for e in flist.entries
    ]
    shown, trunc = output.truncate_items(
        flist.entries, _limit(args), more="solouvm files --limit 0"
    )
    if ui.is_json_mode():
        data = {
            "entries": [
                {
                    "group": e.group,
                    "path": e.path,
                    "source": e.source,
                    "outside_root": e.outside_root,
                }
                for e in shown
            ],
            "total": len(flist.entries),
        }
        output.emit_json("files", data, truncated=trunc)
        return 0
    ui.table(["GROUP", "PATH", "SOURCE"], rows, limit=_limit(args))
    ui.summary(f"{len(flist.entries)} files")
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    target = Path(args.config) if args.config else Path.cwd()
    report = validate_project(target)
    exit_code = 0 if report.ok else 2
    if ui.is_json_mode():
        data = {
            "valid": report.ok,
            "error_count": len(report.errors),
            "warning_count": len(report.warnings),
            "issues": [issue.to_dict() for issue in report.issues],
        }
        output.emit_json("validate", data, next_=["solouvm list"] if report.ok else [])
        return exit_code
    ui.summary(report.format_text())
    return exit_code


def _cmd_schema(args: argparse.Namespace) -> int:
    data = get_schema(args.name)
    if ui.is_json_mode():
        output.emit_json("schema", data)
    else:
        print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    config, _provider = _load_ctx(args)
    cases = collect_cases(config)
    warns: list[str] = []
    plan = build_plan(
        cases=cases,
        config=config,
        selection=_selection(args),
        run_id="preview",
        allow_empty=args.allow_empty,
        force=True,
        order=args.order,
        shard=args.shard,
        on_warning=warns.append,
        rng_seed=getattr(args, "shard_seed", None),
    )
    for w in warns:
        ui.warning(w)
    cmap = {c.name: c for c in cases}
    shown, trunc = output.truncate_items(plan.jobs, _limit(args), more="solouvm list --limit 0")
    if ui.is_json_mode():
        data = {
            "total_jobs": len(plan.jobs),
            "total_cases": len({j.case for j in plan.jobs}),
            "jobs": [
                {
                    "job_id": j.job_id,
                    "case": j.case,
                    "seed": j.seed,
                    "build": j.build,
                    "tags": list(j.tags),
                    "stage": cmap[j.case].stage if j.case in cmap else "wip",
                }
                for j in shown
            ],
        }
        output.emit_json("list", data, truncated=trunc, next_=["solouvm gen", "solouvm regress"])
        return 0
    rows = [
        [j.job_id, j.case, str(j.seed), j.build, ",".join(j.tags), cmap[j.case].stage]
        for j in shown
    ]
    ui.table(["JOB", "CASE", "SEED", "BUILD", "TAGS", "STAGE"], rows, limit=0)
    if trunc:
        ui.info(f"... omitted {trunc['omitted']}; use {trunc['more']}")
    ui.summary(f"{len(plan.jobs)} jobs from {len({j.case for j in plan.jobs})} cases")
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    checks, ok = run_doctor(config, provider)
    code = 0 if ok else 2
    if ui.is_json_mode():
        output.emit_json("doctor", {"ok": ok, "checks": [c.to_dict() for c in checks]})
        return code
    ui.summary(format_doctor(checks))
    return code


def _cmd_new(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    agents = [a.strip() for a in args.agents.split(",") if a.strip()] if args.agents else None
    tdir = Path(config.uvm.templates) if config.uvm.templates else None
    if tdir and not tdir.is_absolute():
        tdir = Path(config.root) / tdir
    created = create_component(
        config.root,
        args.kind,
        args.name,
        env_name=args.env_name,
        agent_name=args.agent_name,
        agents=agents,
        templates_dir=tdir,
    )
    rels = [p.relative_to(config.root).as_posix() for p in created]
    if ui.is_json_mode():
        output.emit_json("new", {"kind": args.kind, "name": args.name, "files": rels})
        return 0
    for r in rels:
        ui.info(f"created {r}")
    ui.summary(f"generated {args.kind} {args.name!r} ({len(rels)} files)")
    return 0


def _cmd_uvm_sync(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    rep = uvm_sync(config.root, env=args.env_name, agent=args.agent_name, dry_run=args.dry_run)
    data = {
        "dry_run": rep.dry_run,
        "updated": [p.as_posix() for p in rep.updated],
        "created": [p.as_posix() for p in rep.created],
        "orphans": [p.as_posix() for p in rep.orphans],
        "modified_outside": [p.as_posix() for p in rep.modified_outside],
        "extra_files": [p.as_posix() for p in rep.extra_files],
        "diff": rep.diff,
    }
    if ui.is_json_mode():
        output.emit_json("uvm sync", data)
        return 0
    if args.dry_run and rep.diff:
        ui.info(rep.diff)
    ui.summary(
        f"uvm sync: {len(rep.updated)} updated, {len(rep.created)} created, "
        f"{len(rep.orphans)} orphan, {len(rep.modified_outside)} outside edits"
    )
    return 0


def _cmd_uvm_diff(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    diff_text = uvm_diff(config.root, env=args.env_name, agent=args.agent_name)
    if ui.is_json_mode():
        output.emit_json("uvm diff", {"diff": diff_text})
        return 0
    ui.summary(diff_text or "(no changes)")
    return 0


def _cmd_lint_style(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    flist = collect_files(config.root, load_filelist(config.root))
    issues = lint_style(config.root, files=flist)
    has_err = any(i.severity == "error" for i in issues)
    code = 1 if issues else 0
    if ui.is_json_mode():
        output.emit_json(
            "lint-style",
            {
                "ok": not issues,
                "issues": [
                    {"file": i.file, "line": i.line, "severity": i.severity, "message": i.message}
                    for i in issues
                ],
            },
        )
        return code
    for issue in issues:
        ui.info(issue.format())
    ui.summary(f"lint-style: {len(issues)} issue(s)" + (" (errors)" if has_err else ""))
    return code


def _plan_for_exec(config: Config, provider: Provider, args: argparse.Namespace) -> Plan:
    cases = collect_cases(config)
    warns: list[str] = []
    run_id = pipeline.make_run_id(config.root, reserve=True)
    try:
        plan = build_plan(
            cases=cases,
            config=config,
            selection=_selection(args),
            run_id=run_id,
            allow_empty=getattr(args, "allow_empty", False),
            force=getattr(args, "force", False),
            order=getattr(args, "order", "name"),
            shard=getattr(args, "shard", None),
            on_warning=warns.append,
            rng_seed=getattr(args, "shard_seed", None),
        )
    except BaseException:
        with contextlib.suppress(OSError):
            run_dir(config.root, run_id).rmdir()
        raise
    plan.provider = provider.name
    for w in warns:
        ui.warning(w)
    return plan


def _cmd_gen(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    plan = _plan_for_exec(config, provider, args)
    flist = collect_files(config.root, load_filelist(config.root))
    res = pipeline.gen(config=config, plan=plan, files=flist, provider=provider)
    if ui.is_json_mode():
        output.emit_json("gen", res, next_=["solouvm build", "solouvm regress"])
        return 0
    ui.summary(f"generated run {plan.run_id} ({len(plan.jobs)} jobs)")
    output.emit_next(["make build", "make regress"])
    return 0


def _cmd_build(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    plan = _plan_for_exec(config, provider, args)
    flist = collect_files(config.root, load_filelist(config.root))
    res = pipeline.build(
        config=config,
        plan=plan,
        provider=provider,
        files=flist,
        rebuild=args.rebuild,
        modes=args.build_modes or None,
        dry_run=args.dry_run,
        jobs=args.jobs,
    )
    if ui.is_json_mode():
        output.emit_json("build", res)
        return 0
    if args.dry_run:
        ui.summary("\n".join(str(c) for c in res.get("commands", [])))
    else:
        ui.summary(f"built {len(res['modes'])} mode(s) for run {plan.run_id}")
    return 0


def _run_or_regress(args: argparse.Namespace, *, require_selector: bool) -> int:
    if require_selector and not (args.case or args.tag or args.stage):
        raise ConfigError(
            "solouvm run requires --case, --tag, or --stage (use 'solouvm regress' for all)",
            code="CFG_INVALID",
            hint="pass --case NAME or use 'solouvm regress'",
        )
    config, provider = _load_ctx(args)
    budget_s = _parse_budget_arg(getattr(args, "budget", None))
    max_fail = _validate_max_fail(getattr(args, "max_fail", None))
    plan = _plan_for_exec(config, provider, args)
    flist = collect_files(config.root, load_filelist(config.root))
    _results, payload, code = pipeline.run_regression(
        config=config,
        plan=plan,
        provider=provider,
        files=flist,
        jobs=args.jobs,
        detach=args.detach,
        wave=args.wave,
        retry=args.retry,
        max_fail=max_fail,
        budget_s=budget_s,
        dry_run=args.dry_run,
        rebuild=args.rebuild,
        verbose=args.verbose,
    )
    cmd_label = "run" if require_selector else "regress"
    if ui.is_json_mode():
        if args.dry_run:
            output.emit_json(cmd_label, payload)
        elif args.detach:
            output.emit_json(cmd_label, {"run_id": plan.run_id, "detached": True})
        else:
            output.emit_json(
                cmd_label,
                payload,
                next_=[f"solouvm triage plan --run {plan.run_id}"] if code == 1 else [],
            )
    elif args.dry_run and payload is not None:
        ui.summary("\n".join(str(c) for c in payload.get("commands", [])))
    elif args.detach:
        ui.summary(f"started detached run {plan.run_id}")
        output.emit_next([f"solouvm status --run {plan.run_id}"])
    return code


def _cmd_rerun(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    budget_s = _parse_budget_arg(getattr(args, "budget", None))
    max_fail = _validate_max_fail(getattr(args, "max_fail", None))
    failed = bool(args.failed or (not args.status and args.note_ref is None))
    new_plan, _results, payload, code = pipeline.rerun(
        config=config,
        provider=provider,
        run_id=args.run_id,
        failed=failed,
        statuses=args.status,
        note_ref=args.note_ref,
        wave=args.wave,
        retry=args.retry,
        max_fail=max_fail,
        budget_s=budget_s,
        jobs=args.jobs,
        detach=args.detach,
        dry_run=args.dry_run,
        rebuild=args.rebuild,
        verbose=args.verbose,
    )
    if ui.is_json_mode():
        data = {"run_id": new_plan.run_id, "detached": True} if args.detach else payload
        output.emit_json("rerun", data)
    elif args.dry_run and payload is not None:
        ui.summary("\n".join(str(c) for c in payload.get("commands", [])))
    elif args.detach:
        ui.summary(f"started rerun {new_plan.run_id}")
    return code


def _cmd_check(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    flist = collect_files(config.root, load_filelist(config.root))
    res, code = run_check(
        config=config,
        files=flist,
        backend=_backend_for(config),
        provider=provider,
        slang_only=args.slang_only,
        no_faketop=args.no_faketop,
        where=args.where,
        fmt=args.fmt,
        incremental_rtl=args.incremental,
    )
    if ui.is_json_mode():
        payload = json.loads(format_check_result(res, fmt="json"))
        output.emit_json("check", payload)
        return code
    if args.fmt == "json":
        print(format_check_result(res, fmt="json"))
    else:
        ui.summary(format_check_result(res, fmt=args.fmt))
    return code


def _cmd_status(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    actual_run = resolve_run_id(config.root, args.run_id)
    provider, _ = _resolve_run_provider(config, provider, args, actual_run)
    snap = pipeline.status_run(
        config=config,
        provider=provider,
        run_id=actual_run,
        watch=args.watch,
    )
    if ui.is_json_mode():
        output.emit_json("status", snap)
        return 0
    s = snap["summary"]
    ui.summary(
        f"run {snap['run_id']}: {s['done']}/{s['total']} done  "
        f"running {s['running']}  queued {s['queued']}  active={snap['active']}"
    )
    return 0


def _cmd_log(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    actual_run = resolve_run_id(config.root, args.run_id)
    provider, _ = _resolve_run_provider(config, provider, args, actual_run)
    data = pipeline.read_job_log(
        config=config,
        provider=provider,
        job_id=args.job_id,
        run_id=actual_run,
        try_num=args.try_num,
        tail=args.tail,
        lines_range=_lines_range(args.lines),
        around=args.around,
        context=args.context,
        grep=args.grep,
        follow=args.follow,
        build_mode=args.build_mode,
        limit=_limit(args, 200),
    )
    if ui.is_json_mode():
        output.emit_json("log", data, truncated=data["truncated"])
        return 0
    if not args.follow:
        for row in data["lines"]:
            ui.info(f"{row['line']}: {row['text']}")
        if data["truncated"]:
            t = data["truncated"]
            ui.info(f"... omitted {t['omitted']}; use {t['more']}")
    return 0


def _cmd_explain(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    actual_run = resolve_run_id(config.root, args.run_id)
    provider, plan = _resolve_run_provider(config, provider, args, actual_run)
    if ui.is_json_mode():
        data = explain_data(
            config=config,
            plan=plan,
            run_id=actual_run,
            job_id=args.job_id,
            try_num=args.try_num,
            provider=provider,
        )
        output.emit_json("explain", data)
        return 0
    md = explain(
        config=config,
        plan=plan,
        run_id=actual_run,
        job_id=args.job_id,
        try_num=args.try_num,
        provider=provider,
    )
    print(md)
    return 0


def _cmd_wave(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    actual_run = resolve_run_id(config.root, args.run_id)
    provider, _ = _resolve_run_provider(config, provider, args, actual_run)
    signals = [s.strip() for s in args.signals.split(",") if s.strip()]
    data = query_wave(
        config=config,
        run_id=actual_run,
        job_id=args.job_id,
        provider=provider,
        signals=signals,
        time_from=args.time_from,
        time_to=args.time_to,
        changes_only=args.changes,
        limit=_limit(args, 200),
        try_num=args.try_num,
    )
    if ui.is_json_mode():
        output.emit_json("wave", data)
        return 0
    print(format_wave_table(data))
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    use_plan_provider = not bool(getattr(args, "provider", None))
    target_run = args.run_id
    if target_run is not None:
        actual_run = resolve_run_id(config.root, target_run)
        provider, _ = _resolve_run_provider(config, provider, args, actual_run)
        target_run = actual_run
        use_plan_provider = False
    res = pipeline.stop_run(
        config=config,
        provider=provider,
        run_id=target_run,
        use_plan_provider=use_plan_provider,
    )
    if ui.is_json_mode():
        output.emit_json("stop", res)
    else:
        ui.summary(f"stopped run {res['run_id']}")
    return 0


def _cmd_judge(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    actual_run = resolve_run_id(config.root, args.run_id)
    rdir = run_dir(config.root, actual_run)
    provider, plan = _resolve_run_provider(config, provider, args, actual_run)
    results = judge_run(
        config=config,
        plan=plan,
        provider=provider,
        allow_missing=args.allow_missing,
        partial=args.partial,
    )
    payload = write_results(
        rdir,
        results,
        run_id=actual_run,
        partial=args.partial,
        judged_at=__import__("datetime").datetime.now().astimezone(),
        config=config,
    )
    code = judge_exit_code(results)
    if ui.is_json_mode():
        output.emit_json("judge", payload)
    else:
        ui.summary(render_text(payload))
    return code


def _cmd_report(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    actual_run = resolve_run_id(config.root, args.run_id)
    rdir = run_dir(config.root, actual_run)
    payload = read_results_json(rdir / "results.json")
    compare_payload = (
        read_results_json(
            run_dir(config.root, resolve_run_id(config.root, args.compare)) / "results.json"
        )
        if args.compare
        else None
    )
    compare_data = (
        compare_results(payload, compare_payload, mode=getattr(args, "compare_mode", "status"))
        if compare_payload is not None
        else None
    )
    formats = args.formats or ["text"]
    if args.output_path:
        out_p = Path(args.output_path)
        write_reports(
            out_p if out_p.is_dir() else out_p.parent,
            payload,
            formats=formats,
            project=config.project,
            compare=compare_data,
            output_path=None if out_p.is_dir() else out_p,
        )
        if ui.is_json_mode():
            output.emit_json(
                "report", {"run_id": actual_run, "formats": formats, "output": str(out_p)}
            )
        else:
            ui.summary(f"wrote report(s) for {actual_run} to {out_p}")
        return 0
    if ui.is_json_mode():
        json_data = {**payload, "compare": compare_data} if compare_data is not None else payload
        output.emit_json("report", json_data)
        return 0
    for fmt in formats:
        print(render_report(payload, fmt=fmt, project=config.project, compare=compare_data))
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    if args.case:
        entries = case_history(config.root, args.case)
        shown, trunc = output.truncate_items(
            entries, _limit(args), more=f"solouvm history {args.case} --limit 0"
        )
        if ui.is_json_mode():
            output.emit_json("history", {"case": args.case, "entries": shown}, truncated=trunc)
            return 0
        rows = [[str(e["run_id"]), str(e["status"]), str(e["duration_s"])] for e in shown]
        ui.table(["RUN_ID", "STATUS", "DURATION"], rows, limit=0)
        return 0
    entries = list_history(config.root)
    shown, trunc = output.truncate_items(entries, _limit(args), more="solouvm history --limit 0")
    if ui.is_json_mode():
        output.emit_json("history", {"runs": shown}, truncated=trunc)
        return 0
    rows = [
        [
            str(e["run_id"]),
            str(e["created_at"]),
            f"{e['passed']}/{e['total']}",
            str(e["commit"] or "-"),
        ]
        for e in shown
    ]
    ui.table(["RUN_ID", "CREATED", "PASSED", "COMMIT"], rows, limit=0)
    return 0


def _cmd_notes(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    if args.next_id:
        nid = next_bug_id(collect_cases(config), args.next_id)
        if ui.is_json_mode():
            output.emit_json("notes", {"case": args.next_id, "next_id": nid})
        else:
            print(nid)
        return 0
    filters: dict[str, object] = {
        "types": args.types,
        "statuses": args.statuses,
        "owner": args.owner,
        "since": args.since,
        "until": args.until,
        "overdue": args.overdue,
        "case": args.case,
        "tag": args.tag,
        "exclude_tag": args.exclude_tag,
    }
    res = query_notes(config=config, filters=filters)
    rows = res["rows"]
    assert isinstance(rows, list)
    shown_rows, trunc = output.truncate_items(rows, _limit(args), more="solouvm notes --limit 0")
    shown_res = {**res, "rows": shown_rows}
    if ui.is_json_mode():
        output.emit_json("notes", shown_res, truncated=trunc)
        return 0
    print(format_notes_table(shown_res))
    if trunc:
        ui.info(f"... omitted {trunc['omitted']}; use {trunc['more']}")
    return 0


def _cmd_bug_add(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    bug = bug_add(
        config=config,
        case_name=args.case,
        title=args.title,
        seed=args.seed,
        run_id=args.run_id,
        job_id=args.job_id,
        owner=args.owner,
        link=args.link,
        body=args.body,
        author=args.author,
        bug_id=args.bug_id,
        role=args.role,
    )
    ref = f"{args.case}#{bug.id}"
    if ui.is_json_mode():
        output.emit_json("bug add", {"case": args.case, "ref": ref, "bug": asdict(bug)})
        return 0
    ui.summary(f"added bug {ref}: {bug.title}")
    return 0


def _cmd_bug_update(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    bug = bug_update(
        config=config,
        ref=args.ref,
        status=args.status,
        owner=args.owner,
        link=args.link,
        append_body=args.append_body,
        author=args.author,
        role=args.role,
    )
    if ui.is_json_mode():
        output.emit_json("bug update", {"ref": args.ref, "bug": asdict(bug)})
        return 0
    ui.summary(f"updated bug {args.ref} (status={bug.status})")
    return 0


def _cmd_note_add(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    rec = note_add(
        config=config,
        case_name=args.case,
        text=args.text,
        kind=args.kind,
        date_str=args.date_str,
        author=args.author,
        role=args.role,
    )
    if ui.is_json_mode():
        output.emit_json(
            "note add",
            {"case": args.case, "kind": args.kind, "record": asdict(rec)},
        )
        return 0
    ui.summary(f"added {args.kind} to {args.case}")
    return 0


def _cmd_todo_add(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    todo = todo_add(
        config=config,
        case_name=args.case,
        title=args.title,
        owner=args.owner,
        due=args.due,
        author=args.author,
        role=args.role,
    )
    ref = f"{args.case}#{todo.id}"
    if ui.is_json_mode():
        output.emit_json("todo add", {"case": args.case, "ref": ref, "todo": asdict(todo)})
        return 0
    ui.summary(f"added todo {ref}: {todo.title}")
    return 0


def _cmd_todo_done(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    todo = todo_done(config=config, ref=args.ref, role=args.role)
    if ui.is_json_mode():
        output.emit_json("todo done", {"ref": args.ref, "todo": asdict(todo)})
        return 0
    ui.summary(f"completed todo {args.ref}")
    return 0


def _cmd_triage_plan(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    actual_run = resolve_run_id(config.root, args.run_id)
    tasks = plan_tasks(
        config=config,
        run_id=actual_run,
        pass_policy=args.pass_policy,
        group_by=args.group_by,
    )
    shown, trunc = output.truncate_items(
        tasks, _limit(args), more=f"solouvm triage plan --run {actual_run} --limit 0"
    )
    next_cmds = [f"solouvm triage packet {tasks[0]['id']} --run {actual_run}"] if tasks else []
    if ui.is_json_mode():
        output.emit_json(
            "triage plan",
            {"run_id": actual_run, "total": len(tasks), "tasks": shown},
            truncated=trunc,
            next_=next_cmds,
        )
        return 0
    if shown:
        rows = [
            [
                str(t["id"]),
                str(t["kind"]),
                ",".join(str(c) for c in (t.get("cases") or [])),
                str(t.get("reason") or ""),
            ]
            for t in shown
        ]
        ui.table(["TASK", "KIND", "CASES", "REASON"], rows, limit=0)
        if trunc:
            ui.info(f"... omitted {trunc['omitted']}; use {trunc['more']}")
    ui.summary(f"{len(tasks)} triage task(s) for run {actual_run}")
    output.emit_next(next_cmds)
    return 0


def _cmd_triage_packet(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    actual_run = resolve_run_id(config.root, args.run_id)
    provider, _ = _resolve_run_provider(config, provider, args, actual_run)
    text = render_packet(
        config=config,
        run_id=actual_run,
        task_id=args.task,
        limit=args.limit,
        provider=provider,
    )
    if ui.is_json_mode():
        output.emit_json("triage packet", {"run_id": actual_run, "task": args.task, "packet": text})
        return 0
    print(text, end="")
    return 0


def _read_triage_doc_arg(raw_doc: str) -> str:
    if raw_doc == "-" or "\n" in raw_doc or raw_doc.lstrip().startswith("#"):
        return raw_doc
    doc_path = Path(raw_doc)
    if not doc_path.is_file():
        raise ConfigError(f"debug doc file not found: {raw_doc}", code="TRIAGE_INVALID")
    return doc_path.read_text(encoding="utf-8")


def _cmd_triage_write(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    actual_run = resolve_run_id(config.root, args.run_id)
    doc_val = _read_triage_doc_arg(args.doc)
    task = write_result(
        config=config,
        run_id=actual_run,
        task_id=args.task,
        verdict=args.verdict,
        confidence=args.confidence,
        summary=args.summary,
        evidence=args.evidence,
        bugs=args.bugs,
        doc_text=doc_val,
        author=args.author,
        role=args.role,
    )
    if ui.is_json_mode():
        output.emit_json(
            "triage write",
            {"run_id": actual_run, "task": task},
            next_=[f"solouvm triage status --run {actual_run}"],
        )
        return 0
    ui.summary(f"wrote triage verdict {args.verdict} for {args.task}")
    return 0


def _cmd_triage_review(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    actual_run = resolve_run_id(config.root, args.run_id)
    task = review_task(
        config=config,
        run_id=actual_run,
        task_id=args.task,
        accept=args.accept,
        reason=args.reject,
        author=args.author,
        role=args.role,
    )
    decision = "accepted" if args.accept else "rejected"
    if ui.is_json_mode():
        output.emit_json(
            "triage review",
            {"run_id": actual_run, "decision": decision, "task": task},
        )
        return 0
    ui.summary(f"{decision} triage task {args.task}")
    return 0


def _cmd_triage_status(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    actual_run = resolve_run_id(config.root, args.run_id)
    tasks, code = triage_status(config=config, run_id=actual_run)
    pending = sum(1 for t in tasks if t.get("review") is None)
    stale = sum(1 for t in tasks if t.get("stale"))
    shown, trunc = output.truncate_items(
        tasks, _limit(args), more=f"solouvm triage status --run {actual_run} --limit 0"
    )
    if ui.is_json_mode():
        output.emit_json(
            "triage status",
            {
                "run_id": actual_run,
                "total": len(tasks),
                "pending": pending,
                "stale": stale,
                "tasks": shown,
            },
            truncated=trunc,
        )
        return code
    if shown:
        rows = []
        for t in shown:
            res = t.get("result")
            rev = t.get("review")
            verdict = res.get("verdict") if isinstance(res, dict) else "-"
            status_lbl = (
                rev.get("decision")
                if isinstance(rev, dict)
                else ("stale" if t.get("stale") else "pending")
            )
            rows.append([str(t["id"]), str(t["kind"]), str(verdict or "-"), str(status_lbl)])
        ui.table(["TASK", "KIND", "VERDICT", "STATUS"], rows, limit=0)
        if trunc:
            ui.info(f"... omitted {trunc['omitted']}; use {trunc['more']}")
    ui.summary(f"{len(tasks)} task(s): {pending} pending, {stale} stale")
    return code


def _cmd_doc(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    res = export_docs(
        config=config,
        out_dir=args.out_dir,
        lang=args.lang,
        with_results=args.with_results,
    )
    if ui.is_json_mode():
        output.emit_json("doc", res)
        return 0
    ui.summary(f"exported {res['case_count']} case doc(s) to {res['out_dir']}")
    return 0


def _cmd_progress(args: argparse.Namespace) -> int:
    config, _ = _load_ctx(args)
    data = compute_progress(config=config)
    if ui.is_json_mode():
        output.emit_json("progress", data)
        return 0
    print(format_progress(data))
    return 0


def _cmd_cov_merge(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    actual_run = resolve_run_id(config.root, args.run_id)
    provider, plan = _resolve_run_provider(config, provider, args, actual_run)
    res = cov_merge(config=config, plan=plan, provider=provider, out_dir=args.out_dir)
    if ui.is_json_mode():
        output.emit_json("cov merge", res)
        return 0
    ui.summary(f"merged coverage from {res['job_count']} job(s) to {res['merged_dir']}")
    return 0


def _cmd_cov_report(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    actual_run = resolve_run_id(config.root, args.run_id)
    provider, plan = _resolve_run_provider(config, provider, args, actual_run)
    res = cov_report(config=config, plan=plan, provider=provider, merged_dir=args.merged_dir)
    if ui.is_json_mode():
        output.emit_json("cov report", res)
        return 0
    ui.summary(str(res.get("output") or f"coverage report in {res['report_dir']}"))
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    config, provider = _load_ctx(args)
    if (
        args.run_id is not None
        and args.run_id.strip() != "latest"
        and not getattr(args, "provider", None)
    ):
        with contextlib.suppress(ConfigError):
            actual_run = resolve_run_id(config.root, args.run_id)
            provider, _ = _resolve_run_provider(config, provider, args, actual_run)
    res = pipeline.clean_project(
        config=config,
        provider=provider,
        run_id=args.run_id,
        all_files=args.all_files,
        locks=args.locks,
        local_only=args.local_only,
    )
    if ui.is_json_mode():
        output.emit_json("clean", res)
        return 0
    removed = res.get("removed") or []
    assert isinstance(removed, list)
    ui.summary(f"cleaned {len(removed)} item(s) ({res['mode']})")
    return 0


def _cmd_help(args: argparse.Namespace) -> int:
    root = Path(args.config) if args.config else Path.cwd()
    with contextlib.suppress(ConfigError):
        root = find_config(root, search_parents=(args.config is None)).parent
    data = show_help(args.topic, root=root)
    if ui.is_json_mode():
        output.emit_json("help", data)
        return 0
    if "content" in data:
        print(data["content"])
        return 0
    topics = data.get("topics") or []
    assert isinstance(topics, list)
    print("Available topics: " + ", ".join(str(t) for t in topics))
    print(data.get("usage", "solouvm help <topic>"))
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    res = run_update(
        repo=args.repo,
        ref=args.ref,
        mode=args.install_mode,
        prefix=args.prefix,
        python_bin=args.python_bin,
        dry_run=args.dry_run,
    )
    if ui.is_json_mode():
        output.emit_json("update", res)
        return 0
    if args.dry_run:
        cmds = res.get("commands") or []
        assert isinstance(cmds, list)
        ui.summary("\n".join(str(c) for c in cmds))
        return 0
    out_text = str(res.get("output") or "").strip()
    if out_text and not ui.is_quiet():
        print(out_text)
    ui.summary(f"updated solouvm to {res['version']} ({res['commit']}) at {res['bin_path']}")
    return 0


def _handlers() -> dict[str, Handler]:
    return {
        "init": _cmd_init,
        "config": _cmd_config,
        "files": _cmd_files,
        "validate": _cmd_validate,
        "schema": _cmd_schema,
        "list": _cmd_list,
        "doctor": _cmd_doctor,
        "new": _cmd_new,
        "uvm sync": _cmd_uvm_sync,
        "uvm diff": _cmd_uvm_diff,
        "lint-style": _cmd_lint_style,
        "gen": _cmd_gen,
        "build": _cmd_build,
        "run": lambda a: _run_or_regress(a, require_selector=True),
        "regress": lambda a: _run_or_regress(a, require_selector=False),
        "rerun": _cmd_rerun,
        "check": _cmd_check,
        "status": _cmd_status,
        "log": _cmd_log,
        "explain": _cmd_explain,
        "wave": _cmd_wave,
        "stop": _cmd_stop,
        "judge": _cmd_judge,
        "report": _cmd_report,
        "history": _cmd_history,
        "notes": _cmd_notes,
        "bug add": _cmd_bug_add,
        "bug update": _cmd_bug_update,
        "note add": _cmd_note_add,
        "todo add": _cmd_todo_add,
        "todo done": _cmd_todo_done,
        "triage plan": _cmd_triage_plan,
        "triage packet": _cmd_triage_packet,
        "triage write": _cmd_triage_write,
        "triage review": _cmd_triage_review,
        "triage status": _cmd_triage_status,
        "doc": _cmd_doc,
        "progress": _cmd_progress,
        "cov merge": _cmd_cov_merge,
        "cov report": _cmd_cov_report,
        "clean": _cmd_clean,
        "help": _cmd_help,
        "update": _cmd_update,
    }


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and dispatch the requested subcommand."""
    parser = build_parser(_handlers())
    args = parser.parse_args(argv)
    eff_limit = _DEFAULT_LIMIT if args.limit is None else args.limit
    ui.reset()
    ui.setup(
        json_mode=bool(args.json),
        quiet=bool(args.quiet),
        verbose=int(args.verbose),
        limit=max(eff_limit, 0),
    )
    canon = getattr(args, "canon_cmd", "")
    try:
        if eff_limit < 0:
            raise ConfigError(f"invalid --limit {eff_limit}: must be >= 0", code="CFG_INVALID")
        if args.version:
            check_role(args.role, "version")
            if ui.is_json_mode():
                output.emit_json("version", {"version": VERSION})
            else:
                print(f"solouvm {VERSION}")
            return 0
        func = getattr(args, "func", None)
        if func is None:
            parser.print_usage(sys.stderr)
            return 2
        check_role(args.role, canon)
        return int(func(args))
    except SolouvmError as exc:
        return output.fail(exc, command=canon or "solouvm")
    except KeyboardInterrupt:
        return 130
