"""Run history, environment capture, flaky/median queries and retention pruning (Y-01..Y-06)."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import platform
import re
import shlex
import shutil
import socket
import statistics
import subprocess
from collections import OrderedDict
from collections.abc import Callable
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.errors import ConfigError, InfraError, RunActiveError
from solouvm.plan import read_plan
from solouvm.units import parse_sim_time_ns

if TYPE_CHECKING:
    from solouvm.backends.base import Backend
    from solouvm.config.schema import Config
    from solouvm.providers.base import Provider

__all__ = [
    "ENV_SCHEMA",
    "RUNS_REL",
    "capture_env",
    "case_historical_medians",
    "case_history",
    "detect_sim_version",
    "job_flaky_in_history",
    "list_history",
    "prune_history",
    "record_run_stats",
    "resolve_run_id",
    "run_dir",
    "runs_dir",
    "update_latest_link",
    "write_env",
]

RUNS_REL = ".solouvm/runs"
ENV_SCHEMA = 1
_STATS_CACHE_REL = Path(".solouvm") / "cache" / "run_stats.json"
_STATS_SCHEMA = 1
_PENDING_MAX = 128

_TEST_DONE_RE = re.compile(r"@\s*([^:]*?)\s*:\s*(?:\S+\s+)?\[TEST_DONE\]")
_SIM_TIME_RE = re.compile(r"sim time:\s*(\d+(?:\.\d+)?)\s*(fs|ps|ns|us|ms|s)\b", re.IGNORECASE)
_SHELL_META_RE = re.compile(r"[;&|<>$`*?~()\[\]{}]")

_PENDING_DIFFS: OrderedDict[tuple[int, str], str] = OrderedDict()
_PENDING_DIFFS_BY_RUN: OrderedDict[str, str] = OrderedDict()


def runs_dir(root: Path) -> Path:
    """Return ``<root>/.solouvm/runs``."""
    return Path(root) / RUNS_REL


def run_dir(root: Path, run_id: str) -> Path:
    """Return ``<root>/.solouvm/runs/<run_id>``."""
    return runs_dir(root) / run_id


def _valid_run_id(run_id: str) -> bool:
    return (
        bool(run_id)
        and run_id not in (".", "..", "latest")
        and not run_id.startswith(".")
        and "/" not in run_id
        and "\\" not in run_id
    )


def _atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise InfraError(f"failed to write {path}: {exc}", code="PROVIDER_FAILED") from exc
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.astimezone()
    return dt


def _read_json_dict(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _run_timestamps(rdir: Path) -> tuple[float, str | None, dict[str, object] | None]:
    """Return ``(sort_epoch, created_at_str, results_dict)`` for a run directory."""
    plan_data = _read_json_dict(rdir / "plan.json")
    results_data = _read_json_dict(rdir / "results.json")

    created_raw = plan_data.get("created_at") if plan_data else None
    judged_raw = results_data.get("judged_at") if results_data else None
    created_dt = _parse_iso(created_raw)
    judged_dt = _parse_iso(judged_raw)

    sort_dt = created_dt or judged_dt
    if sort_dt is not None:
        epoch = sort_dt.timestamp()
    else:
        try:
            epoch = rdir.stat().st_mtime
        except OSError:
            epoch = 0.0

    created_str: str | None = None
    if isinstance(created_raw, str):
        created_str = created_raw
    elif isinstance(judged_raw, str):
        created_str = judged_raw
    return epoch, created_str, results_data


def _iter_run_dirs(root: Path) -> list[Path]:
    base = runs_dir(root)
    if not base.is_dir():
        return []
    dirs: list[Path] = []
    for entry in base.iterdir():
        if entry.is_dir() and not entry.is_symlink() and _valid_run_id(entry.name):
            dirs.append(entry)
    return dirs


def _sorted_run_dirs(root: Path) -> list[tuple[Path, float, str | None, dict[str, object] | None]]:
    items: list[tuple[Path, float, str | None, dict[str, object] | None]] = []
    for rdir in _iter_run_dirs(root):
        epoch, created_str, results_data = _run_timestamps(rdir)
        items.append((rdir, epoch, created_str, results_data))
    items.sort(key=lambda item: (item[1], item[0].name), reverse=True)
    return items


def resolve_run_id(root: Path, run_id: str | None) -> str:
    """Resolve ``None`` or ``"latest"`` to an actual ``run_id`` (or raise ``RUN_NOT_FOUND``)."""
    base = runs_dir(root)
    if run_id is None or run_id == "latest":
        latest = base / "latest"
        if latest.is_symlink():
            try:
                target_name = Path(os.readlink(latest)).name
            except OSError:
                target_name = ""
            if _valid_run_id(target_name) and (base / target_name).is_dir():
                return target_name
        elif latest.is_dir():
            resolved_name = latest.resolve().name
            if _valid_run_id(resolved_name) and (base / resolved_name).is_dir():
                return resolved_name

        candidates: list[tuple[float, str]] = []
        for rdir in _iter_run_dirs(root):
            if (rdir / "plan.json").is_file():
                epoch, _, _ = _run_timestamps(rdir)
                candidates.append((epoch, rdir.name))
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
        label = "latest" if run_id is None else run_id
        raise ConfigError(f"run {label!r} not found", code="RUN_NOT_FOUND")

    if not _valid_run_id(run_id) or not (base / run_id).is_dir():
        raise ConfigError(f"run {run_id!r} not found", code="RUN_NOT_FOUND")
    return run_id


def update_latest_link(root: Path, run_id: str) -> None:
    """Atomically update ``.solouvm/runs/latest`` symlink to relative target ``run_id``."""
    base = runs_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    latest = base / "latest"
    if latest.exists() and not latest.is_symlink() and latest.is_dir():
        shutil.rmtree(latest)
    tmp = base / f".latest.{os.getpid()}.{id(run_id)}.tmp"
    with contextlib.suppress(OSError):
        tmp.unlink()
    os.symlink(run_id, tmp)
    try:
        os.replace(tmp, latest)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _git_output(root: Path, args: list[str], *, strip: bool = True) -> str | None:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() if strip else proc.stdout


def _capture_git(root: Path) -> tuple[dict[str, object], str | None]:
    inside = _git_output(root, ["rev-parse", "--is-inside-work-tree"])
    if inside != "true":
        return {"commit": None, "branch": None, "dirty": False, "diff": None}, None

    commit = _git_output(root, ["rev-parse", "HEAD"]) or None
    branch = _git_output(root, ["symbolic-ref", "--short", "-q", "HEAD"]) or None
    status = _git_output(root, ["status", "--porcelain"], strip=False)
    dirty = bool(status and status.strip())

    diff_text: str | None = None
    if commit is not None:
        raw_diff = _git_output(root, ["diff", "HEAD"], strip=False)
        if raw_diff:
            diff_text = raw_diff
            dirty = True
    elif dirty:
        raw_diff = _git_output(root, ["diff"], strip=False)
        if raw_diff:
            diff_text = raw_diff

    diff_name = "git.diff" if diff_text is not None else None
    return {
        "commit": commit,
        "branch": branch,
        "dirty": dirty,
        "diff": diff_name,
    }, diff_text


def _remember_diff(
    env_data: dict[str, object], git_data: dict[str, object], diff_text: str
) -> None:
    run_id = str(env_data["run_id"])
    for obj in (env_data, git_data):
        key = (id(obj), run_id)
        _PENDING_DIFFS[key] = diff_text
        _PENDING_DIFFS.move_to_end(key)
    while len(_PENDING_DIFFS) > _PENDING_MAX:
        _PENDING_DIFFS.popitem(last=False)

    _PENDING_DIFFS_BY_RUN[run_id] = diff_text
    _PENDING_DIFFS_BY_RUN.move_to_end(run_id)
    while len(_PENDING_DIFFS_BY_RUN) > _PENDING_MAX:
        _PENDING_DIFFS_BY_RUN.popitem(last=False)


def _component_kind(obj: object, cfg_kind: str | None) -> str:
    kind = getattr(obj, "kind", None)
    if isinstance(kind, str) and kind:
        return kind
    if cfg_kind:
        return cfg_kind
    return type(obj).__name__.lower()


def capture_env(
    root: Path,
    *,
    run_id: str,
    config: Config,
    backend: Backend,
    provider: Provider,
    solouvm_version: str = "0.1.0",
    b_host: str | None = None,
    sim_version: str | None = None,
) -> dict[str, object]:
    """Capture environment metadata for ``env.json`` per formats.md §5."""
    root = Path(root)
    a_host = socket.gethostname()
    transport_kind = getattr(provider.transport, "kind", None)
    cfg_provider = config.providers.get(provider.name)

    resolved_t_kind = _component_kind(
        provider.transport, cfg_provider.transport.kind if cfg_provider else None
    )
    resolved_l_kind = _component_kind(
        provider.launcher, cfg_provider.launcher.kind if cfg_provider else None
    )
    resolved_s_kind = _component_kind(
        provider.sync, cfg_provider.sync.kind if cfg_provider else None
    )

    resolved_b_host = b_host
    if resolved_b_host is None:
        t_host = getattr(provider.transport, "host", None)
        if isinstance(t_host, str) and t_host:
            resolved_b_host = t_host
        elif (transport_kind or resolved_t_kind) == "local":
            resolved_b_host = a_host

    git_data, diff_text = _capture_git(root)
    env_data: dict[str, object] = {
        "schema": ENV_SCHEMA,
        "run_id": run_id,
        "solouvm": solouvm_version,
        "python": platform.python_version(),
        "a_host": a_host,
        "b_host": resolved_b_host,
        "provider": {
            "name": provider.name,
            "transport": resolved_t_kind,
            "launcher": resolved_l_kind,
            "sync": resolved_s_kind,
        },
        "git": git_data,
        "simulator": {
            "name": backend.name,
            "version": sim_version,
        },
    }
    if diff_text is not None:
        _remember_diff(env_data, git_data, diff_text)
    return env_data


def write_env(run_path: Path, env_data: dict[str, object]) -> None:
    """Atomically write ``env.json`` (and ``git.diff`` when dirty) into ``run_path``."""
    run_path = Path(run_path)
    run_path.mkdir(parents=True, exist_ok=True)
    git_data = env_data.get("git")
    run_id = str(env_data.get("run_id", ""))
    if isinstance(git_data, dict) and git_data.get("diff") == "git.diff":
        diff_text = (
            _PENDING_DIFFS.get((id(env_data), run_id))
            or _PENDING_DIFFS.get((id(git_data), run_id))
            or _PENDING_DIFFS_BY_RUN.get(run_id)
        )
        if diff_text is not None:
            _atomic_write_text(run_path / "git.diff", diff_text)
    payload = json.dumps(env_data, indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(run_path / "env.json", payload)


def detect_sim_version(backend: Backend, provider: Provider, root: Path) -> str | None:
    """Execute ``backend.version`` on B and return its first non-empty line, or ``None``."""
    raw_cmd = backend.version
    if not raw_cmd or not raw_cmd.strip():
        return None
    cmd_str = raw_cmd.replace("{root}", ".").strip()
    setup = provider.setup.strip() if provider.setup else ""
    if setup:
        argv = ["bash", "-c", f"{{\n{setup}\n}} >/dev/null 2>&1 && {cmd_str}"]
    elif _SHELL_META_RE.search(cmd_str):
        argv = ["bash", "-c", cmd_str]
    else:
        try:
            argv = shlex.split(cmd_str)
        except ValueError:
            return None
        if not argv:
            return None

    exec_output = getattr(provider.transport, "exec_output", None)
    if not callable(exec_output):
        return None

    is_local = getattr(provider.transport, "kind", "local") == "local"
    has_root = getattr(provider.transport, "root", None) is not None
    cwd = str(root) if (is_local or has_root) else "."
    try:
        rc, out = exec_output(argv, cwd=cwd, check=False)
    except (InfraError, OSError, ValueError):
        return None
    if rc != 0 or not isinstance(out, str):
        return None
    return "\n".join(line.strip() for line in out.splitlines() if line.strip()) or None


def _run_has_active_job(rdir: Path) -> bool:
    from solouvm.judge.facts import FactsError, read_facts

    for facts_path in rdir.glob("*/try*/job.facts"):
        try:
            facts = read_facts(facts_path)
        except (FactsError, OSError, UnicodeDecodeError):
            # Fallback line scan in case only header exists or minor corruption
            try:
                text = facts_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            state = None
            for line in text.splitlines():
                k, sep, v = line.partition("=")
                if sep and k.strip() == "state":
                    state = v.strip()
            if state == "running":
                return True
            continue
        if facts is not None and facts.state == "running":
            return True
    return False


def track_run_activity(operation):
    """Hold an A-side run lock from generation until execution returns."""
    @wraps(operation)
    def tracked(**kwargs):
        if kwargs.get("dry_run"):
            return operation(**kwargs)
        path = run_dir(kwargs["config"].root, kwargs["plan"].run_id)
        path.mkdir(parents=True, exist_ok=True)
        lock_path = path / ".controller"
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RunActiveError(f"run {path.name!r} is already active") from None
            try:
                return operation(**kwargs)
            finally:
                lock_path.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    path.rmdir()

    return tracked


def controller_is_active(rdir: Path) -> bool:
    try:
        with (rdir / ".controller").open() as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except FileNotFoundError:
        pass
    return False


def run_is_active(rdir: Path) -> bool:
    if controller_is_active(rdir) or _run_has_active_job(rdir):
        return True
    raw_plan = _read_json_dict(rdir / "plan.json")
    builds = raw_plan.get("builds") if raw_plan else None
    if not isinstance(builds, dict):
        return False
    build_root = rdir.parent.parent / "build"
    return any(
        _build_is_active(build_root / mode / key)
        for mode, key in builds.items()
        if isinstance(mode, str)
        and isinstance(key, str)
        and Path(mode).name == mode
        and Path(key).name == key
        and mode not in ("", ".", "..")
        and key not in ("", ".", "..")
    )


def list_history(root: Path, *, limit: int | None = None) -> list[dict[str, object]]:
    """List past runs newest-first (Y-04)."""
    if limit is not None and limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")
    entries: list[dict[str, object]] = []
    for rdir, _epoch, created_str, results_data in _sorted_run_dirs(root):
        plan_data = _read_json_dict(rdir / "plan.json")
        if plan_data is None and results_data is None:
            continue
        env_data = _read_json_dict(rdir / "env.json")
        git_data = env_data.get("git") if isinstance(env_data, dict) else None
        commit = git_data.get("commit") if isinstance(git_data, dict) else None
        dirty = git_data.get("dirty") if isinstance(git_data, dict) else None

        active = run_is_active(rdir)
        passed: int | None = None
        total: int = 0
        duration_s: float | None = None
        partial = False
        if results_data is not None:
            summary = results_data.get("summary")
            if isinstance(summary, dict):
                raw_passed = summary.get("passed")
                raw_total = summary.get("total")
                raw_dur = summary.get("duration_s")
                passed = int(raw_passed) if isinstance(raw_passed, int) else 0
                total = int(raw_total) if isinstance(raw_total, int) else 0
                duration_s = float(raw_dur) if isinstance(raw_dur, (int, float)) else 0.0
            partial = bool(results_data.get("partial", False))
            if active:
                status = "running"
            elif partial:
                status = "partial"
            elif passed is not None and passed == total:
                status = "pass"
            else:
                status = "fail"
        else:
            jobs_list = plan_data.get("jobs") if isinstance(plan_data, dict) else None
            total = len(jobs_list) if isinstance(jobs_list, list) else 0
            status = "running" if active else "pending"

        entries.append(
            {
                "run_id": rdir.name,
                "created_at": created_str,
                "commit": commit,
                "dirty": dirty,
                "passed": passed,
                "total": total,
                "duration_s": duration_s,
                "partial": partial,
                "status": status,
            }
        )
        if limit is not None and len(entries) >= limit:
            break
    return entries


def _job_duration_from_dict(job: dict[str, object]) -> float:
    attempts = job.get("attempts")
    if not isinstance(attempts, list):
        return 0.0
    total = 0.0
    for att in attempts:
        if isinstance(att, dict) and not att.get("wave"):
            dur = att.get("duration_s")
            if isinstance(dur, (int, float)) and not isinstance(dur, bool):
                total += float(dur)
    return round(total, 3)


def _outcome_bucket(status: object) -> str | None:
    if status == "pass":
        return "pass"
    if status in ("xfail", "skip"):
        return None
    if isinstance(status, str) and status:
        return "fail"
    return None


def case_history(
    root: Path, case_name: str, *, limit: int | None = None
) -> list[dict[str, object]]:
    """Return history entries for ``case_name`` across judged runs with flaky detection."""
    if limit is not None and limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")

    entries: list[dict[str, object]] = []
    job_outcomes: dict[str, set[str]] = {}
    case_outcomes: set[str] = set()
    case_runs: set[str] = set()

    for rdir, _epoch, created_str, results_data in _sorted_run_dirs(root):
        if results_data is None:
            continue
        jobs = results_data.get("jobs")
        if not isinstance(jobs, list):
            continue
        env_data = _read_json_dict(rdir / "env.json")
        git_data = env_data.get("git") if isinstance(env_data, dict) else None
        commit = git_data.get("commit") if isinstance(git_data, dict) else None

        for job in jobs:
            if not isinstance(job, dict) or job.get("case") != case_name:
                continue
            jid = str(job.get("job_id", ""))
            status = str(job.get("status", ""))
            bucket = _outcome_bucket(status)
            if bucket is not None:
                job_outcomes.setdefault(jid, set()).add(bucket)
                case_outcomes.add(bucket)
                case_runs.add(rdir.name)
            entries.append(
                {
                    "run_id": rdir.name,
                    "created_at": created_str,
                    "commit": commit,
                    "job_id": jid,
                    "seed": job.get("seed"),
                    "status": status,
                    "judged_status": job.get("judged_status"),
                    "reason": job.get("reason", ""),
                    "duration_s": _job_duration_from_dict(job),
                    "signature": job.get("signature"),
                    "flaky": bool(job.get("flaky", False)),
                }
            )

    case_multi_run_flaky = len(case_runs) >= 2 and {"pass", "fail"}.issubset(case_outcomes)
    for item in entries:
        jid = str(item["job_id"])
        job_flaky = {"pass", "fail"}.issubset(job_outcomes.get(jid, set()))
        item["flaky"] = bool(item["flaky"] or job_flaky or case_multi_run_flaky)

    if limit is not None:
        return entries[:limit]
    return entries


def case_durations(root: Path) -> dict[str, float]:
    """Return durations from the most recent completed run for each case."""
    durations: dict[str, float] = {}
    for _rdir, _epoch, _created, results in _sorted_run_dirs(root):
        if not results or results.get("partial"):
            continue
        jobs = results.get("jobs")
        if not isinstance(jobs, list):
            continue
        run_durations: dict[str, float] = {}
        for job in jobs:
            if isinstance(job, dict) and isinstance(case := job.get("case"), str):
                duration = _job_duration_from_dict(job)
                if duration > 0:
                    run_durations[case] = max(run_durations.get(case, 0.0), duration)
        for case, duration in run_durations.items():
            durations.setdefault(case, duration)
    return durations


def job_flaky_in_history(
    root: Path,
    job_id: str,
    current_status: str,
    *,
    exclude_run_id: str | None = None,
) -> bool:
    """Return True if ``job_id`` has both passing and failing outcomes across runs (J-17)."""
    cur_bucket = _outcome_bucket(current_status)
    if cur_bucket is None:
        return False
    seen: set[str] = {cur_bucket}
    for rdir in _iter_run_dirs(root):
        if exclude_run_id is not None and rdir.name == exclude_run_id:
            continue
        results_data = _read_json_dict(rdir / "results.json")
        if results_data is None:
            continue
        jobs = results_data.get("jobs")
        if not isinstance(jobs, list):
            continue
        for job in jobs:
            if isinstance(job, dict) and job.get("job_id") == job_id:
                bucket = _outcome_bucket(job.get("status"))
                if bucket is not None:
                    seen.add(bucket)
                    if len(seen) >= 2:
                        return True
    return False


def _load_stats_cache(root: Path) -> dict[str, dict[str, object]]:
    path = Path(root) / _STATS_CACHE_REL
    data = _read_json_dict(path)
    if data is None or data.get("schema") != _STATS_SCHEMA:
        return {}
    runs = data.get("runs")
    if not isinstance(runs, dict):
        return {}
    return {str(k): v for k, v in runs.items() if isinstance(v, dict)}


def _save_stats_cache(root: Path, runs: dict[str, dict[str, object]]) -> None:
    path = Path(root) / _STATS_CACHE_REL
    payload = {"schema": _STATS_SCHEMA, "runs": runs}
    _atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def record_run_stats(
    root: Path,
    run_id: str,
    stats_by_case: dict[str, dict[str, list[float | int]]],
) -> None:
    """Persist lightweight per-run sim_end_ns / log_lines stats under ``.solouvm/cache/``."""
    runs = _load_stats_cache(root)
    existing = set(d.name for d in _iter_run_dirs(root))
    pruned = {rid: val for rid, val in runs.items() if rid in existing or rid == run_id}
    pruned[run_id] = stats_by_case
    _save_stats_cache(root, pruned)


def _extract_log_stats_from_file(log_path: Path) -> tuple[float | None, int | None]:
    if not log_path.is_file():
        return None, None
    line_count = 0
    test_done_ns: float | None = None
    sim_time_ns: float | None = None
    try:
        with log_path.open(encoding="utf-8", errors="replace") as fh:
            for line_count, raw in enumerate(fh, 1):
                if "[TEST_DONE]" in raw:
                    match = _TEST_DONE_RE.search(raw)
                    if match:
                        with contextlib.suppress(TypeError, ValueError):
                            test_done_ns = parse_sim_time_ns("".join(match.group(1).split()))
                if "sim time:" in raw.lower():
                    match2 = _SIM_TIME_RE.search(raw)
                    if match2:
                        with contextlib.suppress(TypeError, ValueError):
                            sim_time_ns = parse_sim_time_ns(f"{match2.group(1)}{match2.group(2)}")
                _ = line_count
    except OSError:
        return None, None
    return (test_done_ns if test_done_ns is not None else sim_time_ns), line_count


def case_historical_medians(
    root: Path,
    case_name: str,
    *,
    exclude_run_id: str | None = None,
) -> tuple[float | None, float | None]:
    """Compute ``(median_sim_end_ns, median_log_lines)`` across prior passing runs (J-19)."""
    root = Path(root)
    cached_runs = _load_stats_cache(root)
    sim_ends: list[float] = []
    log_lines: list[float] = []

    for rdir in _iter_run_dirs(root):
        rid = rdir.name
        if exclude_run_id is not None and rid == exclude_run_id:
            continue
        if rid in cached_runs:
            case_stats = cached_runs[rid].get(case_name)
            if isinstance(case_stats, dict):
                for val in case_stats.get("sim_end_ns", []) or ():
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        sim_ends.append(float(val))
                for val in case_stats.get("log_lines", []) or ():
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        log_lines.append(float(val))
            continue

        results_data = _read_json_dict(rdir / "results.json")
        if results_data is None:
            continue
        jobs = results_data.get("jobs")
        if not isinstance(jobs, list):
            continue
        for job in jobs:
            if not isinstance(job, dict) or job.get("case") != case_name:
                continue
            if job.get("status") != "pass":
                continue
            raw_end = job.get("sim_end_ns")
            raw_lines = job.get("log_lines", job.get("line_count"))
            if isinstance(raw_end, (int, float)) and not isinstance(raw_end, bool):
                sim_ends.append(float(raw_end))
            if isinstance(raw_lines, (int, float)) and not isinstance(raw_lines, bool):
                log_lines.append(float(raw_lines))
            if raw_end is None or raw_lines is None:
                attempts = job.get("attempts")
                if isinstance(attempts, list):
                    passing_atts = [
                        a
                        for a in attempts
                        if isinstance(a, dict) and not a.get("wave") and a.get("status") == "pass"
                    ]
                    target_att = passing_atts[0] if passing_atts else None
                    if target_att and isinstance(target_att.get("log"), str):
                        log_end, log_cnt = _extract_log_stats_from_file(root / target_att["log"])
                        if raw_end is None and log_end is not None:
                            sim_ends.append(log_end)
                        if raw_lines is None and log_cnt is not None:
                            log_lines.append(float(log_cnt))

    med_end = float(statistics.median(sim_ends)) if sim_ends else None
    med_lines = float(statistics.median(log_lines)) if log_lines else None
    return med_end, med_lines


def _build_is_active(build_dir: Path) -> bool:
    from solouvm.judge.facts import FactsError, parse_facts

    lock_dir = build_dir.with_name(f"{build_dir.name}.lock")
    if lock_dir.exists():
        return True
    facts_path = build_dir / "job.facts"
    if not facts_path.is_file():
        return False
    try:
        raw = parse_facts(facts_path.read_text(encoding="utf-8", errors="replace"))
    except (FactsError, OSError):
        return False
    else:
        return raw.state == "running"


def prune_history(
    root: Path,
    *,
    keep: int,
    delete: bool = True,
    dry_run: bool = False,
    remote_run_active: Callable[[str], bool] | None = None,
    remote_build_active: Callable[[str], bool] | None = None,
) -> dict[str, list[str]]:
    """Identify and optionally remove old runs beyond ``keep`` and unreferenced builds (Y-06)."""
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 0:
        raise ConfigError(
            f"history.keep must be a non-negative int, got {keep!r}", code="CFG_INVALID"
        )
    root = Path(root)
    sorted_runs = _sorted_run_dirs(root)

    def active(path: Path) -> bool:
        return run_is_active(path) or bool(remote_run_active and remote_run_active(path.name))

    def build_active(path: Path) -> bool:
        label = f"{path.parent.name}/{path.name}"
        return _build_is_active(path) or bool(remote_build_active and remote_build_active(label))

    kept_set: set[str] = set()
    for idx, (rdir, _epoch, _created, _results) in enumerate(sorted_runs):
        if idx < keep or active(rdir):
            kept_set.add(rdir.name)

    kept_runs = [rdir.name for rdir, *_ in sorted_runs if rdir.name in kept_set]
    removed_runs = [rdir.name for rdir, *_ in sorted_runs if rdir.name not in kept_set]

    referenced_builds: set[tuple[str, str]] = set()
    base_runs = runs_dir(root)
    for rid in kept_runs:
        plan_path = base_runs / rid / "plan.json"
        if not plan_path.is_file():
            continue
        try:
            plan = read_plan(plan_path)
        except ConfigError:
            plan_raw = _read_json_dict(plan_path)
            if isinstance(plan_raw, dict) and isinstance(plan_raw.get("builds"), dict):
                for mode, key in plan_raw["builds"].items():
                    if isinstance(mode, str) and isinstance(key, str):
                        referenced_builds.add((mode, key))
            continue
        for mode, key in plan.builds.items():
            referenced_builds.add((mode, key))

    build_root = root / ".solouvm" / "build"
    removed_builds: list[str] = []
    build_dirs_to_remove: list[Path] = []
    if build_root.is_dir():
        for mode_dir in sorted(build_root.iterdir()):
            if not mode_dir.is_dir() or mode_dir.name.startswith("."):
                continue
            for key_dir in sorted(mode_dir.iterdir()):
                if (
                    not key_dir.is_dir()
                    or key_dir.name.startswith(".")
                    or key_dir.name.endswith(".lock")
                ):
                    continue
                if (mode_dir.name, key_dir.name) in referenced_builds:
                    continue
                if build_active(key_dir):
                    continue
                removed_builds.append(f"{mode_dir.name}/{key_dir.name}")
                build_dirs_to_remove.append(key_dir)

    if delete and not dry_run:
        for rid in list(removed_runs):
            target = base_runs / rid
            if active(target):
                removed_runs.remove(rid)
                kept_runs.append(rid)
                continue
            if target.exists():
                shutil.rmtree(target)

        latest = base_runs / "latest"
        if latest.is_symlink():
            try:
                cur_target = Path(os.readlink(latest)).name
            except OSError:
                cur_target = ""
            if cur_target in removed_runs or not (base_runs / cur_target).is_dir():
                if kept_runs:
                    update_latest_link(root, kept_runs[0])
                else:
                    with contextlib.suppress(OSError):
                        latest.unlink()

        for bdir in build_dirs_to_remove:
            if build_active(bdir):
                removed_builds.remove(f"{bdir.parent.name}/{bdir.name}")
            elif bdir.exists():
                shutil.rmtree(bdir)

        if removed_runs and (root / _STATS_CACHE_REL).is_file():
            cached = _load_stats_cache(root)
            for rid in removed_runs:
                cached.pop(rid, None)
            _save_stats_cache(root, cached)

    return {
        "removed_runs": removed_runs,
        "removed_builds": removed_builds,
        "kept_runs": kept_runs,
    }
