"""Triage task planning and ``tasks.json`` persistence (AI-05, AI-20, formats.md §10)."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
import time
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.collect.cases import CaseSource, collect_cases_with_sources
from solouvm.errors import ConfigError, InfraError
from solouvm.history import resolve_run_id, run_dir, runs_dir
from solouvm.model import Case, Result
from solouvm.report.json import read_results_json, render_json

if TYPE_CHECKING:
    from solouvm.config.schema import Config

__all__ = [
    "FAIL_TRIAGE_STATUSES",
    "GROUP_BY_MODES",
    "PASS_POLICIES",
    "SKIP_TRIAGE_STATUSES",
    "TASKS_SCHEMA",
    "atomic_write_json",
    "plan_tasks",
    "primary_case_for_task",
    "read_tasks_json",
    "tasks_json_path",
    "tasks_lock",
]

TASKS_SCHEMA = 1
PASS_POLICIES: tuple[str, ...] = ("none", "changed", "all")
GROUP_BY_MODES: tuple[str, ...] = ("signature", "case", "job")
FAIL_TRIAGE_STATUSES: frozenset[str] = frozenset({"fail", "timeout", "killed", "xpass"})
SKIP_TRIAGE_STATUSES: frozenset[str] = frozenset({"error", "incomplete", "xfail", "skip"})

_LOCK_TIMEOUT_S = 30.0
_LOCK_POLL_S = 0.05
_SHARED_TEST_MARKERS = ("_base_test", "_test_pkg", "_test_files", "base_test", "test_pkg")


def tasks_json_path(root: Path, run_id: str) -> Path:
    """Return ``<root>/.solouvm/runs/<run_id>/triage/tasks.json``."""
    return run_dir(Path(root), run_id) / "triage" / "tasks.json"


@contextmanager
def tasks_lock(root: Path, run_id: str, *, timeout: float = _LOCK_TIMEOUT_S) -> Iterator[None]:
    """Acquire an exclusive ``mkdir`` lock guarding ``tasks.json`` updates for ``run_id``."""
    tpath = tasks_json_path(root, run_id)
    tpath.parent.mkdir(parents=True, exist_ok=True)
    lock_path = tpath.parent / ".tasks.lock"
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock_path.mkdir()
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise InfraError(
                    f"could not acquire triage tasks lock within {timeout:g}s",
                    code="PROVIDER_FAILED",
                    file=lock_path,
                    hint=f"if no triage process is running, remove {lock_path}",
                ) from None
            time.sleep(_LOCK_POLL_S)
    try:
        yield
    finally:
        with contextlib.suppress(OSError):
            lock_path.rmdir()


def atomic_write_text_safe(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a unique same-directory temp file and ``os.replace``."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InfraError(
            f"failed to create directory {path.parent}: {exc}",
            code="PROVIDER_FAILED",
        ) from exc
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise InfraError(f"failed to write {path}: {exc}", code="PROVIDER_FAILED") from exc
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    """Serialize ``payload`` deterministically and atomically to ``path``."""
    atomic_write_text_safe(path, render_json(payload))


def read_tasks_json(path: Path) -> dict[str, object]:
    """Read and validate ``triage/tasks.json`` per formats.md §10 and N-08."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"triage tasks file {path} not found",
            code="TASK_NOT_FOUND",
            file=path,
            hint="run 'solouvm triage plan' first",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigError(
            f"cannot load triage tasks file {path}: {exc}",
            code="CFG_LOAD_FAILED",
            file=path,
        ) from exc
    if not isinstance(data, dict) or type(data.get("schema")) is not int:
        raise ConfigError(
            f"invalid triage tasks file {path}: missing integer schema",
            code="CFG_INVALID",
            file=path,
        )
    if data["schema"] != TASKS_SCHEMA:
        raise ConfigError(
            f"unsupported triage tasks schema {data['schema']!r} (expected {TASKS_SCHEMA})",
            code="CFG_INVALID",
            file=path,
            hint="re-run 'solouvm triage plan' to regenerate tasks.json",
        )
    if not isinstance(data.get("tasks"), list):
        raise ConfigError(
            f"invalid triage tasks file {path}: 'tasks' must be a list",
            code="CFG_INVALID",
            file=path,
        )
    return data


def _job_case_from_id(job_id: str, known_cases: Sequence[str]) -> str:
    prefix, sep, _ = job_id.partition("__")
    if sep and (not known_cases or prefix in known_cases):
        return prefix
    for name in known_cases:
        if job_id.startswith(f"{name}__"):
            return name
    return prefix or (known_cases[0] if known_cases else "")


def primary_case_for_task(task: dict[str, object]) -> str:
    """Return the case in ``task`` with the most affected jobs (tie-broken deterministically)."""
    raw_cases = task.get("cases")
    cases = (
        [str(c) for c in raw_cases if isinstance(c, str) and c]
        if isinstance(raw_cases, list)
        else []
    )
    if not cases:
        raise ConfigError(
            f"task {task.get('id', '?')!r} has no cases",
            code="TRIAGE_INVALID",
        )
    counts: dict[str, int] = dict.fromkeys(cases, 0)
    raw_jobs = task.get("jobs")
    if isinstance(raw_jobs, list):
        for job in raw_jobs:
            if not isinstance(job, dict):
                continue
            cname = job.get("case")
            if not isinstance(cname, str) or cname not in counts:
                cname = _job_case_from_id(str(job.get("job_id", "")), cases)
            if cname in counts:
                counts[cname] += 1
    best = max(cases, key=lambda name: (counts[name], -cases.index(name)))
    return best


def _dict_to_result(raw: dict[str, object]) -> Result:
    status = str(raw.get("status", ""))
    judged = str(raw.get("judged_status") or status)
    raw_status = str(raw.get("raw_status") or judged)
    sig = raw.get("signature")
    sig_dict = (
        {str(k): str(v) for k, v in sig.items() if v is not None} if isinstance(sig, dict) else None
    )
    first_err = raw.get("first_error")
    first_err_dict = dict(first_err) if isinstance(first_err, dict) else None
    seed_val = raw.get("seed", 0)
    seed = int(seed_val) if isinstance(seed_val, int) and not isinstance(seed_val, bool) else 0
    repro_raw = raw.get("repro")
    repro_dict = (
        {str(k): str(v) for k, v in repro_raw.items() if isinstance(v, str)}
        if isinstance(repro_raw, dict)
        else {}
    )
    return Result(
        job_id=str(raw.get("job_id", "")),
        case=str(raw.get("case", "")),
        seed=seed,
        status=status,
        judged_status=judged,
        raw_status=raw_status,
        category=str(raw["category"]) if isinstance(raw.get("category"), str) else None,
        reason=str(raw.get("reason", "")),
        first_error=first_err_dict,
        signature=sig_dict,
        uvm=dict(raw["uvm"]) if isinstance(raw.get("uvm"), dict) else {},
        waivers=[dict(w) for w in (raw.get("waivers") or []) if isinstance(w, dict)],
        expect=[dict(e) for e in (raw.get("expect") or []) if isinstance(e, dict)],
        weak_signals=[str(s) for s in (raw.get("weak_signals") or [])],
        known=[str(k) for k in (raw.get("known") or [])],
        triage=dict(raw["triage"]) if isinstance(raw.get("triage"), dict) else None,
        flaky=bool(raw.get("flaky", False)),
        attempts=[dict(a) for a in (raw.get("attempts") or []) if isinstance(a, dict)],
        repro=repro_dict,
    )


def _load_results_list(root: Path, run_id: str) -> list[Result]:
    data = read_results_json(run_dir(root, run_id) / "results.json")
    raw_jobs = data.get("jobs")
    if not isinstance(raw_jobs, list):
        raise ConfigError(
            f"invalid results.json for run {run_id!r}: 'jobs' must be a list",
            code="CFG_INVALID",
        )
    return [_dict_to_result(item) for item in raw_jobs if isinstance(item, dict)]


def _parse_iso(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.astimezone() if dt.tzinfo is None else dt


def _read_json_dict(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _valid_run_dir(entry: Path) -> bool:
    name = entry.name
    return (
        entry.is_dir()
        and not entry.is_symlink()
        and bool(name)
        and name not in (".", "..", "latest")
        and not name.startswith(".")
    )


def _previous_judged_run(root: Path, current_run_id: str) -> tuple[Path, dict[str, object]] | None:
    base = runs_dir(root)
    if not base.is_dir():
        return None

    entries: list[tuple[float, int, str, Path, dict[str, object] | None]] = []
    for entry in base.iterdir():
        if not _valid_run_dir(entry):
            continue
        plan_path = entry / "plan.json"
        res_path = entry / "results.json"
        plan_data = _read_json_dict(plan_path)
        res_data = _read_json_dict(res_path)
        created_dt = _parse_iso(plan_data.get("created_at") if plan_data else None)
        judged_dt = _parse_iso(res_data.get("judged_at") if res_data else None)
        ref_dt = created_dt or judged_dt
        try:
            ref_ns = (
                plan_path.stat().st_mtime_ns
                if plan_path.is_file()
                else (
                    res_path.stat().st_mtime_ns if res_path.is_file() else entry.stat().st_mtime_ns
                )
            )
        except OSError:
            ref_ns = 0
        epoch = ref_dt.timestamp() if ref_dt is not None else ref_ns / 1e9
        entries.append((epoch, ref_ns, entry.name, entry, res_data))

    entries.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
    cur_idx = next((idx for idx, item in enumerate(entries) if item[2] == current_run_id), None)
    candidates = entries[cur_idx + 1 :] if cur_idx is not None else entries
    for _, _, name, rdir, res_data in candidates:
        if name != current_run_id and res_data is not None:
            return rdir, res_data
    return None


def _run_git(root: Path, args: list[str]) -> tuple[int, str] | None:
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
    return proc.returncode, proc.stdout


def _git_changed_files(root: Path, prev_commit: str) -> set[str] | None:
    inside = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if inside is None or inside[0] != 0 or inside[1].strip() != "true":
        return None
    diff_res = _run_git(root, ["diff", "--name-only", prev_commit])
    if diff_res is None or diff_res[0] != 0:
        return None
    changed = {line.strip().replace("\\", "/") for line in diff_res[1].splitlines() if line.strip()}
    untracked = _run_git(root, ["ls-files", "--others", "--exclude-standard"])
    if untracked is not None and untracked[0] == 0:
        changed.update(
            line.strip().replace("\\", "/") for line in untracked[1].splitlines() if line.strip()
        )
    return changed


def _prev_reference_times(prev_dir: Path) -> tuple[int | None, int | None]:
    """Return ``(file_mtime_ns, created_at_ns)`` for ``prev_dir`` plan or results."""
    plan_path = prev_dir / "plan.json"
    res_path = prev_dir / "results.json"
    target = plan_path if plan_path.is_file() else (res_path if res_path.is_file() else None)
    file_ns: int | None = None
    if target is not None:
        with contextlib.suppress(OSError):
            file_ns = target.stat().st_mtime_ns

    plan_data = _read_json_dict(plan_path)
    res_data = _read_json_dict(res_path)
    dt = _parse_iso(plan_data.get("created_at") if plan_data else None) or _parse_iso(
        res_data.get("judged_at") if res_data else None
    )
    created_ns = int(dt.timestamp() * 1_000_000_000) if dt is not None else None
    return file_ns, created_ns


def _file_newer_than_prev(path: Path, file_ns: int | None, created_ns: int | None) -> bool:
    try:
        mtime_ns = path.stat().st_mtime_ns
    except OSError:
        return False
    if file_ns is not None:
        return mtime_ns > file_ns
    if created_ns is not None:
        return mtime_ns > created_ns
    return False


def _rel_posix(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return path.as_posix()


def _verif_test_files_for_case(
    *,
    root: Path,
    case: Case,
    all_cases: Sequence[Case],
    all_test_files: Sequence[Path],
) -> list[Path]:
    if not all_test_files:
        return []
    other_tokens: list[str] = []
    for other in all_cases:
        if other.name == case.name:
            continue
        other_tokens.append(other.name.lower())
        if other.uvm_test:
            other_tokens.append(other.uvm_test.lower())

    matched: list[Path] = []
    case_name_low = case.name.lower()
    uvm_low = case.uvm_test.lower() if case.uvm_test else ""
    for path in all_test_files:
        stem = path.stem.lower()
        if (uvm_low and uvm_low in stem) or (case_name_low in stem):
            matched.append(path)
            continue
        if any(marker in stem for marker in _SHARED_TEST_MARKERS):
            matched.append(path)
            continue
        if not any(tok in stem for tok in other_tokens):
            matched.append(path)
    return matched


def _changed_cases_vs_prev(
    *,
    root: Path,
    current_run_id: str,
    case_map: dict[str, Case],
    case_files: dict[str, Path],
) -> set[str]:
    prev_info = _previous_judged_run(root, current_run_id)
    if prev_info is None:
        return set()
    prev_dir, prev_results = prev_info

    prev_cases = {
        str(j["case"])
        for j in (prev_results.get("jobs") or [])
        if isinstance(j, dict) and isinstance(j.get("case"), str)
    }

    tests_dir = root / "verif" / "tests"
    all_test_files = (
        sorted(p for p in tests_dir.rglob("*") if p.is_file()) if tests_dir.is_dir() else []
    )
    all_cases = list(case_map.values())

    env_data = _read_json_dict(prev_dir / "env.json")
    git_data = env_data.get("git") if isinstance(env_data, dict) else None
    prev_commit = git_data.get("commit") if isinstance(git_data, dict) else None
    prev_dirty = bool(git_data.get("dirty")) if isinstance(git_data, dict) else False

    git_changed: set[str] | None = None
    head_commit: str | None = None
    if isinstance(prev_commit, str) and prev_commit.strip():
        git_changed = _git_changed_files(root, prev_commit.strip())
        head_res = _run_git(root, ["rev-parse", "HEAD"])
        if head_res is not None and head_res[0] == 0 and head_res[1].strip():
            head_commit = head_res[1].strip()

    file_ns, created_ns = _prev_reference_times(prev_dir)

    changed: set[str] = set()
    for cname, case in case_map.items():
        if prev_cases and cname not in prev_cases:
            changed.add(cname)
            continue
        watched: list[Path] = []
        if cname in case_files:
            watched.append(case_files[cname])
        watched.extend(
            _verif_test_files_for_case(
                root=root,
                case=case,
                all_cases=all_cases,
                all_test_files=all_test_files,
            )
        )
        for path in watched:
            if git_changed is not None:
                rel = _rel_posix(root, path)
                if rel in git_changed and (
                    not prev_dirty
                    or (head_commit is not None and head_commit != prev_commit.strip())
                    or _file_newer_than_prev(path, file_ns, created_ns)
                ):
                    changed.add(cname)
                    break
            elif _file_newer_than_prev(path, file_ns, created_ns):
                changed.add(cname)
                break
    return changed


def _sig_id_for(res: Result) -> str | None:
    if isinstance(res.signature, dict):
        raw_id = res.signature.get("id")
        if isinstance(raw_id, str) and raw_id:
            return raw_id
    return None


def _fail_group_key(res: Result, group_by: str) -> tuple[object, ...]:
    sig_id = _sig_id_for(res)
    sig_key = sig_id if sig_id is not None else f"__reason__:{res.reason or res.judged_status}"
    if group_by == "signature":
        return ("fail", sig_key)
    if group_by == "case":
        return ("fail", sig_key, res.case)
    return ("fail", res.job_id)


def _pass_group_key(res: Result, group_by: str) -> tuple[object, ...]:
    if group_by == "job":
        return ("pass", res.job_id)
    return ("pass", res.case)


def _existing_task_key(
    task: dict[str, object],
    *,
    eff_group_by: str,
    existing_group_by: str,
) -> tuple[object, ...] | None:
    kind = str(task.get("kind", ""))
    raw_jobs = task.get("jobs")
    jobs = [j for j in raw_jobs if isinstance(j, dict)] if isinstance(raw_jobs, list) else []
    raw_cases = task.get("cases")
    cases = [str(c) for c in raw_cases if isinstance(c, str)] if isinstance(raw_cases, list) else []

    if kind == "fail":
        sig_val = task.get("signature")
        sig_key = (
            sig_val
            if isinstance(sig_val, str) and sig_val
            else f"__reason__:{task.get('reason', '')}"
        )
        if eff_group_by == "signature" and existing_group_by == "signature":
            return ("fail", sig_key)
        if eff_group_by == "case" and (existing_group_by == "case" or len(cases) == 1):
            return ("fail", sig_key, cases[0] if cases else "")
        if eff_group_by == "job" and len(jobs) == 1:
            return ("fail", str(jobs[0].get("job_id", "")))
        return None

    if kind == "pass":
        if eff_group_by == "job" and len(jobs) == 1:
            return ("pass", str(jobs[0].get("job_id", "")))
        if eff_group_by in ("signature", "case") and existing_group_by != "job" and cases:
            return ("pass", cases[0])
        return None

    return None


def _ordered_cases_for_results(items: Sequence[Result]) -> list[str]:
    counts: dict[str, int] = defaultdict(int)
    for res in items:
        counts[res.case] += 1
    return sorted(counts, key=lambda cname: (-counts[cname], cname))


def _pass_reason_for(
    items: Sequence[Result],
    case: Case | None,
    changed_cases: set[str],
) -> str:
    weak: list[str] = []
    for res in items:
        for sig in res.weak_signals:
            if sig not in weak:
                weak.append(sig)
    if weak:
        return f"weak signal: {'; '.join(weak)}"
    if case is not None and case.stage == "wip":
        return "stage=wip"
    case_name = items[0].case
    if case_name in changed_cases:
        return "changed since previous run"
    return "pass_policy=all"


def _load_cases_and_files(
    config: Config,
    cases: list[Case] | None,
) -> tuple[dict[str, Case], dict[str, Path]]:
    sources: list[CaseSource] = []
    if cases is None:
        sources = collect_cases_with_sources(config)
        return {cs.case.name: cs.case for cs in sources}, {cs.case.name: cs.file for cs in sources}

    case_map = {c.name: c for c in cases}
    with contextlib.suppress(ConfigError):
        sources = collect_cases_with_sources(config)
    case_files = {cs.case.name: cs.file for cs in sources if cs.case.name in case_map}
    return case_map, case_files


def _resolve_plan_run_id(root: Path, run_id: str, *, has_results: bool) -> str:
    if not has_results or run_id in ("", "latest"):
        return resolve_run_id(root, run_id)
    if (
        not run_id
        or run_id in (".", "..", "latest")
        or run_id.startswith(".")
        or "/" in run_id
        or "\\" in run_id
    ):
        raise ConfigError(f"run {run_id!r} not found", code="RUN_NOT_FOUND")
    return run_id


def plan_tasks(
    *,
    config: Config,
    run_id: str,
    results: list[Result] | None = None,
    pass_policy: str | None = None,
    group_by: str | None = None,
    cases: list[Case] | None = None,
) -> list[dict[str, object]]:
    """Plan and persist triage tasks for ``run_id`` into ``triage/tasks.json``."""
    root = Path(config.root)
    actual_run = _resolve_plan_run_id(root, run_id, has_results=results is not None)

    eff_pass_policy = config.triage.pass_policy if pass_policy is None else pass_policy
    if not isinstance(eff_pass_policy, str) or eff_pass_policy not in PASS_POLICIES:
        raise ConfigError(
            f"invalid triage pass_policy {eff_pass_policy!r}; "
            f"expected one of {', '.join(PASS_POLICIES)}",
            code="CFG_INVALID",
        )
    eff_group_by = config.triage.group_by if group_by is None else group_by
    if not isinstance(eff_group_by, str) or eff_group_by not in GROUP_BY_MODES:
        raise ConfigError(
            f"invalid triage group_by {eff_group_by!r}; "
            f"expected one of {', '.join(GROUP_BY_MODES)}",
            code="CFG_INVALID",
        )

    result_list = _load_results_list(root, actual_run) if results is None else list(results)
    case_map, case_files = _load_cases_and_files(config, cases)

    changed_cases: set[str] = set()
    if eff_pass_policy == "changed":
        changed_cases = _changed_cases_vs_prev(
            root=root,
            current_run_id=actual_run,
            case_map=case_map,
            case_files=case_files,
        )

    fail_items: list[Result] = []
    pass_items: list[Result] = []
    for res in result_list:
        jstatus = res.judged_status or res.status
        if jstatus in FAIL_TRIAGE_STATUSES:
            fail_items.append(res)
        elif jstatus in SKIP_TRIAGE_STATUSES:
            continue
        elif jstatus == "pass":
            has_weak = bool(res.weak_signals) and bool(config.triage.weak_signals)
            if has_weak:
                pass_items.append(res)
            elif eff_pass_policy == "none":
                continue
            elif eff_pass_policy == "all":
                pass_items.append(res)
            elif eff_pass_policy == "changed":
                case_obj = case_map.get(res.case)
                is_wip = case_obj is not None and case_obj.stage == "wip"
                if is_wip or res.case in changed_cases:
                    pass_items.append(res)

    fail_items.sort(key=lambda r: (r.case, r.seed, r.job_id))
    pass_items.sort(key=lambda r: (r.case, r.seed, r.job_id))

    grouped: dict[tuple[object, ...], list[Result]] = defaultdict(list)
    group_order: list[tuple[object, ...]] = []
    for res in fail_items:
        key = _fail_group_key(res, eff_group_by)
        if key not in grouped:
            group_order.append(key)
        grouped[key].append(res)
    for res in pass_items:
        key = _pass_group_key(res, eff_group_by)
        if key not in grouped:
            group_order.append(key)
        grouped[key].append(res)

    current_judged = {r.job_id: (r.judged_status or r.status) for r in result_list}
    tpath = tasks_json_path(root, actual_run)

    with tasks_lock(root, actual_run):
        existing_tasks: list[dict[str, object]] = []
        existing_group_by = eff_group_by
        if tpath.is_file():
            existing_data = read_tasks_json(tpath)
            raw_pol = existing_data.get("policy")
            if isinstance(raw_pol, dict) and isinstance(raw_pol.get("group_by"), str):
                existing_group_by = raw_pol["group_by"]
            existing_tasks = [dict(t) for t in existing_data["tasks"] if isinstance(t, dict)]

        existing_by_key: dict[tuple[object, ...], dict[str, object]] = {}
        used_seqs: set[int] = set()
        for task in existing_tasks:
            tid = str(task.get("id", ""))
            _, _, tail = tid.rpartition("/")
            if tail.isascii() and tail.isdigit():
                used_seqs.add(int(tail))
            # Check stale against current mechanical results.
            raw_jobs = task.get("jobs")
            if isinstance(raw_jobs, list):
                for jentry in raw_jobs:
                    if not isinstance(jentry, dict):
                        continue
                    jid = str(jentry.get("job_id", ""))
                    if jid in current_judged and current_judged[jid] != jentry.get("judged_status"):
                        task["stale"] = True
            ekey = _existing_task_key(
                task,
                eff_group_by=eff_group_by,
                existing_group_by=existing_group_by,
            )
            if ekey is not None and ekey not in existing_by_key:
                existing_by_key[ekey] = task

        next_seq = max(used_seqs, default=0) + 1
        for key in group_order:
            items = grouped[key]
            kind = str(key[0])
            new_jobs = [
                {"job_id": r.job_id, "judged_status": r.judged_status or r.status} for r in items
            ]
            if key in existing_by_key:
                existing_task = existing_by_key[key]
                cur_jobs = (
                    [dict(j) for j in existing_task["jobs"] if isinstance(j, dict)]
                    if isinstance(existing_task.get("jobs"), list)
                    else []
                )
                seen_jids = {str(j.get("job_id", "")) for j in cur_jobs}
                added = False
                for jentry in new_jobs:
                    if jentry["job_id"] not in seen_jids:
                        cur_jobs.append(jentry)
                        seen_jids.add(jentry["job_id"])
                        added = True
                if added:
                    existing_task["jobs"] = cur_jobs
                    # Recompute ordered cases from combined jobs.
                    existing_cases = (
                        [str(c) for c in existing_task.get("cases", []) if isinstance(c, str)]
                        if isinstance(existing_task.get("cases"), list)
                        else []
                    )
                    counts: dict[str, int] = defaultdict(int)
                    for jentry in cur_jobs:
                        cname = _job_case_from_id(str(jentry.get("job_id", "")), existing_cases)
                        if cname:
                            counts[cname] += 1
                    for r in items:
                        if r.case not in counts:
                            counts[r.case] = 0
                    existing_task["cases"] = sorted(counts, key=lambda c: (-counts[c], c))
                    if existing_task.get("result") is not None:
                        existing_task["stale"] = True
                continue

            while next_seq in used_seqs:
                next_seq += 1
            used_seqs.add(next_seq)
            sig_id = _sig_id_for(items[0]) if kind == "fail" else None
            reason = (
                items[0].reason
                if kind == "fail"
                else _pass_reason_for(items, case_map.get(items[0].case), changed_cases)
            )
            existing_tasks.append(
                {
                    "id": f"{actual_run}/{next_seq}",
                    "kind": kind,
                    "cases": _ordered_cases_for_results(items),
                    "jobs": new_jobs,
                    "signature": sig_id,
                    "reason": reason,
                    "stale": False,
                    "result": None,
                    "review": None,
                }
            )
            next_seq += 1

        payload: dict[str, object] = {
            "schema": TASKS_SCHEMA,
            "run_id": actual_run,
            "policy": {
                "pass_policy": eff_pass_policy,
                "group_by": eff_group_by,
            },
            "tasks": existing_tasks,
        }
        atomic_write_json(tpath, payload)
        return existing_tasks
