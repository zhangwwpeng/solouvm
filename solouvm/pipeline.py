"""Execution pipeline: gen, build, run/regress, rerun, status, stop, log, clean."""

from __future__ import annotations

import contextlib
import inspect
import math
import os
import re
import shlex
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm import __version__, output, ui
from solouvm.backends import fake_sim_source, get_backend
from solouvm.buildkey import compute_build_keys
from solouvm.collect import collect_cases
from solouvm.errors import BuildError, ConfigError, InfraError
from solouvm.gen import (
    render_launch,
    render_run_job,
    write_generated_project_files,
)
from solouvm.history import (
    capture_env,
    detect_sim_version,
    prune_history,
    resolve_run_id,
    run_dir,
    runs_dir,
    update_latest_link,
    write_env,
)
from solouvm.judge import judge_completed_attempt, judge_exit_code, judge_run, write_results
from solouvm.judge.facts import FactsError, parse_facts
from solouvm.model import Bug, Case, Facts, Job, Plan, Result
from solouvm.packages import collect_project_files, package_sync_files
from solouvm.plan import read_plan, write_plan
from solouvm.providers import get_provider
from solouvm.providers.transport_local import LocalTransport
from solouvm.report.json import PASS_STATUSES, read_results_json
from solouvm.report.text import render_text
from solouvm.units import parse_duration

if TYPE_CHECKING:
    from solouvm.backends.base import Backend
    from solouvm.collect.files import FileList
    from solouvm.config.schema import Config
    from solouvm.providers.base import Provider, Transport

__all__ = [
    "build",
    "clean_project",
    "gen",
    "make_run_id",
    "read_job_log",
    "regress",
    "rerun",
    "run",
    "run_regression",
    "status_run",
    "stop_run",
]

_SOLOUVM_VERSION = __version__
_SAFE_RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z", re.ASCII)
_REGEX_LIST_FIELDS = ("error_regex", "license_regex")


def _backend_for(config: Config) -> Backend:
    overrides = {
        k: (
            shlex.join(v)
            if isinstance(v, list)
            and k not in _REGEX_LIST_FIELDS
            and all(isinstance(x, str) for x in v)
            else v
        )
        for k, v in config.sim.items()
    }
    return get_backend(config.simulator, overrides)


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def make_run_id(
    root: Path | None = None,
    *,
    now: datetime | None = None,
    reserve: bool = False,
) -> str:
    """Create a unique run_id, honoring ``SOLOUVM_RUN_ID`` and optionally reserving its dir."""
    override = os.environ.get("SOLOUVM_RUN_ID", "").strip()
    if override:
        if override == "latest" or not _SAFE_RUN_ID_RE.fullmatch(override):
            raise ConfigError(
                f"invalid SOLOUVM_RUN_ID {override!r}",
                code="CFG_INVALID",
                hint="use letters, digits, '.', '_' or '-', and never 'latest'",
            )
        if root is not None:
            rdir = runs_dir(Path(root))
            target = rdir / override
            exists = target.exists()
            if reserve and not exists:
                rdir.mkdir(parents=True, exist_ok=True)
                try:
                    target.mkdir(exist_ok=False)
                except FileExistsError:
                    exists = True
            if exists:
                raise ConfigError(
                    f"run_id {override!r} from SOLOUVM_RUN_ID already exists",
                    code="CFG_INVALID",
                    hint=(
                        f"unset SOLOUVM_RUN_ID, choose a new ID, or remove it first with "
                        f"'solouvm clean --run {override}'"
                    ),
                )
        return override

    stamp = (now or datetime.now().astimezone()).strftime("%Y%m%d-%H%M%S")
    if root is not None:
        rdir = runs_dir(Path(root))
        if reserve:
            rdir.mkdir(parents=True, exist_ok=True)
            for _ in range(64):
                candidate = f"{stamp}-{os.urandom(2).hex()}"
                try:
                    (rdir / candidate).mkdir(exist_ok=False)
                except FileExistsError:
                    continue
                return candidate
            raise InfraError(
                "failed to reserve unique run directory after 64 attempts",
                code="PROVIDER_FAILED",
            )
        candidate = f"{stamp}-{os.urandom(2).hex()}"
        while (rdir / candidate).exists():
            candidate = f"{stamp}-{os.urandom(2).hex()}"
        return candidate
    return f"{stamp}-{os.urandom(2).hex()}"


def _stage_bin_scripts(root: Path, version: str = _SOLOUVM_VERSION) -> None:
    """Write ``gen/bin`` scripts early so ``detect_sim_version('fake')`` is deterministic."""
    scripts = (
        (".solouvm/gen/bin/run_job.sh", render_run_job(version)),
        (".solouvm/gen/bin/launch.sh", render_launch(version)),
        (".solouvm/gen/bin/fake_sim.sh", fake_sim_source()),
    )
    for rel, content in scripts:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        target.chmod(0o755)


def _apply_rebuild_salt(
    keys: dict[str, str],
    *,
    run_id: str,
    modes: Sequence[str],
) -> dict[str, str]:
    import hashlib

    out = dict(keys)
    for mode in modes:
        if mode in out:
            digest = hashlib.sha1(f"{out[mode]}:{run_id}:rebuild".encode()).hexdigest()[:12]
            out[mode] = digest
    return out


def gen(
    *,
    config: Config,
    plan: Plan,
    files: FileList,
    provider: Provider | None = None,
    sim_version: str | None = None,
    rebuild: bool = False,
    solouvm_version: str = _SOLOUVM_VERSION,
    allow_overwrite: bool = False,
) -> dict[str, object]:
    """Generate build keys, filelists, scripts, ``run.mk``, ``plan.json`` and ``env.json``."""
    root = Path(config.root)
    rpath = run_dir(root, plan.run_id)
    if not allow_overwrite and rpath.exists():
        has_artifacts = (
            (rpath / "results.json").exists()
            or (rpath / "triage").exists()
            or (rpath / "launch.pid").exists()
            or any(rpath.glob("*/try*"))
        )
        if has_artifacts:
            raise ConfigError(
                f"run directory {rpath} already contains execution artifacts",
                code="CFG_INVALID",
                hint=f"choose a new run_id or run 'solouvm clean --run {plan.run_id}' first",
            )
    backend = _backend_for(config)
    eff_provider = provider or get_provider(config, plan.provider)
    if provider is not None:
        plan.provider = eff_provider.name
    _stage_bin_scripts(root, solouvm_version)

    if sim_version is None and (
        type(eff_provider.transport) is LocalTransport or backend.name == "fake"
    ):
        sim_version = detect_sim_version(backend, eff_provider, root)

    used_modes = sorted({job.build for job in plan.jobs} or set(plan.builds))
    bk = compute_build_keys(
        config=config,
        files=files,
        backend=backend,
        modes=used_modes,
        run_id=plan.run_id,
        sim_version=sim_version,
        solouvm_version=solouvm_version,
        provider=eff_provider.name,
        provider_setup=eff_provider.setup,
    )
    build_keys = dict(bk.keys)
    if rebuild:
        build_keys = _apply_rebuild_salt(build_keys, run_id=plan.run_id, modes=used_modes)
    plan.builds = build_keys

    for mode, reasons in bk.non_reusable.items():
        if reasons:
            ui.warning(f"build '{mode}' will not be cached across runs ({reasons[0]})")

    manifest = write_generated_project_files(
        config=config,
        plan=plan,
        files=files,
        backend=backend,
        build_keys=build_keys,
        version=solouvm_version,
    )

    write_plan(rpath / "plan.json", plan)
    env_data = capture_env(
        root,
        run_id=plan.run_id,
        config=config,
        backend=backend,
        provider=eff_provider,
        solouvm_version=solouvm_version,
        sim_version=sim_version,
    )
    write_env(rpath, env_data)
    update_latest_link(root, plan.run_id)

    return {
        "run_id": plan.run_id,
        "builds": dict(plan.builds),
        "manifest": manifest,
        "non_reusable": bk.non_reusable,
    }


def _make_assigns(vars_map: dict[str, str]) -> list[str]:
    return [f"{k}={v}" for k, v in sorted(vars_map.items())]


def _first_build_error(text: str, backend: Backend) -> tuple[int | None, str]:
    patterns = [re.compile(p) for p in backend.error_regex]
    lines = text.splitlines()
    for lineno, line in enumerate(lines, 1):
        if any(p.search(line) for p in patterns) or "error:" in line.lower():
            start = max(1, lineno - 2)
            end = min(len(lines), lineno + 3)
            ctx = "\n".join(f"{n}: {lines[n - 1]}" for n in range(start, end + 1))
            return lineno, ctx
    tail = "\n".join(lines[-5:]) if lines else ""
    return None, tail


def _read_remote_or_local(provider: Provider, rel_path: str) -> str:
    if not provider.transport.exists(rel_path):
        return ""
    return "\n".join(text for _, text in provider.transport.read_lines(rel_path))


def _check_build_outputs(
    *,
    config: Config,
    plan: Plan,
    provider: Provider,
    backend: Backend,
    modes: Sequence[str],
) -> None:
    root = Path(config.root)
    for mode in modes:
        key = plan.builds.get(mode)
        if not key:
            continue
        rel_dir = f".solouvm/build/{mode}/{key}"
        if provider.transport.exists(f"{rel_dir}/.ok"):
            continue
        log_rel = f"{rel_dir}/build.log"
        log_text = _read_remote_or_local(provider, log_rel)
        err_line, ctx = _first_build_error(log_text, backend)
        detail = f"build failed for mode {mode!r} (key {key})"
        if ctx:
            detail += f":\n{ctx}"
        raise BuildError(
            detail,
            code="BUILD_FAILED",
            file=root / log_rel,
            line=err_line,
            hint=f"inspect build log: {log_rel}",
        )


def _push_generated(config: Config, provider: Provider, manifest: Sequence[str]) -> None:
    provider.sync.push(provider.transport, str(config.root), manifest)
    # Sync implementations like NoneSync skip file writes; ssh+none shares disk. Ensure ssh
    # without shared FS still gets generated artifacts when sync kind is neither git nor rsync
    # is covered by rsync/git transports; no extra copy needed for local/none.


def _gen_manifest(
    root: Path, run_id: str, builds: dict[str, str], config: Config | None = None
) -> list[str]:
    manifest = [
        ".solouvm/Makefile",
        ".solouvm/gen/bin/fake_sim.sh",
        ".solouvm/gen/bin/launch.sh",
        ".solouvm/gen/bin/run_job.sh",
        f".solouvm/runs/{run_id}/run.mk",
    ]
    for group_file in ("filelist.f", "filelist.rtl.f", "filelist.tb.f", "filelist.top.f"):
        rel = f".solouvm/gen/{group_file}"
        if (root / rel).is_file():
            manifest.append(rel)
    for mode, key in sorted(builds.items()):
        for group_file in ("filelist.f", "filelist.rtl.f", "filelist.tb.f", "filelist.top.f"):
            rel = f".solouvm/build/{mode}/{key}/{group_file}"
            if (root / rel).is_file():
                manifest.append(rel)
    if config is not None:
        manifest.extend(package_sync_files(root, config))
    return sorted(set(manifest))


def build(
    *,
    config: Config,
    plan: Plan,
    provider: Provider,
    files: FileList | None = None,
    rebuild: bool = False,
    modes: Sequence[str] | None = None,
    dry_run: bool = False,
    jobs: int | None = None,
    _skip_gen: bool = False,
) -> dict[str, object]:
    """Run ``gen -> push -> build`` for selected or active build modes."""
    root = Path(config.root)
    backend = _backend_for(config)
    selected_modes = list(modes) if modes else sorted(plan.builds or {j.build for j in plan.jobs})
    if rebuild and _skip_gen:
        _skip_gen = False

    if not _skip_gen:
        eff_files = files if files is not None else collect_project_files(config)
        gen_res = gen(
            config=config,
            plan=plan,
            files=eff_files,
            provider=provider,
            rebuild=rebuild,
        )
        manifest = list(gen_res["manifest"])  # type: ignore[arg-type]
    else:
        manifest = _gen_manifest(root, plan.run_id, plan.builds, config=config)

    for m in selected_modes:
        if m not in plan.builds:
            avail = ", ".join(sorted(plan.builds))
            raise ConfigError(
                f"build mode {m!r} is not part of this plan (available: {avail})",
                code="CFG_INVALID",
            )

    run_mk_rel = f".solouvm/runs/{plan.run_id}/run.mk"
    targets = [f"build/{m}" for m in selected_modes] if modes else ["build"]
    n_jobs = provider.launcher.jobs(jobs or config.regress.jobs)
    make_vars = _make_assigns(provider.launcher.make_vars(plan))
    make_cmd = ["make", "-f", run_mk_rel, "-k", f"-j{n_jobs}", *make_vars, *targets]

    if dry_run:
        return {
            "run_id": plan.run_id,
            "dry_run": True,
            "builds": dict(plan.builds),
            "files": manifest,
            "commands": [shlex.join(make_cmd)],
        }

    _push_generated(config, provider, manifest)
    rc, build_out = provider.transport.exec_output(make_cmd, cwd=".", check=False)
    if build_out.strip() and ui.verbosity() >= 1 and not ui.is_json_mode():
        ui.info(build_out.rstrip())
    _check_build_outputs(
        config=config,
        plan=plan,
        provider=provider,
        backend=backend,
        modes=selected_modes,
    )
    if rc != 0:
        raise BuildError(
            f"make build exited with code {rc}",
            code="BUILD_FAILED",
            hint=f"check logs under .solouvm/build/ for run {plan.run_id}",
        )
    return {
        "run_id": plan.run_id,
        "builds": dict(plan.builds),
        "modes": selected_modes,
        "ok": True,
    }


def _read_many_with_timeout(
    transport: Transport,
    pattern: str,
    *,
    timeout: float | None = None,
) -> dict[str, str]:
    if timeout is None:
        return transport.read_many(pattern)
    eff_timeout = timeout
    base_op_timeout = getattr(transport, "op_timeout", None)
    if (
        not isinstance(base_op_timeout, bool)
        and isinstance(base_op_timeout, (int, float))
        and base_op_timeout > 0
    ):
        eff_timeout = min(eff_timeout, float(base_op_timeout))
    try:
        sig = inspect.signature(transport.read_many)
    except (TypeError, ValueError):
        sig = None
    if sig is not None and (
        "timeout" in sig.parameters
        or any(p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
    ):
        return transport.read_many(pattern, timeout=eff_timeout)  # type: ignore[call-arg]
    return transport.read_many(pattern)


def _facts_info(facts: Facts) -> dict[str, object]:
    return {
        "try": facts.try_,
        "state": facts.state,
        "rc": facts.rc,
        "signal": facts.signal,
        "timed_out": facts.timed_out,
        "hung": facts.hung,
        "host": facts.host,
        "duration_s": facts.duration_s,
    }


def _parse_facts_objects(raw_map: dict[str, str], *, run_id: str) -> dict[str, Facts]:
    """Map ``job_id`` -> latest parsed ``Facts`` from ``read_many``."""
    per_job: dict[str, Facts] = {}
    for text in raw_map.values():
        try:
            facts = parse_facts(text)
        except FactsError:
            continue
        if facts.run_id != run_id or facts.kind != "job":
            continue
        prev = per_job.get(facts.job_id)
        if prev is None or facts.try_ >= prev.try_:
            per_job[facts.job_id] = facts
    return per_job


def _parse_facts_map(raw_map: dict[str, str], *, run_id: str) -> dict[str, dict[str, object]]:
    """Map ``job_id`` -> latest parsed status dict from ``read_many``."""
    return {
        jid: _facts_info(facts)
        for jid, facts in _parse_facts_objects(raw_map, run_id=run_id).items()
    }


def _live_summary(
    plan: Plan,
    facts_by_job: dict[str, dict[str, object]],
    *,
    judged_statuses: dict[tuple[str, int], str] | None = None,
) -> dict[str, object]:
    total = len(plan.jobs)
    done = 0
    running = 0
    ok_approx = 0
    fail_approx = 0
    for job in plan.jobs:
        info = facts_by_job.get(job.job_id)
        if info is None:
            continue
        if info["state"] == "running":
            running += 1
        else:
            done += 1
            try_num = int(info["try"])  # type: ignore[arg-type]
            judged = (
                judged_statuses.get((job.job_id, try_num))
                if judged_statuses is not None
                else None
            )
            if judged is not None:
                is_pass = judged in PASS_STATUSES
            else:
                is_pass = (
                    info["rc"] == 0
                    and not info["timed_out"]
                    and not info["hung"]
                    and not info["signal"]
                )
            if is_pass:
                ok_approx += 1
            else:
                fail_approx += 1
    queued = max(0, total - done - running)
    return {
        "total": total,
        "done": done,
        "passed_approx": ok_approx,
        "failed_approx": fail_approx,
        "running": running,
        "queued": queued,
    }


def _launch_make(
    *,
    config: Config,
    plan: Plan,
    provider: Provider,
    jobs: int,
    targets: Sequence[str],
    try_num: int | str = 1,
    wave: bool = False,
    detach: bool = False,
    on_progress: Callable[[dict[str, object]], None] | None = None,
    max_fail: int | None = None,
    budget_s: float | None = None,
    deadline: float | None = None,
    budget_label: float | None = None,
    verbose: int = 0,
) -> bool:
    """Launch ``launch.sh`` running ``make``; return True if stopped early by guard."""
    eff_deadline = (
        deadline
        if deadline is not None
        else ((time.monotonic() + budget_s) if budget_s is not None else None)
    )
    eff_budget_label = budget_label if budget_label is not None else budget_s
    budget_desc = f"{eff_budget_label:g}s" if eff_budget_label is not None else "time"

    run_mk_rel = f".solouvm/runs/{plan.run_id}/run.mk"
    n_jobs = provider.launcher.jobs(jobs)
    assigns: dict[str, str] = {
        "TRY": str(try_num),
        "WAVE": "1" if wave else "0",
    }
    assigns.update(provider.launcher.make_vars(plan))
    make_cmd = ["make", "-f", run_mk_rel, "-k", f"-j{n_jobs}", *_make_assigns(assigns), *targets]
    launch_cmd = ["bash", ".solouvm/gen/bin/launch.sh", "--run-id", plan.run_id]
    if detach:
        if eff_deadline is not None:
            remaining = eff_deadline - time.monotonic()
            if remaining <= 0:
                ui.warning(
                    f"regression budget ({budget_desc}) exceeded; stopping run {plan.run_id}"
                )
                return True
            launch_cmd.extend(["--budget", f"{remaining:g}"])
        launch_cmd.append("--detach")
        launch_cmd.extend(["--", *make_cmd])
        provider.transport.exec(launch_cmd, cwd=".", check=True)
        return False

    if eff_deadline is not None and time.monotonic() >= eff_deadline:
        ui.warning(f"regression budget ({budget_desc}) exceeded; stopping run {plan.run_id}")
        return True

    launch_cmd.extend(["--", *make_cmd])
    proc = provider.transport.spawn(launch_cmd, cwd=".")
    last_snapshot: tuple[int, int, int] | None = None
    printed_jobs: set[str] = set()
    stopped_early = False
    judged_cache: dict[tuple[str, int], str] = {}
    cases_by_name: dict[str, Case] | None = None
    jobs_by_id: dict[str, Job] | None = None
    backend: Backend | None = None
    if max_fail is not None:
        cases_by_name = {c.name: c for c in collect_cases(config)}
        jobs_by_id = {j.job_id: j for j in plan.jobs}
        backend = _backend_for(config)

    def _emit_finished_delta(facts_map: dict[str, dict[str, object]]) -> None:
        for job in plan.jobs:
            jid = job.job_id
            info = facts_map.get(jid)
            if info is None or info["state"] == "running":
                continue
            key = f"{jid}:{info['try']}"
            if key in printed_jobs:
                continue
            printed_jobs.add(key)
            if ui.is_json_mode():
                output.emit_event(
                    "job_done",
                    run_id=plan.run_id,
                    job_id=jid,
                    try_=info["try"],
                    rc=info["rc"],
                )
            elif not ui.is_tty() and not ui.is_quiet():
                ui.info(f"[{plan.run_id}] {jid} try{info['try']} finished (rc={info['rc']})")
            if verbose >= 1 and not ui.is_json_mode():
                log_rel = f"{job.dir}/try{info['try']}/sim.log"
                if provider.transport.exists(log_rel):
                    for _, line in provider.transport.read_lines(log_rel):
                        ui.info(f"  [{jid}] {line}")

    def _update_online_judgments(facts_objs: dict[str, Facts]) -> None:
        if cases_by_name is None or jobs_by_id is None:
            return
        for jid, facts_obj in facts_objs.items():
            if facts_obj.state == "running":
                continue
            cache_key = (jid, facts_obj.try_)
            if cache_key in judged_cache:
                continue
            if eff_deadline is not None and time.monotonic() >= eff_deadline:
                break
            job = jobs_by_id.get(jid)
            if job is None:
                continue
            case = cases_by_name.get(job.case) or Case(
                name=job.case,
                uvm_test=job.uvm_test,
                expect_fail=job.expect_fail,
                tags=job.tags,
            )
            verdict = judge_completed_attempt(
                config=config,
                plan=plan,
                provider=provider,
                job=job,
                case=case,
                facts=facts_obj,
                backend=backend,
            )
            judged_cache[cache_key] = verdict.status

    facts_pattern = f".solouvm/runs/{plan.run_id}/*/try*/job.facts"
    try:
        while proc.poll() is None:
            poll_timeout: float | None = None
            if eff_deadline is not None:
                remaining = eff_deadline - time.monotonic()
                if remaining <= 0:
                    ui.warning(
                        f"regression budget ({budget_desc}) exceeded; stopping run {plan.run_id}"
                    )
                    stopped_early = True
                    break
                poll_timeout = max(0.05, remaining)

            try:
                raw_facts = _read_many_with_timeout(
                    provider.transport,
                    facts_pattern,
                    timeout=poll_timeout,
                )
            except InfraError:
                if eff_deadline is not None and time.monotonic() >= eff_deadline:
                    ui.warning(
                        f"regression budget ({budget_desc}) exceeded; stopping run {plan.run_id}"
                    )
                    stopped_early = True
                    break
                raise

            facts_objs = _parse_facts_objects(raw_facts, run_id=plan.run_id)
            facts_map = {jid: _facts_info(facts_obj) for jid, facts_obj in facts_objs.items()}
            _emit_finished_delta(facts_map)
            if max_fail is not None:
                _update_online_judgments(facts_objs)
            snap = _live_summary(
                plan,
                facts_map,
                judged_statuses=judged_cache if max_fail is not None else None,
            )
            curr = (int(snap["done"]), int(snap["running"]), int(snap["queued"]))  # type: ignore[arg-type]
            if on_progress is not None and curr != last_snapshot:
                on_progress(snap)
            elif curr != last_snapshot and ui.is_json_mode():
                output.emit_event("progress", run_id=plan.run_id, **snap)
            last_snapshot = curr

            if max_fail is not None and int(snap["failed_approx"]) >= max_fail:  # type: ignore[arg-type]
                ui.warning(f"max_fail ({max_fail}) reached; stopping run {plan.run_id}")
                stopped_early = True
                break
            if eff_deadline is not None:
                remaining = eff_deadline - time.monotonic()
                if remaining <= 0:
                    ui.warning(
                        f"regression budget ({budget_desc}) exceeded; stopping run {plan.run_id}"
                    )
                    stopped_early = True
                    break
                time.sleep(min(0.25, remaining))
            else:
                time.sleep(0.25)

        if stopped_early:
            _stop_b_processes(config=config, provider=provider, run_id=plan.run_id)
            proc.terminate()
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                proc.wait(timeout=10)
        else:
            rc = proc.wait()
            raw_facts = provider.transport.read_many(facts_pattern)
            _emit_finished_delta(_parse_facts_map(raw_facts, run_id=plan.run_id))
            if rc not in (0, 2):
                # make -k returns 2 on build failure; non-2 indicates launcher failure
                raise InfraError(
                    f"launch.sh exited unexpectedly with code {rc}",
                    code="PROVIDER_FAILED",
                    hint=f"check .solouvm/runs/{plan.run_id}/launch.log",
                )
    except KeyboardInterrupt:
        _stop_b_processes(config=config, provider=provider, run_id=plan.run_id)
        with contextlib.suppress(OSError):
            proc.terminate()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            proc.wait(timeout=10)
        raise

    return stopped_early


def _stop_b_processes(*, config: Config, provider: Provider, run_id: str) -> None:
    """Terminate LSF jobs (if any) and the remote/local ``make`` process group."""
    with contextlib.suppress(Exception):
        provider.launcher.stop(provider.transport, run_id)

    pid_rel = f".solouvm/runs/{run_id}/launch.pid"
    with contextlib.suppress(Exception):
        if not provider.transport.exists(pid_rel):
            return
        content = _read_remote_or_local(provider, pid_rel).strip()
        parts = content.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return
        pgid = int(parts[1])
        killer = getattr(provider.transport, "stop_pgid", None)
        if callable(killer):
            killer(pgid)
        else:
            provider.transport.exec(["kill", "-TERM", f"-{pgid}"], cwd=".", check=False)


def _retry_candidates(
    results: Sequence[Result],
    plan: Plan,
    retry_on: Sequence[str],
    *,
    retry_override: int | None,
    attempt_num: int,
) -> list[Job]:
    allowed = set(retry_on)
    by_jid = {r.job_id: r for r in results}
    out: list[Job] = []
    for job in plan.jobs:
        res = by_jid.get(job.job_id)
        if res is None or res.status not in allowed:
            continue
        max_retry = retry_override if retry_override is not None else job.retry
        if attempt_num <= max_retry:
            out.append(job)
    return out


def _wave_candidates(
    results: Sequence[Result],
    plan: Plan,
    mode: bool | str,
) -> list[Job]:
    if not mode:
        return []
    failing = {r.job_id: r for r in results if r.status in ("fail", "timeout", "killed", "xpass")}
    by_jid = {j.job_id: j for j in plan.jobs}
    if mode == "all":
        return [by_jid[jid] for jid in failing if jid in by_jid]
    if mode == "per_signature":
        seen_sigs: set[str] = set()
        picked: list[Job] = []
        for r in results:
            if r.job_id not in failing or r.job_id not in by_jid:
                continue
            sig_id = str((r.signature or {}).get("id") or r.reason or r.job_id)
            if sig_id in seen_sigs:
                continue
            seen_sigs.add(sig_id)
            picked.append(by_jid[r.job_id])
        return picked
    return []


def _max_try_seen(res: Result) -> int:
    return max((int(att.get("try", 1)) for att in res.attempts), default=1)


def _run_wave_on_fail(
    *,
    config: Config,
    plan: Plan,
    provider: Provider,
    results: Sequence[Result],
    jobs_count: int,
    deadline: float | None = None,
    budget_label: float | None = None,
) -> bool:
    candidates = _wave_candidates(results, plan, config.regress.wave_on_fail)
    if not candidates:
        return False
    if deadline is not None and time.monotonic() >= deadline:
        budget_desc = f"{budget_label:g}s" if budget_label is not None else "time"
        ui.warning(
            f"regression budget ({budget_desc}) exceeded; skipping wave_on_fail for {plan.run_id}"
        )
        return True
    by_jid = {r.job_id: r for r in results}
    next_try = max(_max_try_seen(by_jid[j.job_id]) for j in candidates) + 1
    for job in candidates:
        provider.transport.put(f"{job.dir}/try{next_try}/.wave", b"1\n")
    targets = [f"run/{j.job_id}" for j in candidates]
    return _launch_make(
        config=config,
        plan=plan,
        provider=provider,
        jobs=jobs_count,
        targets=targets,
        try_num=next_try,
        wave=True,
        detach=False,
        deadline=deadline,
        budget_label=budget_label,
    )


def run_regression(
    *,
    config: Config,
    plan: Plan,
    provider: Provider,
    files: FileList,
    jobs: int | None = None,
    detach: bool = False,
    wave: bool = False,
    retry: int | None = None,
    max_fail: int | None = None,
    budget_s: float | None = None,
    dry_run: bool = False,
    rebuild: bool = False,
    allow_missing: bool = False,
    verbose: int = 0,
    on_progress: Callable[[dict[str, object]], None] | None = None,
) -> tuple[list[Result], dict[str, object] | None, int]:
    """Execute full ``gen -> push -> build -> run -> retry -> wave -> judge -> report``."""
    eff_max_fail = max_fail if max_fail is not None else config.regress.max_fail
    if eff_max_fail is not None and eff_max_fail <= 0:
        raise ConfigError(
            f"invalid max_fail {eff_max_fail!r}: must be >= 1",
            code="CFG_INVALID",
        )
    if budget_s is not None:
        eff_budget: float | None = budget_s
    elif config.regress.budget is not None:
        try:
            eff_budget = parse_duration(config.regress.budget)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"invalid regress.budget {config.regress.budget!r}: {exc}",
                code="CFG_INVALID",
            ) from None
    else:
        eff_budget = None
    if eff_budget is not None and (not math.isfinite(eff_budget) or eff_budget < 0):
        raise ConfigError(
            f"invalid budget {eff_budget!r}: must be a finite, non-negative number",
            code="CFG_INVALID",
        )

    reg_started = time.monotonic()
    deadline = (reg_started + eff_budget) if eff_budget is not None else None
    root = Path(config.root)
    gen_res = gen(
        config=config,
        plan=plan,
        files=files,
        provider=provider,
        rebuild=rebuild,
    )
    manifest = list(gen_res["manifest"])  # type: ignore[arg-type]

    n_jobs = provider.launcher.jobs(jobs or config.regress.jobs)
    assigns = {"TRY": "1", "WAVE": "1" if wave else "0", **provider.launcher.make_vars(plan)}
    make_cmd = [
        "make",
        "-f",
        f".solouvm/runs/{plan.run_id}/run.mk",
        "-k",
        f"-j{n_jobs}",
        *_make_assigns(assigns),
        "regress",
    ]
    if dry_run:
        dry_payload = {
            "run_id": plan.run_id,
            "dry_run": True,
            "builds": dict(plan.builds),
            "files": manifest,
            "commands": [shlex.join(make_cmd)],
        }
        return [], dry_payload, 0

    _push_generated(config, provider, manifest)

    if detach:
        _launch_make(
            config=config,
            plan=plan,
            provider=provider,
            jobs=n_jobs,
            targets=["regress"],
            try_num=1,
            wave=wave,
            detach=True,
            deadline=deadline,
            budget_label=eff_budget,
        )
        return [], None, 0

    try:
        stopped_early = _launch_make(
            config=config,
            plan=plan,
            provider=provider,
            jobs=n_jobs,
            targets=["regress"],
            try_num=1,
            wave=wave,
            detach=False,
            on_progress=on_progress,
            max_fail=eff_max_fail,
            deadline=deadline,
            budget_label=eff_budget,
            verbose=verbose,
        )
    except KeyboardInterrupt:
        partial_results = judge_run(
            config=config,
            plan=plan,
            provider=provider,
            partial=True,
            allow_missing=True,
        )
        payload = write_results(
            run_dir(root, plan.run_id),
            partial_results,
            run_id=plan.run_id,
            partial=True,
            judged_at=datetime.now().astimezone(),
            config=config,
        )
        return partial_results, payload, 130

    results = judge_run(
        config=config,
        plan=plan,
        provider=provider,
        allow_missing=allow_missing or stopped_early,
        partial=stopped_early,
    )

    if not stopped_early:
        attempt = 1
        while True:
            to_retry = _retry_candidates(
                results,
                plan,
                config.regress.retry_on,
                retry_override=retry,
                attempt_num=attempt,
            )
            if not to_retry:
                break
            if deadline is not None and time.monotonic() >= deadline:
                assert eff_budget is not None
                ui.warning(
                    f"regression budget ({eff_budget:g}s) exceeded; stopping run {plan.run_id}"
                )
                stopped_early = True
                break
            attempt += 1
            try:
                retry_stopped = _launch_make(
                    config=config,
                    plan=plan,
                    provider=provider,
                    jobs=n_jobs,
                    targets=[f"run/{j.job_id}" for j in to_retry],
                    try_num=attempt,
                    wave=wave,
                    detach=False,
                    deadline=deadline,
                    budget_label=eff_budget,
                    verbose=verbose,
                )
            except KeyboardInterrupt:
                partial_results = judge_run(
                    config=config,
                    plan=plan,
                    provider=provider,
                    partial=True,
                    allow_missing=True,
                )
                payload = write_results(
                    run_dir(root, plan.run_id),
                    partial_results,
                    run_id=plan.run_id,
                    partial=True,
                    judged_at=datetime.now().astimezone(),
                    config=config,
                )
                return partial_results, payload, 130
            if retry_stopped:
                stopped_early = True
            results = judge_run(
                config=config,
                plan=plan,
                provider=provider,
                allow_missing=allow_missing or stopped_early,
                partial=stopped_early,
            )
            if retry_stopped:
                break

        if not stopped_early and not wave and config.regress.wave_on_fail:
            wave_stopped = _run_wave_on_fail(
                config=config,
                plan=plan,
                provider=provider,
                results=results,
                jobs_count=n_jobs,
                deadline=deadline,
                budget_label=eff_budget,
            )
            if wave_stopped:
                stopped_early = True
            results = judge_run(
                config=config,
                plan=plan,
                provider=provider,
                allow_missing=allow_missing or stopped_early,
                partial=stopped_early,
            )

    payload = write_results(
        run_dir(root, plan.run_id),
        results,
        run_id=plan.run_id,
        partial=stopped_early,
        judged_at=datetime.now().astimezone(),
        config=config,
    )
    if not ui.is_json_mode():
        summary_text = render_text(payload)
        ui.summary(summary_text)
    return results, payload, judge_exit_code(results)


def run(
    *,
    config: Config,
    plan: Plan,
    provider: Provider,
    files: FileList | None = None,
    jobs: int | None = None,
    detach: bool = False,
    wave: bool = False,
    retry: int | None = None,
    max_fail: int | None = None,
    budget_s: float | None = None,
    dry_run: bool = False,
    rebuild: bool = False,
    verbose: int = 0,
) -> int:
    """Execute ``solouvm run`` and return its exit code (0/1/2/130)."""
    eff_files = files if files is not None else collect_project_files(config)
    _, _, exit_code = run_regression(
        config=config,
        plan=plan,
        provider=provider,
        files=eff_files,
        jobs=jobs,
        detach=detach,
        wave=wave,
        retry=retry,
        max_fail=max_fail,
        budget_s=budget_s,
        dry_run=dry_run,
        rebuild=rebuild,
        verbose=verbose,
    )
    return exit_code


def regress(
    *,
    config: Config,
    plan: Plan,
    provider: Provider,
    files: FileList | None = None,
    jobs: int | None = None,
    detach: bool = False,
    wave: bool = False,
    retry: int | None = None,
    max_fail: int | None = None,
    budget_s: float | None = None,
    dry_run: bool = False,
    rebuild: bool = False,
    verbose: int = 0,
) -> int:
    """Execute ``solouvm regress`` and return its exit code (0/1/2/130)."""
    return run(
        config=config,
        plan=plan,
        provider=provider,
        files=files,
        jobs=jobs,
        detach=detach,
        wave=wave,
        retry=retry,
        max_fail=max_fail,
        budget_s=budget_s,
        dry_run=dry_run,
        rebuild=rebuild,
        verbose=verbose,
    )


def _build_rerun_note_plan(
    *,
    config: Config,
    provider: Provider,
    new_run_id: str,
    note_ref: str,
) -> Plan:
    case_name, sep, ident = note_ref.partition("#")
    if not sep or not ident.isdigit():
        raise ConfigError(
            f"invalid --note reference {note_ref!r} (expected CASE#ID)",
            code="RECORD_INVALID",
        )
    bug_id = int(ident)
    cases = {c.name: c for c in collect_cases(config)}
    if case_name not in cases:
        raise ConfigError(f"case {case_name!r} not found", code="RECORD_INVALID")
    case = cases[case_name]
    bug = next((r for r in case.notes if isinstance(r, Bug) and r.id == bug_id), None)
    if bug is None:
        raise ConfigError(f"bug {note_ref!r} not found", code="RECORD_INVALID")
    if bug.seed is None:
        raise ConfigError(f"bug {note_ref!r} has no recorded seed", code="RECORD_INVALID")
    if case.build not in config.builds:
        raise ConfigError(f"unknown build {case.build!r}", code="CFG_INVALID")
    jid = f"{case.name}__{bug.seed}"
    build_cfg = config.builds[case.build]
    opts = [*config.run.opts, *build_cfg.run_opts, *case.opts]
    timeout_s = parse_duration(case.timeout if case.timeout is not None else config.run.timeout)
    hang_src = case.hang if case.hang is not None else config.run.hang
    hang_s = parse_duration(hang_src) if hang_src is not None else None
    new_jobs = [
        Job(
            job_id=jid,
            case=case.name,
            seed=bug.seed,
            build=case.build,
            uvm_test=case.uvm_test if case.uvm_test is not None else config.uvm_test,
            params=dict(case.params),
            opts=opts,
            env={**config.run.env, **case.env},
            timeout_s=timeout_s,
            hang_s=hang_s,
            retry=case.retry if case.retry is not None else config.regress.retry,
            expect_fail=case.expect_fail,
            tags=case.tags,
            dir=f".solouvm/runs/{new_run_id}/{jid}",
        )
    ]
    selection = {"note": note_ref, "case": [case.name], "seed": bug.seed}
    return Plan(
        run_id=new_run_id,
        created_at=datetime.now().astimezone(),
        parent_run=None,
        provider=provider.name,
        selection=selection,
        builds={m: m for m in dict.fromkeys(j.build for j in new_jobs)},
        jobs=new_jobs,
    )


def _build_rerun_from_run(
    *,
    root: Path,
    provider: Provider,
    new_run_id: str,
    run_id: str | None,
    failed: bool,
    statuses: Sequence[str],
) -> Plan:
    actual_base = resolve_run_id(root, run_id)
    base_dir = run_dir(root, actual_base)
    base_plan = read_plan(base_dir / "plan.json")
    res_path = base_dir / "results.json"
    if not res_path.is_file():
        raise ConfigError(
            f"results.json not found for run {actual_base!r}",
            code="RUN_NOT_FOUND",
            hint=f"run 'solouvm judge --run {actual_base}' first",
        )
    results_data = read_results_json(res_path)
    by_jid = {
        str(entry["job_id"]): str(entry["status"])
        for entry in results_data.get("jobs", [])  # type: ignore[union-attr]
        if isinstance(entry, dict) and "job_id" in entry
    }
    status_filter = set(statuses)
    new_jobs: list[Job] = []
    for old_job in base_plan.jobs:
        old_status = by_jid.get(old_job.job_id, "error")
        if status_filter:
            keep = old_status in status_filter
        elif failed:
            keep = old_status not in PASS_STATUSES
        else:
            keep = True
        if keep:
            new_jobs.append(
                Job(
                    job_id=old_job.job_id,
                    case=old_job.case,
                    seed=old_job.seed,
                    build=old_job.build,
                    uvm_test=old_job.uvm_test,
                    params=dict(old_job.params),
                    opts=list(old_job.opts),
                    env=dict(old_job.env),
                    timeout_s=old_job.timeout_s,
                    hang_s=old_job.hang_s,
                    retry=old_job.retry,
                    expect_fail=old_job.expect_fail,
                    tags=old_job.tags,
                    dir=f".solouvm/runs/{new_run_id}/{old_job.job_id}",
                )
            )
    if not new_jobs:
        raise ConfigError(
            f"no matching jobs to rerun from {actual_base!r}",
            code="SELECT_EMPTY",
            hint="try --status <status> or check previous results",
        )
    selection = {
        "rerun_of": actual_base,
        "failed": bool(failed and not status_filter),
        "statuses": sorted(status_filter),
    }
    return Plan(
        run_id=new_run_id,
        created_at=datetime.now().astimezone(),
        parent_run=actual_base,
        provider=provider.name,
        selection=selection,
        builds={m: m for m in dict.fromkeys(j.build for j in new_jobs)},
        jobs=new_jobs,
    )


def rerun(
    *,
    config: Config,
    provider: Provider,
    run_id: str | None = None,
    failed: bool = True,
    statuses: Sequence[str] = (),
    note_ref: str | None = None,
    wave: bool = False,
    retry: int | None = None,
    max_fail: int | None = None,
    budget_s: float | None = None,
    jobs: int | None = None,
    detach: bool = False,
    dry_run: bool = False,
    rebuild: bool = False,
    verbose: int = 0,
) -> tuple[Plan, list[Result], dict[str, object] | None, int]:
    """Build a seed-preserving rerun plan (or ``--note CASE#ID``) and run it (R-02, M-12)."""
    root = Path(config.root)
    new_run_id = make_run_id(root, reserve=True)
    try:
        if note_ref is not None:
            new_plan = _build_rerun_note_plan(
                config=config,
                provider=provider,
                new_run_id=new_run_id,
                note_ref=note_ref,
            )
        else:
            new_plan = _build_rerun_from_run(
                root=root,
                provider=provider,
                new_run_id=new_run_id,
                run_id=run_id,
                failed=failed,
                statuses=statuses,
            )
        files = collect_project_files(config)
        results, payload, code = run_regression(
            config=config,
            plan=new_plan,
            provider=provider,
            files=files,
            jobs=jobs,
            detach=detach,
            wave=wave,
            retry=retry,
            max_fail=max_fail,
            budget_s=budget_s,
            dry_run=dry_run,
            rebuild=rebuild,
            verbose=verbose,
        )
    except BaseException:
        with contextlib.suppress(OSError):
            run_dir(root, new_run_id).rmdir()
        raise
    return new_plan, results, payload, code


def _launch_pid_alive(provider: Provider, run_id: str) -> bool:
    pid_rel = f".solouvm/runs/{run_id}/launch.pid"
    if not provider.transport.exists(pid_rel):
        return False
    text = _read_remote_or_local(provider, pid_rel).strip()
    parts = text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return False
    _host, pgid = parts[0], int(parts[1])
    if type(provider.transport) is LocalTransport:
        try:
            os.killpg(pgid, 0)
        except OSError:
            return False
        else:
            return True
    rc = provider.transport.exec(["kill", "-0", f"-{pgid}"], cwd=".", check=False)
    return rc == 0


def status_run(
    *,
    config: Config,
    provider: Provider,
    run_id: str | None = None,
    watch: bool = False,
    poll_s: float = 0.5,
) -> dict[str, object]:
    """Inspect active or completed progress for ``run_id`` (Z-04, H-13)."""
    root = Path(config.root)
    actual_run_id = resolve_run_id(root, run_id)
    plan = read_plan(run_dir(root, actual_run_id) / "plan.json")

    def _snapshot() -> dict[str, object]:
        raw_facts = provider.transport.read_many(f".solouvm/runs/{actual_run_id}/*/try*/job.facts")
        facts_map = _parse_facts_map(raw_facts, run_id=actual_run_id)
        summary = _live_summary(plan, facts_map)
        alive = _launch_pid_alive(provider, actual_run_id)
        running_flag = alive or int(summary["running"]) > 0  # type: ignore[arg-type]
        return {
            "run_id": actual_run_id,
            "active": running_flag,
            "summary": summary,
            "jobs": [
                {
                    "job_id": j.job_id,
                    "case": j.case,
                    "seed": j.seed,
                    **(facts_map.get(j.job_id, {"state": "queued", "try": 0, "rc": None})),
                }
                for j in plan.jobs
            ],
        }

    snap = _snapshot()
    while watch and snap["active"]:
        if ui.is_json_mode():
            output.emit_event("status", **snap)
        elif not ui.is_quiet():
            s = snap["summary"]
            ui.info(
                f"[{actual_run_id}] {s['done']}/{s['total']} done  "  # type: ignore[index]
                f"running {s['running']}  queued {s['queued']}"  # type: ignore[index]
            )
        time.sleep(poll_s)
        snap = _snapshot()
    return snap


def _provider_for_plan(config: Config, default_provider: Provider, rdir: Path) -> Provider:
    plan_path = rdir / "plan.json"
    if not plan_path.is_file():
        return default_provider
    try:
        plan = read_plan(plan_path)
    except ConfigError:
        return default_provider
    if plan.provider and plan.provider != default_provider.name:
        try:
            return get_provider(config, plan.provider)
        except ConfigError:
            return default_provider
    return default_provider


def _active_runs(
    config: Config,
    provider: Provider,
    *,
    use_plan_provider: bool = False,
) -> list[str]:
    rdir = runs_dir(Path(config.root))
    if not rdir.is_dir():
        return []
    active: list[str] = []
    for entry in sorted(rdir.iterdir()):
        if not entry.is_dir() or entry.is_symlink() or not (entry / "plan.json").is_file():
            continue
        rid = entry.name
        eff_prov = _provider_for_plan(config, provider, entry) if use_plan_provider else provider
        raw_facts = eff_prov.transport.read_many(f".solouvm/runs/{rid}/*/try*/job.facts")
        facts_map = _parse_facts_map(raw_facts, run_id=rid)
        any_running = any(info["state"] == "running" for info in facts_map.values())
        if any_running or _launch_pid_alive(eff_prov, rid):
            active.append(rid)
    return active


def stop_run(
    *,
    config: Config,
    provider: Provider,
    run_id: str | None = None,
    judge_after: bool = True,
    use_plan_provider: bool = False,
) -> dict[str, object]:
    """Terminate an active regression on B and judge finished jobs (Q3)."""
    root = Path(config.root)
    if run_id is None:
        running = _active_runs(config, provider, use_plan_provider=use_plan_provider)
        if not running:
            raise ConfigError("no active run found to stop", code="RUN_NOT_FOUND")
        if len(running) > 1:
            raise ConfigError(
                f"multiple runs are active ({', '.join(running)}); specify --run RUN_ID",
                code="CFG_INVALID",
                hint=f"pass --run {running[-1]}",
            )
        actual_run_id = running[0]
    else:
        actual_run_id = resolve_run_id(root, run_id)

    eff_provider = (
        _provider_for_plan(config, provider, run_dir(root, actual_run_id))
        if use_plan_provider
        else provider
    )
    _stop_b_processes(config=config, provider=eff_provider, run_id=actual_run_id)

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and _launch_pid_alive(eff_provider, actual_run_id):
        time.sleep(0.1)

    payload: dict[str, object] | None = None
    if judge_after and (run_dir(root, actual_run_id) / "plan.json").is_file():
        plan = read_plan(run_dir(root, actual_run_id) / "plan.json")
        results = judge_run(
            config=config,
            plan=plan,
            provider=eff_provider,
            partial=True,
            allow_missing=True,
        )
        payload = write_results(
            run_dir(root, actual_run_id),
            results,
            run_id=actual_run_id,
            partial=True,
            judged_at=datetime.now().astimezone(),
            config=config,
        )

    return {
        "run_id": actual_run_id,
        "stopped": True,
        "judged": payload is not None,
    }


def read_job_log(
    *,
    config: Config,
    provider: Provider,
    job_id: str | None = None,
    run_id: str | None = None,
    try_num: str | None = None,
    tail: int | None = None,
    lines_range: tuple[int, int] | None = None,
    around: int | None = None,
    context: int = 30,
    grep: str | None = None,
    follow: bool = False,
    build_mode: str | None = None,
    limit: int = 200,
) -> dict[str, object]:
    """Read a job or build log via ``provider.transport`` (Z-05, AI-14)."""
    root = Path(config.root)
    actual_run_id = resolve_run_id(root, run_id)
    plan = read_plan(run_dir(root, actual_run_id) / "plan.json")

    if build_mode is not None:
        if build_mode not in plan.builds:
            raise ConfigError(
                f"build mode {build_mode!r} not used in run {actual_run_id!r}",
                code="JOB_NOT_FOUND",
            )
        rel_log = f".solouvm/build/{build_mode}/{plan.builds[build_mode]}/build.log"
        target_label = f"build/{build_mode}"
        facts_rel: str | None = f".solouvm/build/{build_mode}/{plan.builds[build_mode]}/job.facts"
    else:
        if not job_id:
            raise ConfigError("missing JOB_ID (or pass --build MODE)", code="JOB_NOT_FOUND")
        job = next((j for j in plan.jobs if j.job_id == job_id), None)
        if job is None:
            raise ConfigError(
                f"job {job_id!r} not found in run {actual_run_id!r}",
                code="JOB_NOT_FOUND",
            )
        target_label = job_id
        if try_num is not None:
            try_dirname = try_num if try_num.startswith("try") else f"try{try_num}"
        else:
            raw = provider.transport.read_many(f"{job.dir}/try*/job.facts")
            facts_map = _parse_facts_map(raw, run_id=actual_run_id)
            if job_id in facts_map:
                try_dirname = f"try{facts_map[job_id]['try']}"
            else:
                local_dirs = sorted(
                    [p.name for p in (root / job.dir).glob("try*") if p.is_dir()],
                    key=lambda s: (0, int(s[3:])) if s[3:].isdigit() else (1, s),
                )
                try_dirname = local_dirs[-1] if local_dirs else "try1"
        rel_log = f"{job.dir}/{try_dirname}/sim.log"
        facts_rel = f"{job.dir}/{try_dirname}/job.facts"

    if not provider.transport.exists(rel_log):
        raise ConfigError(f"log file not found: {rel_log}", code="JOB_NOT_FOUND")

    line_window: tuple[int, int] | None = lines_range
    if around is not None:
        line_window = (max(1, around - context), around + context)

    grep_re = re.compile(grep) if grep else None
    matched: list[tuple[int, str]] = []
    for lineno, text in provider.transport.read_lines(rel_log, lines=line_window):
        if grep_re is not None and not grep_re.search(text):
            continue
        matched.append((lineno, text))

    if follow:
        last_line = matched[-1][0] if matched else 0
        for _, text in matched:
            if ui.is_json_mode():
                output.emit_event("log_line", job_id=target_label, text=text)
            else:
                ui.info(text)
        while facts_rel and provider.transport.exists(facts_rel):
            facts_txt = _read_remote_or_local(provider, facts_rel)
            try:
                still_running = parse_facts(facts_txt).state == "running"
            except FactsError:
                still_running = False
            for lineno, text in provider.transport.read_lines(rel_log):
                if lineno <= last_line:
                    continue
                last_line = lineno
                if grep_re is not None and not grep_re.search(text):
                    continue
                matched.append((lineno, text))
                if ui.is_json_mode():
                    output.emit_event("log_line", job_id=target_label, line=lineno, text=text)
                else:
                    ui.info(text)
            if not still_running:
                break
            time.sleep(0.2)

    if tail is not None and tail >= 0:
        matched = matched[-tail:] if tail > 0 else []

    rows = [{"line": lineno, "text": text} for lineno, text in matched]
    more_cmd = f"solouvm log {target_label} --run {actual_run_id} --limit 0"
    shown, truncated = output.truncate_items(rows, limit, more=more_cmd)
    return {
        "run_id": actual_run_id,
        "job_id": target_label,
        "path": rel_log,
        "lines": shown,
        "total": len(rows),
        "truncated": truncated,
    }


def _rm_path(provider: Provider, root: Path, rel_path: str, *, local_only: bool) -> None:
    target = root / rel_path
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    if not local_only and type(provider.transport) is not LocalTransport:
        rm_fn = getattr(provider.transport, "rm_tree", None)
        if callable(rm_fn):
            rm_fn(rel_path)
        else:
            provider.transport.exec(["rm", "-rf", rel_path], cwd=".", check=False)


def clean_project(
    *,
    config: Config,
    provider: Provider,
    run_id: str | None = None,
    all_files: bool = False,
    locks: bool = False,
    local_only: bool = False,
) -> dict[str, object]:
    """Clean ``.solouvm`` artifacts on A and (unless ``local_only``) B (D23, Y-06)."""
    root = Path(config.root)
    if run_id is not None and run_id.strip() == "latest":
        raise ConfigError(
            "clean --run requires an explicit run_id, not 'latest'",
            code="CFG_INVALID",
            hint="run 'solouvm history' to find the exact run_id",
        )

    removed: list[str] = []
    if all_files:
        if (root / ".solouvm").exists():
            _rm_path(provider, root, ".solouvm", local_only=local_only)
            removed.append(".solouvm")
        return {"removed": removed, "mode": "all", "local_only": local_only}

    if locks:
        for lock_path in sorted((root / ".solouvm").glob("**/*.lock")):
            rel = lock_path.relative_to(root).as_posix()
            _rm_path(provider, root, rel, local_only=local_only)
            removed.append(rel)
        return {"removed": removed, "mode": "locks", "local_only": local_only}

    if run_id is not None:
        actual = resolve_run_id(root, run_id)
        rel = f".solouvm/runs/{actual}"
        _rm_path(provider, root, rel, local_only=local_only)
        removed.append(rel)
        latest_link = runs_dir(root) / "latest"
        if latest_link.is_symlink() and os.readlink(latest_link) == actual:
            latest_link.unlink()
        return {"removed": removed, "mode": "run", "run_id": actual, "local_only": local_only}

    pruned = prune_history(root, keep=config.history.keep)
    removed_runs = list(pruned.get("removed_runs", []))
    removed_builds = list(pruned.get("removed_builds", []))
    if not local_only and type(provider.transport) is not LocalTransport:
        for rel in [*removed_runs, *removed_builds]:
            _rm_path(provider, root, rel, local_only=False)
    return {
        "removed": [*removed_runs, *removed_builds],
        "removed_runs": removed_runs,
        "removed_builds": removed_builds,
        "kept_runs": pruned.get("kept_runs", []),
        "mode": "prune",
        "local_only": local_only,
    }
