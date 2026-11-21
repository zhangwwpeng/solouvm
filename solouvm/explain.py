"""Failure explanation helper: 8-section markdown and structured data (AI-04, AI-15, K-11)."""

from __future__ import annotations

import itertools
import json
import re
import shlex
import subprocess
from collections import deque
from datetime import datetime
from difflib import get_close_matches
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.collect.cases import collect_cases
from solouvm.errors import ConfigError, InfraError
from solouvm.history import resolve_run_id, run_dir, runs_dir
from solouvm.model import Bug, Case, Triage
from solouvm.report.json import PASS_STATUSES, read_results_json

if TYPE_CHECKING:
    from solouvm.config.schema import Config
    from solouvm.model import Plan
    from solouvm.providers.base import Provider, Transport

__all__ = [
    "EXPLAIN_SECTIONS",
    "explain",
    "explain_data",
    "extract_source_location",
    "find_previous_pass",
    "find_signature_history",
    "git_commits_since_pass",
    "read_log_context",
    "read_source_snippet",
]

EXPLAIN_SECTIONS: tuple[str, ...] = (
    "## 1. Status & Reason",
    "## 2. First Error & Log Context",
    "## 3. Source Code Snippets",
    "## 4. Expect Checks & Weak Signals",
    "## 5. Failure Signature & History",
    "## 6. Related Bugs & Last Triage",
    "## 7. Previous Pass & Git Commits",
    "## 8. Repro Commands",
)

# 匹配顺序覆盖 UVM `path(line)`、VCS `"path", line`、xrun `path:line`
_LOCATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?P<file>[\w./-]+\.(?:sv|svh|v|vh))\((?P<line>\d+)\)"),
    re.compile(r'"(?P<file>[\w./-]+\.(?:sv|svh|v|vh))",\s*(?P<line>\d+)'),
    re.compile(r"(?P<file>[\w./-]+\.(?:sv|svh|v|vh)):(?P<line>\d+)"),
)

_UVM_ERR_LINE_RE = re.compile(r"^\s*UVM_(?:ERROR|FATAL)\b")
_SIM_ERR_LINE_RE = re.compile(
    r"(?:^Error-\[|^Fatal:|Offending '|xmsim: \*[EF],|ncsim: \*[EF],|"
    r"^\*\* Error|^\*\* Fatal|^%Error|^ERROR:|Assertion failed)"
)
_LOG_PREFIX_RE = re.compile(r"^[> ]\s*\d+\s{2}")


def extract_source_location(text: str) -> tuple[str, int] | None:
    """Extract the first ``(file, line)`` reference from UVM, VCS, or xrun error text."""
    if not text:
        return None
    best_match: re.Match[str] | None = None
    for pattern in _LOCATION_PATTERNS:
        match = pattern.search(text)
        if match is not None and (best_match is None or match.start() < best_match.start()):
            best_match = match
    if best_match is None:
        return None
    return best_match.group("file"), int(best_match.group("line"))


def read_source_snippet(root: Path, rel_or_abs: str, line: int, context: int = 5) -> str:
    """Read lines ``max(1, line - context)``..``line + context`` with ``> `` marking ``line``."""
    raw_path = Path(rel_or_abs)
    target = raw_path if raw_path.is_absolute() else Path(root) / raw_path
    try:
        if not target.is_file():
            return f"source not available: {rel_or_abs}"
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return f"source not available: {rel_or_abs}"

    if not lines:
        return f"source not available: {rel_or_abs} (empty file)"

    ctx = max(0, context)
    start = max(1, line - ctx)
    end = min(len(lines), line + ctx)
    if start > end:
        return f"source not available: {rel_or_abs} (line {line} out of range; {len(lines)} lines)"

    out: list[str] = []
    for number in range(start, end + 1):
        marker = ">" if number == line else " "
        out.append(f"{marker} {number:5d}  {lines[number - 1]}")
    if line > len(lines):
        out.append(f"# note: line {line} exceeds file length ({len(lines)} lines)")
    return "\n".join(out)


def _format_log_line(lineno: int, target_line: int, text: str) -> str:
    marker = ">" if lineno == target_line else " "
    return f"{marker} {lineno:6d}  {text}"


def read_log_context(
    log_path: Path,
    line: int,
    context: int = 30,
    *,
    transport: Transport | None = None,
) -> str:
    """Stream log lines around ``line`` using ``islice`` or ``transport.read_lines``."""
    ctx = max(0, context)
    start = max(1, line - ctx)
    end = max(start, line + ctx)

    if transport is not None:
        path_str = str(log_path)
        try:
            if not transport.exists(path_str):
                return f"log not available: {log_path} (missing or cleaned)"
            pairs = list(transport.read_lines(path_str, lines=(start, end)))
        except (InfraError, OSError):
            return f"log not available: {log_path} (missing or cleaned)"
        filtered = [(ln, txt) for ln, txt in pairs if start <= ln <= end]
        if not filtered:
            return f"log not available: {log_path} (no lines in range {start}-{end})"
        return "\n".join(_format_log_line(ln, line, txt) for ln, txt in filtered)

    path = Path(log_path)
    if not path.is_file():
        return f"log not available: {log_path} (missing or cleaned)"
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            chunk = list(itertools.islice(handle, start - 1, end))
    except OSError:
        return f"log not available: {log_path} (missing or cleaned)"
    if not chunk:
        return f"log not available: {log_path} (no lines in range {start}-{end})"
    out: list[str] = []
    for offset, raw in enumerate(chunk):
        lineno = start + offset
        out.append(_format_log_line(lineno, line, raw.rstrip("\r\n")))
    return "\n".join(out)


def _read_log_tail(
    log_path: Path,
    tail: int = 30,
    *,
    transport: Transport | None = None,
) -> str:
    if transport is not None:
        path_str = str(log_path)
        try:
            if not transport.exists(path_str):
                return f"log not available: {log_path} (missing or cleaned)"
            line_count_fn = getattr(transport, "line_count", None)
            total = line_count_fn(path_str) if callable(line_count_fn) else None
            if isinstance(total, int) and total > 0:
                start = max(1, total - tail + 1)
                pairs = list(transport.read_lines(path_str, lines=(start, total)))
            else:
                pairs = list(deque(transport.read_lines(path_str), maxlen=tail))
        except (InfraError, OSError):
            return f"log not available: {log_path} (missing or cleaned)"
        if not pairs:
            return f"log not available: {log_path} (empty)"
        return "\n".join(_format_log_line(ln, -1, txt) for ln, txt in pairs)

    path = Path(log_path)
    if not path.is_file():
        return f"log not available: {log_path} (missing or cleaned)"
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            tail_items = list(deque(enumerate(handle, 1), maxlen=tail))
    except OSError:
        return f"log not available: {log_path} (missing or cleaned)"
    if not tail_items:
        return f"log not available: {log_path} (empty)"
    return "\n".join(_format_log_line(ln, -1, raw.rstrip("\r\n")) for ln, raw in tail_items)


def _valid_run_dir(entry: Path) -> bool:
    name = entry.name
    return (
        entry.is_dir()
        and not entry.is_symlink()
        and bool(name)
        and name not in (".", "..", "latest")
        and not name.startswith(".")
    )


def _read_json_dict(path: Path) -> dict[str, object] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


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


def _sorted_runs(root: Path) -> list[tuple[Path, str | None, dict[str, object] | None]]:
    """Return ``(run_dir, created_at_str, results_dict)`` sorted newest-first by ``created_at``."""
    base = runs_dir(root)
    if not base.is_dir():
        return []
    entries: list[tuple[float, str, Path, str | None, dict[str, object] | None]] = []
    for entry in base.iterdir():
        if not _valid_run_dir(entry):
            continue
        plan_data = _read_json_dict(entry / "plan.json")
        results_data = _read_json_dict(entry / "results.json")
        created_raw = plan_data.get("created_at") if plan_data else None
        judged_raw = results_data.get("judged_at") if results_data else None
        sort_dt = _parse_iso(created_raw) or _parse_iso(judged_raw)
        if sort_dt is not None:
            epoch = sort_dt.timestamp()
        else:
            try:
                epoch = entry.stat().st_mtime
            except OSError:
                epoch = 0.0
        created_str = (
            created_raw
            if isinstance(created_raw, str)
            else (judged_raw if isinstance(judged_raw, str) else None)
        )
        entries.append((epoch, entry.name, entry, created_str, results_data))
    entries.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [(rdir, created_str, res_data) for _, _, rdir, created_str, res_data in entries]


def find_previous_pass(root: Path, case_name: str, before_run_id: str) -> str | None:
    """Return the most recent ``run_id`` before ``before_run_id`` where ``case_name`` passed."""
    ordered = _sorted_runs(Path(root))
    before_idx = next(
        (idx for idx, (rdir, _, _) in enumerate(ordered) if rdir.name == before_run_id),
        None,
    )
    candidates = ordered[before_idx + 1 :] if before_idx is not None else ordered
    for rdir, _, results_data in candidates:
        if rdir.name == before_run_id or results_data is None:
            continue
        jobs = results_data.get("jobs")
        if not isinstance(jobs, list):
            continue
        case_jobs = [j for j in jobs if isinstance(j, dict) and j.get("case") == case_name]
        if (
            case_jobs
            and any(j.get("status") == "pass" for j in case_jobs)
            and all(j.get("status") in PASS_STATUSES for j in case_jobs)
        ):
            return rdir.name
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


def git_commits_since_pass(root: Path, prev_run_id: str | None) -> str:
    """Return ``git log --oneline <commit>..HEAD`` for ``prev_run_id`` or ``'git unavailable'``."""
    root = Path(root)
    inside = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if inside is None or inside[0] != 0 or inside[1].strip() != "true":
        return "git unavailable"
    head_res = _run_git(root, ["rev-parse", "HEAD"])
    if head_res is None or head_res[0] != 0 or not head_res[1].strip():
        return "git unavailable"

    if prev_run_id is None:
        return "no previous passing run"

    env_data = _read_json_dict(run_dir(root, prev_run_id) / "env.json")
    git_info = env_data.get("git") if isinstance(env_data, dict) else None
    commit = git_info.get("commit") if isinstance(git_info, dict) else None
    if not isinstance(commit, str) or not commit.strip():
        return f"no git commit recorded for previous pass {prev_run_id}"

    log_res = _run_git(root, ["log", "--oneline", f"{commit.strip()}..HEAD"])
    if log_res is None:
        return "git unavailable"
    if log_res[0] != 0:
        return f"git commit {commit.strip()} not reachable in history"
    output = log_res[1].strip()
    return output if output else "no new commits since previous pass"


def find_signature_history(
    root: Path,
    sig_id: str,
    exclude_run_id: str | None = None,
) -> list[dict[str, object]]:
    """Find historical failed jobs sharing failure signature ``sig_id`` newest-first."""
    if not sig_id:
        return []
    matches: list[dict[str, object]] = []
    for rdir, created_str, results_data in _sorted_runs(Path(root)):
        if exclude_run_id is not None and rdir.name == exclude_run_id:
            continue
        if results_data is None:
            continue
        jobs = results_data.get("jobs")
        if not isinstance(jobs, list):
            continue
        for job in jobs:
            if not isinstance(job, dict):
                continue
            sig = job.get("signature")
            if not isinstance(sig, dict):
                continue
            if sig.get("id") == sig_id or sig.get("text") == sig_id:
                matches.append(
                    {
                        "run_id": rdir.name,
                        "created_at": created_str,
                        "job_id": str(job.get("job_id", "")),
                        "case": str(job.get("case", "")),
                        "seed": job.get("seed"),
                        "status": str(job.get("status", "")),
                        "reason": str(job.get("reason", "")),
                        "signature": dict(sig),
                    }
                )
    return matches


def _select_attempt(
    *,
    root: Path,
    plan_job_dir: str,
    job_id: str,
    attempts: list[dict[str, object]],
    try_num: int | None,
    transport: Transport | None,
) -> tuple[int | None, str]:
    if try_num is not None:
        if isinstance(try_num, bool) or not isinstance(try_num, int) or try_num <= 0:
            raise ConfigError(
                f"invalid try number {try_num!r}; expected positive int",
                code="CFG_INVALID",
            )
        chosen = next((a for a in attempts if a.get("try") == try_num), None)
        if chosen is not None:
            log_rel = str(chosen.get("log") or f"{plan_job_dir}/try{try_num}/sim.log")
            return try_num, log_rel
        try_rel = f"{plan_job_dir}/try{try_num}"
        exists = transport.exists(try_rel) if transport is not None else (root / try_rel).exists()
        if not exists:
            raise ConfigError(
                f"try {try_num} not found for job {job_id!r}",
                code="JOB_NOT_FOUND",
            )
        return try_num, f"{try_rel}/sim.log"

    non_wave = [a for a in attempts if not a.get("wave")]
    chosen = non_wave[-1] if non_wave else (attempts[-1] if attempts else None)
    if chosen is not None:
        raw_try = chosen.get("try")
        resolved_try = (
            int(raw_try) if isinstance(raw_try, int) and not isinstance(raw_try, bool) else None
        )
        log_rel = str(chosen.get("log") or f"{plan_job_dir}/try{resolved_try or 1}/sim.log")
        return resolved_try, log_rel
    return None, f"{plan_job_dir}/try1/sim.log"


def _is_error_or_fatal_line(raw_line: str) -> bool:
    return bool(_UVM_ERR_LINE_RE.match(raw_line) or _SIM_ERR_LINE_RE.search(raw_line))


def _collect_source_snippets(
    *,
    root: Path,
    first_error: dict[str, object] | None,
    log_context: str,
) -> list[dict[str, object]]:
    locations: list[tuple[str, int]] = []
    seen: set[tuple[str, int]] = set()

    def _add_from(text: str) -> None:
        loc = extract_source_location(text)
        if loc is not None and loc not in seen:
            seen.add(loc)
            locations.append(loc)

    if isinstance(first_error, dict) and isinstance(first_error.get("text"), str):
        _add_from(first_error["text"])

    if not log_context.startswith("log not available:"):
        for formatted in log_context.splitlines():
            raw = _LOG_PREFIX_RE.sub("", formatted, count=1)
            if _is_error_or_fatal_line(raw):
                _add_from(raw)

    return [
        {
            "file": file_path,
            "line": lineno,
            "snippet": read_source_snippet(root, file_path, lineno, context=5),
        }
        for file_path, lineno in locations
    ]


def _load_cases_safe(config: Config) -> list[Case]:
    try:
        return collect_cases(config)
    except ConfigError:
        return []


def _collect_bugs_and_triage(
    *,
    config: Config,
    case_name: str,
    job_id: str,
    job_result: dict[str, object],
    signature: dict[str, str] | None,
) -> tuple[list[dict[str, object]], dict[str, object] | None]:
    cases = _load_cases_safe(config)
    case_map = {c.name: c for c in cases}
    bug_index: dict[str, tuple[Case, Bug]] = {}
    for c in cases:
        for rec in c.notes:
            if isinstance(rec, Bug):
                bug_index[f"{c.name}#{rec.id}"] = (c, rec)

    sig_id = signature.get("id") if signature else None
    sig_text = signature.get("text") if signature else None

    refs: list[str] = []
    seen_refs: set[str] = set()

    def _add_ref(ref: str) -> None:
        if ref not in seen_refs:
            seen_refs.add(ref)
            refs.append(ref)

    for raw_ref in job_result.get("known") or ():
        if isinstance(raw_ref, str) and raw_ref:
            _add_ref(raw_ref)

    target_case = case_map.get(case_name)
    if target_case is not None:
        for rec in target_case.notes:
            if isinstance(rec, Bug) and rec.status in ("open", "fixed"):
                _add_ref(f"{target_case.name}#{rec.id}")

    if sig_id or sig_text:
        for c in cases:
            for rec in c.notes:
                if (
                    isinstance(rec, Bug)
                    and rec.status in ("open", "fixed")
                    and rec.signature
                    and rec.signature in (sig_id, sig_text)
                ):
                    _add_ref(f"{c.name}#{rec.id}")

    known_bugs: list[dict[str, object]] = []
    for ref in refs:
        found = bug_index.get(ref)
        if found is None:
            known_bugs.append({"ref": ref, "status": None, "title": None, "owner": None})
        else:
            owner_case, bug = found
            known_bugs.append(
                {
                    "ref": ref,
                    "status": bug.status,
                    "title": bug.title,
                    "owner": bug.owner if bug.owner is not None else owner_case.owner,
                    "seed": bug.seed,
                    "link": bug.link,
                }
            )

    res_triage = job_result.get("triage")
    triage_record: Triage | None = None
    if target_case is not None:
        triage_notes = [r for r in target_case.notes if isinstance(r, Triage)]
        if isinstance(res_triage, dict) and isinstance(res_triage.get("task"), str):
            triage_record = next(
                (r for r in reversed(triage_notes) if r.task == res_triage["task"]),
                None,
            )
        if triage_record is None:
            triage_record = next(
                (r for r in reversed(triage_notes) if job_id in r.judged_status),
                triage_notes[-1] if triage_notes else None,
            )

    last_triage: dict[str, object] | None = None
    if isinstance(res_triage, dict):
        last_triage = dict(res_triage)
        if triage_record is not None:
            last_triage.setdefault("summary", triage_record.summary)
            last_triage.setdefault("evidence", list(triage_record.evidence))
    elif triage_record is not None:
        review_val = (
            str(triage_record.review.get("decision", "pending"))
            if isinstance(triage_record.review, dict)
            else "pending"
        )
        last_triage = {
            "task": triage_record.task,
            "verdict": triage_record.verdict,
            "confidence": triage_record.confidence,
            "summary": triage_record.summary,
            "evidence": list(triage_record.evidence),
            "review": review_val,
            "bugs": list(triage_record.bugs),
            "doc": triage_record.doc,
        }

    return known_bugs, last_triage


def explain_data(
    *,
    config: Config,
    plan: Plan,
    run_id: str,
    job_id: str,
    try_num: int | None = None,
    provider: Provider | None = None,
) -> dict[str, object]:
    """Collect structured 8-section explanation data for ``job_id`` in ``run_id``."""
    root = Path(config.root)
    actual_run_id = resolve_run_id(root, run_id)
    results_data = read_results_json(run_dir(root, actual_run_id) / "results.json")

    plan_job = next((j for j in plan.jobs if j.job_id == job_id), None)
    if plan_job is None:
        all_ids = [j.job_id for j in plan.jobs]
        close = get_close_matches(job_id, all_ids, n=1)
        hint = f"did you mean {close[0]!r}?" if close else None
        raise ConfigError(
            f"job {job_id!r} not found in run {actual_run_id!r}",
            code="JOB_NOT_FOUND",
            hint=hint,
        )

    jobs_list = results_data.get("jobs")
    job_result = (
        next(
            (j for j in jobs_list if isinstance(j, dict) and j.get("job_id") == job_id),
            None,
        )
        if isinstance(jobs_list, list)
        else None
    )
    if job_result is None:
        raise ConfigError(
            f"job {job_id!r} not found in results for run {actual_run_id!r}",
            code="JOB_NOT_FOUND",
            hint="run 'solouvm judge' to update results.json",
        )

    transport = provider.transport if provider is not None else None
    attempts = [a for a in (job_result.get("attempts") or []) if isinstance(a, dict)]
    selected_try, log_rel = _select_attempt(
        root=root,
        plan_job_dir=plan_job.dir,
        job_id=job_id,
        attempts=attempts,
        try_num=try_num,
        transport=transport,
    )

    is_local = transport is None or getattr(transport, "kind", "local") == "local"
    has_root = getattr(transport, "root", None) is not None
    log_arg = (root / log_rel) if (is_local or has_root) else Path(log_rel)

    raw_first = job_result.get("first_error")
    first_error: dict[str, object] | None = dict(raw_first) if isinstance(raw_first, dict) else None
    err_line = first_error.get("line") if first_error else None
    if isinstance(err_line, int) and not isinstance(err_line, bool) and err_line > 0:
        log_context = read_log_context(log_arg, err_line, context=30, transport=transport)
    else:
        log_context = _read_log_tail(log_arg, tail=30, transport=transport)

    snippets = _collect_source_snippets(
        root=root,
        first_error=first_error,
        log_context=log_context,
    )

    raw_sig = job_result.get("signature")
    signature: dict[str, str] | None = (
        {str(k): str(v) for k, v in raw_sig.items() if v is not None}
        if isinstance(raw_sig, dict)
        else None
    )
    sig_id = signature.get("id", "") if signature else ""
    sig_history = find_signature_history(root, sig_id, exclude_run_id=actual_run_id)

    case_name = str(job_result.get("case", plan_job.case))
    raw_seed = job_result.get("seed", plan_job.seed)
    seed = int(raw_seed) if isinstance(raw_seed, int) and not isinstance(raw_seed, bool) else 0

    known_bugs, last_triage = _collect_bugs_and_triage(
        config=config,
        case_name=case_name,
        job_id=job_id,
        job_result=job_result,
        signature=signature,
    )

    prev_pass = find_previous_pass(root, case_name, actual_run_id)
    git_commits = git_commits_since_pass(root, prev_pass)

    raw_repro = job_result.get("repro")
    repro_dict = raw_repro if isinstance(raw_repro, dict) else {}
    make_cmd = (
        str(repro_dict["make"])
        if isinstance(repro_dict.get("make"), str) and repro_dict["make"]
        else (
            f"make -f {shlex.quote(f'.solouvm/runs/{actual_run_id}/run.mk')} "
            f"TRY=r1 WAVE=1 {shlex.quote(f'run/{job_id}')}"
        )
    )
    solouvm_cmd = (
        str(repro_dict["solouvm"])
        if isinstance(repro_dict.get("solouvm"), str) and repro_dict["solouvm"]
        else f"solouvm run --case {shlex.quote(case_name)} --seed {seed} --wave"
    )

    return {
        "run_id": actual_run_id,
        "job_id": job_id,
        "case": case_name,
        "seed": seed,
        "try": selected_try,
        "status": str(job_result.get("status", "")),
        "judged_status": str(job_result.get("judged_status", job_result.get("status", ""))),
        "raw_status": str(job_result.get("raw_status", job_result.get("status", ""))),
        "category": job_result.get("category"),
        "reason": str(job_result.get("reason", "")),
        "flaky": bool(job_result.get("flaky", False)),
        "uvm": dict(job_result["uvm"]) if isinstance(job_result.get("uvm"), dict) else {},
        "waivers": [w for w in (job_result.get("waivers") or []) if isinstance(w, dict)],
        "first_error": first_error,
        "log_path": log_rel,
        "log_context": log_context,
        "snippets": snippets,
        "expect": [e for e in (job_result.get("expect") or []) if isinstance(e, dict)],
        "weak_signals": [str(s) for s in (job_result.get("weak_signals") or [])],
        "signature": signature,
        "signature_history": sig_history,
        "known_bugs": known_bugs,
        "last_triage": last_triage,
        "previous_pass": prev_pass,
        "git_commits": git_commits,
        "repro": {"make": make_cmd, "solouvm": solouvm_cmd},
    }


def explain(
    *,
    config: Config,
    plan: Plan,
    run_id: str,
    job_id: str,
    try_num: int | None = None,
    provider: Provider | None = None,
) -> str:
    """Render the 8-section markdown explanation for ``job_id`` in ``run_id``."""
    data = explain_data(
        config=config,
        plan=plan,
        run_id=run_id,
        job_id=job_id,
        try_num=try_num,
        provider=provider,
    )
    lines: list[str] = [
        f"# Explain: {data['job_id']} (run {data['run_id']})",
        "",
        EXPLAIN_SECTIONS[0],
        f"- Run: `{data['run_id']}`",
        f"- Job: `{data['job_id']}` (case `{data['case']}`, seed `{data['seed']}`"
        + (f", try `{data['try']}`" if data["try"] is not None else "")
        + ")",
        f"- Status: `{data['status']}`"
        + (
            f" (judged=`{data['judged_status']}`, raw=`{data['raw_status']}`)"
            if data["judged_status"] != data["status"] or data["raw_status"] != data["status"]
            else ""
        ),
        f"- Category: `{data['category'] or '-'}`",
        f"- Reason: {data['reason'] or '-'}",
    ]
    if data["flaky"]:
        lines.append("- Flaky: yes")
    uvm = data["uvm"]
    if isinstance(uvm, dict) and uvm:
        lines.append(
            "- UVM counts: "
            f"info={uvm.get('info', 0)}, warning={uvm.get('warning', 0)}, "
            f"error={uvm.get('error', 0)}, fatal={uvm.get('fatal', 0)}"
        )
    waivers = data["waivers"]
    if isinstance(waivers, list) and waivers:
        w_text = ", ".join(f"{w.get('id')} ({w.get('lines')} lines)" for w in waivers)
        lines.append(f"- Waivers applied: {w_text}")

    lines.extend(["", EXPLAIN_SECTIONS[1]])
    first_error = data["first_error"]
    if isinstance(first_error, dict):
        lines.append(f"- First error (line {first_error.get('line')}): `{first_error.get('text')}`")
    else:
        lines.append("- First error: none")
    lines.append(f"- Log: `{data['log_path']}`")
    log_ctx = str(data["log_context"])
    if log_ctx.startswith("log not available:"):
        lines.append(log_ctx)
    else:
        lines.extend(["```text", log_ctx, "```"])

    lines.extend(["", EXPLAIN_SECTIONS[2]])
    snippets = data["snippets"]
    if isinstance(snippets, list) and snippets:
        for item in snippets:
            if not isinstance(item, dict):
                continue
            lines.append(f"### `{item['file']}:{item['line']}`")
            snip = str(item["snippet"])
            if snip.startswith("source not available:"):
                lines.append(snip)
            else:
                lines.extend(["```systemverilog", snip, "```"])
    else:
        lines.append("No source locations found.")

    lines.extend(["", EXPLAIN_SECTIONS[3]])
    expects = data["expect"]
    if isinstance(expects, list) and expects:
        for exp in expects:
            if not isinstance(exp, dict):
                continue
            mark = "PASS" if exp.get("ok") else "FAIL"
            line = f"- [{mark}] {exp.get('rule')} (observed: {exp.get('value')!r})"
            if not exp.get("ok") and exp.get("detail"):
                line += f" - {exp.get('detail')}"
            lines.append(line)
    else:
        lines.append("- Expect checks: none")
    weak = data["weak_signals"]
    if isinstance(weak, list) and weak:
        for sig in weak:
            lines.append(f"- Weak signal: {sig}")
    else:
        lines.append("- Weak signals: none")

    lines.extend(["", EXPLAIN_SECTIONS[4]])
    sig = data["signature"]
    if isinstance(sig, dict):
        lines.append(f"- Signature: `{sig.get('id', '')}` `{sig.get('text', '')}`")
        hist = data["signature_history"]
        if isinstance(hist, list) and hist:
            lines.append("- History with same signature:")
            for h in hist:
                if not isinstance(h, dict):
                    continue
                lines.append(
                    f"  - run `{h.get('run_id')}` job `{h.get('job_id')}` "
                    f"[{h.get('status')}] ({h.get('created_at') or '-'})"
                )
        else:
            lines.append("- History with same signature: none")
    else:
        lines.append("- Signature: none")

    lines.extend(["", EXPLAIN_SECTIONS[5]])
    bugs = data["known_bugs"]
    if isinstance(bugs, list) and bugs:
        for b in bugs:
            if not isinstance(b, dict):
                continue
            if b.get("title"):
                owner = f", owner={b['owner']}" if b.get("owner") else ""
                seed_str = f", seed={b['seed']}" if b.get("seed") is not None else ""
                lines.append(
                    f"- Bug `{b['ref']}` [{b.get('status')}{owner}{seed_str}]: {b['title']}"
                )
            else:
                lines.append(f"- Bug `{b['ref']}`")
    else:
        lines.append("- Related bugs: none")

    triage = data["last_triage"]
    if isinstance(triage, dict):
        summary = f": {triage['summary']}" if triage.get("summary") else ""
        lines.append(
            f"- Last triage (`{triage.get('task')}`): verdict=`{triage.get('verdict')}`, "
            f"confidence=`{triage.get('confidence')}`, review=`{triage.get('review')}`{summary}"
        )
        if triage.get("doc"):
            lines.append(f"  - Doc: `{triage['doc']}`")
    else:
        lines.append("- Last triage: none")

    lines.extend(["", EXPLAIN_SECTIONS[6]])
    prev = data["previous_pass"]
    lines.append(f"- Previous passing run: `{prev}`" if prev else "- Previous passing run: none")
    commits = str(data["git_commits"])
    no_log_statuses = (
        "git unavailable",
        "no previous passing run",
        "no new commits since previous pass",
    )
    if commits in no_log_statuses or commits.startswith(("no git commit", "git commit ")):
        lines.append(f"- Git commits: {commits}")
    else:
        lines.extend(["- Git commits since previous pass:", "```text", commits, "```"])

    lines.extend(["", EXPLAIN_SECTIONS[7]])
    repro = data["repro"]
    assert isinstance(repro, dict)
    lines.extend(
        [
            "```bash",
            str(repro["make"]),
            str(repro["solouvm"]),
            "```",
        ]
    )

    return "\n".join(lines) + "\n"
