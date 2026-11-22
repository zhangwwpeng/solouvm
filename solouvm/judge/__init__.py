"""Judge orchestration: combine build facts, job.facts and logs into ``Result``s (task 18)."""

from __future__ import annotations

import concurrent.futures
import contextlib
import importlib
import os
import pickle
import re
import shlex
import socket
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from solouvm.backends import resolve_sim_backend
from solouvm.collect import collect_cases
from solouvm.errors import RunActiveError
from solouvm.history import (
    case_historical_medians,
    job_flaky_in_history,
    record_run_stats,
)
from solouvm.judge.expect import ExpectResult, apply_expect
from solouvm.judge.facts import FactsError, parse_facts, read_facts
from solouvm.judge.prefilter import extract_keywords, extract_literal, keywords_for
from solouvm.judge.rules import CATEGORIES, Verdict, apply_expect_fail, decide
from solouvm.judge.scan import SEVERITIES, ScanResult, scan
from solouvm.judge.signature import normalize_signature, signature_id
from solouvm.judge.waivers import WaiverResult, apply_waivers, waiver_label
from solouvm.model import Bug, Case, Checks, Expect, Facts, Job, Plan, Result, Triage
from solouvm.providers.transport_local import LocalTransport
from solouvm.report import write_reports
from solouvm.report.json import results_to_payload

if TYPE_CHECKING:
    from solouvm.backends.base import Backend
    from solouvm.config.schema import Config
    from solouvm.providers.base import Provider

__all__ = [
    "CATEGORIES",
    "ExpectResult",
    "FactsError",
    "ScanResult",
    "Verdict",
    "WaiverResult",
    "apply_expect",
    "apply_expect_fail",
    "apply_waivers",
    "build_results_payload",
    "compute_run_hints",
    "decide",
    "extract_keywords",
    "extract_literal",
    "judge_completed_attempt",
    "judge_exit_code",
    "judge_run",
    "keywords_for",
    "normalize_signature",
    "parse_facts",
    "read_facts",
    "scan",
    "signature_id",
    "write_results",
]

_TRY_DIR_RE = re.compile(r"try(\d+)\Z")
_NO_LOG_KINDS = frozenset({"build", "missing", "stale", "schema"})


@dataclass
class _RunMeta:
    run_id: str
    waivers_summary: dict[str, object]
    hints: list[str]
    project: str
    stats_by_case: dict[str, dict[str, list[float | int]]]


class _ResultList(list[Result]):
    """List of ``Result`` carrying run-level metadata without ``id()`` cache hazards."""

    _run_meta: _RunMeta | None = None


@dataclass
class _BuildStatus:
    ok: bool
    facts: Facts | None
    build_key: str
    error_line: int | None


@dataclass
class _AttemptRecord:
    try_num: int
    try_rel: str
    facts: Facts | None
    facts_error: str | None
    is_wave: bool


@dataclass
class _LocalEvalTask:
    log_path: Path | None
    checks: Checks
    backend: Backend
    expects: tuple[Expect, ...]
    facts: Facts | None
    build_ok: bool
    build_facts: Facts | None
    build_key: str
    case: Case
    allow_missing: bool
    run_id: str
    facts_error: str | None
    build_error_line: int | None
    today: date
    partial: bool = False
    stopped_early: bool = False


@dataclass
class _EvaluatedAttempt:
    try_num: int
    try_rel: str
    facts: Facts | None
    facts_error: str | None
    is_wave: bool
    scan_res: ScanResult
    log_read: bool
    raw_verdict: Verdict
    judged_verdict: Verdict


def _empty_scan() -> ScanResult:
    return ScanResult(
        uvm_counts=dict.fromkeys(SEVERITIES, 0),
        uvm_lines={},
        sim_errors=[],
        fail_hits=[],
        pass_hits=[],
        env_fail=None,
        first_error=None,
        last_uvm_id=None,
        info_by_id={},
        sim_end_ns=None,
        line_count=0,
        is_uvm=False,
        has_summary=False,
        test_top_seen=False,
        done_marker=None,
    )


def _resolve_backend(config: Config) -> Backend:
    return resolve_sim_backend(config.simulator, config.sim)


def _transport_path(config: Config, provider: Provider, rel_path: str) -> str:
    if (
        getattr(provider.transport, "kind", "local") == "local"
        and getattr(provider.transport, "root", None) is None
    ):
        return str(Path(config.root) / rel_path)
    return rel_path


def _iter_sparse_lines(pairs: Iterable[tuple[int, str]]) -> Iterator[str]:
    current = 1
    for lineno, text in pairs:
        while current < lineno:
            yield ""
            current += 1
        if current == lineno:
            yield text
            current += 1


def _has_wave_flag(facts_text: str) -> bool:
    for line in facts_text.splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() == "wave" and v.strip().lower() in ("1", "true"):
            return True
    return False


def _should_read_log(
    *,
    build_ok: bool,
    facts: Facts | None,
    facts_error: str | None,
    run_id: str,
) -> bool:
    if not build_ok or facts is None or facts_error is not None:
        return False
    if facts.run_id != run_id or facts.schema != 1:
        return False
    if not str(facts.generator).startswith("solouvm "):
        return False
    return facts.state != "running"


def _eval_local_attempt_worker(task: _LocalEvalTask) -> tuple[ScanResult, Verdict, Verdict, bool]:
    log_read = False
    if task.log_path is not None and task.log_path.is_file():
        with task.log_path.open(encoding="utf-8", errors="replace") as handle:
            scan_res = scan(
                handle,
                checks=task.checks,
                backend=task.backend,
                expects=task.expects,
                verdict_date=task.today,
            )
        log_read = True
    else:
        scan_res = _empty_scan()

    raw_v = decide(
        facts=task.facts,
        scan=scan_res,
        build_ok=task.build_ok,
        build_facts=task.build_facts,
        build_key=task.build_key,
        checks=task.checks,
        case=task.case,
        allow_missing=task.allow_missing,
        run_id=task.run_id,
        facts_error=task.facts_error,
        build_error_line=task.build_error_line,
        today=task.today,
        partial=task.partial,
        stopped_early=task.stopped_early,
    )
    judged_v = apply_expect_fail(raw_v, task.case)
    return scan_res, raw_v, judged_v, log_read


def _eval_remote_attempt(
    *,
    config: Config,
    provider: Provider,
    backend: Backend,
    checks: Checks,
    case: Case,
    build_status: _BuildStatus,
    attempt: _AttemptRecord,
    allow_missing: bool,
    run_id: str,
    today: date,
    partial: bool = False,
    stopped_early: bool = False,
) -> tuple[ScanResult, Verdict, Verdict, bool]:
    log_rel = f"{attempt.try_rel}/sim.log"
    t_log_path = _transport_path(config, provider, log_rel)
    read_log = _should_read_log(
        build_ok=build_status.ok,
        facts=attempt.facts,
        facts_error=attempt.facts_error,
        run_id=run_id,
    )
    log_read = False
    if read_log and provider.transport.exists(t_log_path):
        is_local = getattr(provider.transport, "kind", "local") == "local"
        if not is_local:
            kws = extract_keywords(
                backend=backend,
                checks=checks,
                expects=case.expect,
                today=today,
            )
            line_count_fn = getattr(provider.transport, "line_count", None)
            total_lines = line_count_fn(t_log_path) if callable(line_count_fn) else None
            pairs = provider.transport.read_lines(t_log_path, keywords=kws)
            line_iter = (
                _iter_sparse_lines(pairs) if kws is not None else (text for _, text in pairs)
            )
            scan_res = scan(
                line_iter,
                checks=checks,
                backend=backend,
                expects=case.expect,
                verdict_date=today,
            )
            if isinstance(total_lines, int) and total_lines > scan_res.line_count:
                scan_res = replace(scan_res, line_count=total_lines)
        else:
            pairs = provider.transport.read_lines(t_log_path)
            scan_res = scan(
                (text for _, text in pairs),
                checks=checks,
                backend=backend,
                expects=case.expect,
                verdict_date=today,
            )
        log_read = True
    else:
        scan_res = _empty_scan()

    raw_v = decide(
        facts=attempt.facts,
        scan=scan_res,
        build_ok=build_status.ok,
        build_facts=build_status.facts,
        build_key=build_status.build_key,
        checks=checks,
        case=case,
        allow_missing=allow_missing,
        run_id=run_id,
        facts_error=attempt.facts_error,
        build_error_line=build_status.error_line,
        today=today,
        partial=partial,
        stopped_early=stopped_early,
    )
    judged_v = apply_expect_fail(raw_v, case)
    return scan_res, raw_v, judged_v, log_read


def _resolve_build_statuses(
    *,
    config: Config,
    plan: Plan,
    provider: Provider,
    backend: Backend,
) -> dict[str, _BuildStatus]:
    statuses: dict[str, _BuildStatus] = {}
    modes = {job.build for job in plan.jobs}
    for mode in sorted(modes):
        key = plan.builds.get(mode, mode)
        build_rel = f".solouvm/build/{mode}/{key}"
        build_key_label = f"{mode}/{key}"
        ok_path = _transport_path(config, provider, f"{build_rel}/.ok")
        if provider.transport.exists(ok_path):
            statuses[mode] = _BuildStatus(
                ok=True,
                facts=None,
                build_key=build_key_label,
                error_line=None,
            )
            continue

        build_facts: Facts | None = None
        facts_path = _transport_path(config, provider, f"{build_rel}/job.facts")
        if provider.transport.exists(facts_path):
            try:
                lines = [text for _, text in provider.transport.read_lines(facts_path)]
                build_facts = parse_facts("\n".join(lines))
            except (FactsError, OSError, Exception):
                build_facts = None

        error_line: int | None = None
        if (
            build_facts is not None
            and build_facts.state == "done"
            and build_facts.rc is not None
            and build_facts.rc != 0
        ):
            blog_path = _transport_path(config, provider, f"{build_rel}/build.log")
            if provider.transport.exists(blog_path):
                with contextlib.suppress(Exception):
                    blog_scan = scan(
                        (text for _, text in provider.transport.read_lines(blog_path)),
                        checks=Checks(),
                        backend=backend,
                        verdict_date=date.today(),
                    )
                    if blog_scan.first_error is not None:
                        error_line = blog_scan.first_error[0]

        statuses[mode] = _BuildStatus(
            ok=False,
            facts=build_facts,
            build_key=build_key_label,
            error_line=error_line,
        )
    return statuses


def _match_job_id(
    parts: tuple[str, ...],
    attempts_by_job: dict[str, dict[int, _AttemptRecord]],
    norm_dir_to_jid: dict[str, str],
) -> str | None:
    if len(parts) < 3:
        return None
    jid = parts[-3]
    if jid in attempts_by_job:
        return jid
    parent_posix = "/".join(parts[:-2])
    return next(
        (
            cand_jid
            for d, cand_jid in norm_dir_to_jid.items()
            if parent_posix == d or parent_posix.endswith(f"/{d}")
        ),
        None,
    )


def _discover_attempts(
    *,
    config: Config,
    plan: Plan,
    provider: Provider,
) -> dict[str, list[_AttemptRecord]]:
    root = Path(config.root)
    raw_facts: dict[str, str] = {}
    wave_files: set[tuple[str, str]] = set()
    attempts_by_job: dict[str, dict[int, _AttemptRecord]] = {j.job_id: {} for j in plan.jobs}
    job_dir_by_id = {j.job_id: j.dir for j in plan.jobs}
    norm_dir_to_jid = {j.dir.strip("/"): j.job_id for j in plan.jobs}

    patterns = [f".solouvm/runs/{plan.run_id}/*/try*/job.facts"]
    wave_patterns = [f".solouvm/runs/{plan.run_id}/*/try*/.wave"]
    default_prefix = f".solouvm/runs/{plan.run_id}/"
    for job in plan.jobs:
        if not job.dir.startswith(default_prefix):
            patterns.append(f"{job.dir}/try*/job.facts")
            wave_patterns.append(f"{job.dir}/try*/.wave")

    for pat in dict.fromkeys(patterns):
        raw_facts.update(provider.transport.read_many(_transport_path(config, provider, pat)))

    for pat in dict.fromkeys(wave_patterns):
        for wpath in provider.transport.read_many(_transport_path(config, provider, pat)):
            parts = PurePosixPath(wpath.replace("\\", "/")).parts
            matched = _match_job_id(parts, attempts_by_job, norm_dir_to_jid)
            if matched is not None:
                wave_files.add((matched, parts[-2]))

    is_local = getattr(provider.transport, "kind", "local") == "local"

    for path_str, content in sorted(raw_facts.items()):
        parts = PurePosixPath(path_str.replace("\\", "/")).parts
        jid = _match_job_id(parts, attempts_by_job, norm_dir_to_jid)
        if jid is None:
            continue
        try_name = parts[-2]
        match = _TRY_DIR_RE.fullmatch(try_name)
        if not match:
            continue
        try_num = int(match.group(1))
        if try_num <= 0:
            continue
        try_rel = f"{job_dir_by_id[jid]}/{try_name}"
        is_wave = (jid, try_name) in wave_files or _has_wave_flag(content)
        if not is_wave and is_local and (root / try_rel / ".wave").exists():
            is_wave = True

        try:
            facts: Facts | None = parse_facts(content)
            facts_error: str | None = None
        except FactsError as exc:
            facts = None
            facts_error = str(exc)

        attempts_by_job[jid][try_num] = _AttemptRecord(
            try_num=try_num,
            try_rel=try_rel,
            facts=facts,
            facts_error=facts_error,
            is_wave=is_wave,
        )

    return {jid: [by_num[n] for n in sorted(by_num)] for jid, by_num in attempts_by_job.items()}


def _find_plugin_judge(config: Config) -> Callable[[Result, Case], tuple[str, str] | None] | None:
    if not config.plugins:
        return None
    root_str = str(Path(config.root).resolve())
    for spec in config.plugins:
        if ":" in spec:
            mod_name, attr_name = spec.split(":", 1)
        elif "." in spec:
            mod_name, attr_name = spec.rsplit(".", 1)
        else:
            continue
        sys.path.insert(0, root_str)
        try:
            module = importlib.import_module(mod_name)
            obj = getattr(module, attr_name, None)
        except Exception:
            continue
        finally:
            with contextlib.suppress(ValueError):
                sys.path.remove(root_str)
        if callable(obj) and not isinstance(obj, type):
            return obj  # type: ignore[return-value]
        if isinstance(obj, type) and callable(getattr(obj, "judge", None)):
            inst = obj()
            return inst.judge  # type: ignore[no-any-return]
    return None


def _build_uvm_dict(scan_res: ScanResult, waived: WaiverResult | None) -> dict[str, object]:
    waived_map: dict[str, int] = {
        "error": waived.waived.get("ERROR", 0) if waived is not None else 0,
        "warning": waived.waived.get("WARNING", 0) if waived is not None else 0,
    }
    if waived is not None:
        if waived.waived.get("FATAL", 0):
            waived_map["fatal"] = waived.waived["FATAL"]
        if waived.waived.get("SIM", 0):
            waived_map["sim"] = waived.waived["SIM"]
    return {
        "info": scan_res.uvm_counts.get("INFO", 0),
        "warning": scan_res.uvm_counts.get("WARNING", 0),
        "error": scan_res.uvm_counts.get("ERROR", 0),
        "fatal": scan_res.uvm_counts.get("FATAL", 0),
        "waived": waived_map,
    }


def _expect_to_dict(item: ExpectResult) -> dict[str, object]:
    out: dict[str, object] = {
        "rule": item.rule,
        "ok": item.ok,
        "value": item.value,
    }
    if not item.ok and item.detail:
        out["detail"] = item.detail
    return out


def _signature_dict(
    verdict: Verdict,
    scan_res: ScanResult,
    log_read: bool,
) -> dict[str, str] | None:
    if verdict.signature is None:
        if verdict.status == "xpass":
            return {"id": "xpass", "text": "xpass"}
        return None
    if verdict.first_error is not None:
        text = normalize_signature(verdict.first_error[1])
    elif log_read and scan_res.last_uvm_id and "|" in verdict.signature:
        text = verdict.signature
    else:
        text = verdict.signature
    return {"id": verdict.signature, "text": text}


def _weak_signals_for(
    *,
    config: Config,
    case: Case,
    scan_res: ScanResult,
    judged_status: str,
    log_read: bool,
    medians: tuple[float | None, float | None],
) -> list[str]:
    if not config.triage.weak_signals or judged_status != "pass" or not log_read:
        return []
    signals: list[str] = []
    med_end_ns, med_lines = medians
    if scan_res.sim_end_ns == 0:
        signals.append("sim time 0ns")
    elif (
        med_end_ns is not None
        and med_end_ns > 0
        and scan_res.sim_end_ns is not None
        and scan_res.sim_end_ns < 0.1 * med_end_ns
    ):
        signals.append(
            f"sim time {scan_res.sim_end_ns:g}ns < 10% of historical median {med_end_ns:g}ns"
        )
    if scan_res.is_uvm and not scan_res.test_top_seen:
        signals.append("no UVM messages from uvm_test_top")
    if med_lines is not None and med_lines > 0 and scan_res.line_count < 0.1 * med_lines:
        signals.append(f"log lines {scan_res.line_count} < 10% of historical median {med_lines:g}")
    if not case.expect:
        signals.append("case has no 'expect'")
    return signals


def _known_bugs_for(
    *,
    case: Case,
    cases_list: Sequence[Case],
    judged_status: str,
    signature: dict[str, str] | None,
) -> list[str]:
    if judged_status in ("pass", "xfail", "skip"):
        return []
    sig_id = signature.get("id") if signature else None
    sig_text = signature.get("text") if signature else None
    known: list[str] = []
    seen: set[str] = set()

    for record in case.notes:
        if isinstance(record, Bug) and record.status in ("open", "fixed"):
            label = f"{case.name}#{record.id}"
            if label not in seen:
                seen.add(label)
                known.append(label)

    if sig_id or sig_text:
        for other_case in cases_list:
            for record in other_case.notes:
                if (
                    isinstance(record, Bug)
                    and record.status in ("open", "fixed")
                    and record.signature
                    and record.signature in (sig_id, sig_text)
                ):
                    label = f"{other_case.name}#{record.id}"
                    if label not in seen:
                        seen.add(label)
                        known.append(label)

    return known


def _triage_run_id(task: str) -> str | None:
    run_part, sep, seq_part = task.rpartition("/")
    if sep and run_part and seq_part:
        return run_part
    return None


def _triage_for(
    case: Case,
    job_id: str,
    judged_status: str,
    run_id: str,
) -> tuple[dict[str, object] | None, str, str | None]:
    for record in reversed(case.notes):
        if (
            isinstance(record, Triage)
            and _triage_run_id(record.task) == run_id
            and job_id in record.judged_status
        ):
            stale = record.judged_status[job_id] != judged_status
            review_val = (
                str(record.review.get("decision", "pending"))
                if isinstance(record.review, dict)
                else "pending"
            )
            triage_dict: dict[str, object] = {
                "task": record.task,
                "verdict": record.verdict,
                "confidence": record.confidence,
                "review": review_val,
                "stale": stale,
                "bugs": list(record.bugs),
                "doc": record.doc,
            }
            if review_val == "accepted" and not stale:
                if judged_status == "pass" and record.verdict == "false_pass":
                    return triage_dict, "false_pass", "design"
                if judged_status not in ("pass", "xfail", "skip") and record.verdict == "env_issue":
                    return triage_dict, "error", "environment"
            return triage_dict, judged_status, CATEGORIES.get(judged_status)
    return None, judged_status, CATEGORIES.get(judged_status)


def _compute_waivers_summary(
    *,
    config: Config,
    cases_list: Sequence[Case],
    used_totals: dict[str, int],
    today: date,
) -> dict[str, object]:
    all_waivers = list(config.checks.waivers)
    for case in cases_list:
        if case.checks:
            eff = Checks.from_dict(case.checks, config.checks)
            all_waivers.extend(eff.waivers)

    active_order: list[str] = []
    active_seen: set[str] = set()
    expired_order: list[str] = []
    expired_seen: set[str] = set()

    for waiver in all_waivers:
        label = waiver_label(waiver)
        if waiver.until is not None and date.fromisoformat(waiver.until) < today:
            if label not in expired_seen:
                expired_seen.add(label)
                expired_order.append(label)
        elif label not in active_seen:
            active_seen.add(label)
            active_order.append(label)

    used: list[dict[str, object]] = []
    unused: list[str] = []
    for label in active_order:
        count = used_totals.get(label, 0)
        if count > 0:
            used.append({"id": label, "lines": count})
        else:
            unused.append(label)
    for label, count in used_totals.items():
        if label not in active_seen and count > 0:
            used.append({"id": label, "lines": count})

    return {
        "used": used,
        "unused": unused,
        "expired": expired_order,
    }


def compute_run_hints(
    config: Config | None,
    cases: Sequence[Case],
    results: Sequence[Result],
) -> list[str]:
    """Compute M-13 / M-22 record consistency hints from results and case bugs."""
    _ = config
    by_case_seed: dict[tuple[str, int], list[Result]] = {}
    for res in results:
        by_case_seed.setdefault((res.case, res.seed), []).append(res)

    hints: list[str] = []
    seen: set[str] = set()
    for case in cases:
        for record in case.notes:
            if not isinstance(record, Bug) or record.seed is None:
                continue
            matching = by_case_seed.get((case.name, record.seed), [])
            if not matching:
                continue
            all_passed = all(r.status == "pass" for r in matching)
            failed = next((r for r in matching if r.status not in ("pass", "xfail", "skip")), None)
            msg: str | None = None
            if record.status == "open" and all_passed:
                msg = f"{case.name}#{record.id} may be fixed (seed {record.seed} passed)"
            elif record.status == "fixed" and all_passed:
                msg = f"{case.name}#{record.id} can be marked verified (seed {record.seed} passed)"
            elif record.status == "verified" and failed is not None:
                msg = (
                    f"regression? {case.name}#{record.id} was verified "
                    f"(seed {record.seed} {failed.status})"
                )
            if msg is not None and msg not in seen:
                seen.add(msg)
                hints.append(msg)
    if any(
        res.status == "fail" and res.reason == "missing SOLOUVM_DONE marker" for res in results
    ):
        marker_hint = "non-UVM testbench? print 'SOLOUVM_DONE status=pass' before $finish"
        if marker_hint not in seen:
            seen.add(marker_hint)
            hints.append(marker_hint)
    return hints


def _local_hostnames() -> set[str]:
    try:
        full = socket.gethostname()
    except OSError:
        return set()
    short = full.split(".", 1)[0]
    return {full, short} - {""}


def _local_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _get_run_meta(results: Sequence[Result], run_id: str) -> _RunMeta | None:
    meta = getattr(results, "_run_meta", None)
    if isinstance(meta, _RunMeta) and meta.run_id == run_id:
        return meta
    return None


def judge_completed_attempt(
    *,
    config: Config,
    plan: Plan,
    provider: Provider,
    job: Job,
    case: Case,
    facts: Facts,
    backend: Backend | None = None,
    today: date | None = None,
) -> Verdict:
    """Mechanically judge a single finished job attempt during live regression monitoring."""
    eff_backend = backend if backend is not None else _resolve_backend(config)
    checks = Checks.from_dict(case.checks, config.checks) if case.checks else config.checks
    build_key = f"{job.build}/{plan.builds.get(job.build, job.build)}"
    bstatus = _BuildStatus(ok=True, facts=None, build_key=build_key, error_line=None)
    attempt = _AttemptRecord(
        try_num=facts.try_,
        try_rel=f"{job.dir}/try{facts.try_}",
        facts=facts,
        facts_error=None,
        is_wave=False,
    )
    _, _, judged_v, _ = _eval_remote_attempt(
        config=config,
        provider=provider,
        backend=eff_backend,
        checks=checks,
        case=case,
        build_status=bstatus,
        attempt=attempt,
        allow_missing=False,
        run_id=plan.run_id,
        today=today or date.today(),
    )
    return judged_v


def judge_run(
    *,
    config: Config,
    plan: Plan,
    provider: Provider,
    cases: list[Case] | None = None,
    allow_missing: bool = False,
    partial: bool = False,
    stopped_early: bool = False,
    run_finished: bool = False,
    custom_judge: Callable[[Result, Case], tuple[str, str] | None] | None = None,
    today: date | None = None,
) -> list[Result]:
    """Judge every job in ``plan`` in order and return ``Result`` objects."""
    ref_date = today or date.today()
    root = Path(config.root)
    cases_list = collect_cases(config) if cases is None else list(cases)
    case_map = {c.name: c for c in cases_list}
    backend = _resolve_backend(config)

    build_statuses = _resolve_build_statuses(
        config=config,
        plan=plan,
        provider=provider,
        backend=backend,
    )
    attempts_by_job = _discover_attempts(config=config, plan=plan, provider=provider)

    if not partial and not run_finished:
        if any(
            (status.facts is not None and status.facts.state == "running")
            or provider.transport.exists(
                _transport_path(config, provider, f".solouvm/build/{status.build_key}.lock")
            )
            for status in build_statuses.values()
        ):
            raise RunActiveError(
                f"run {plan.run_id} is still compiling",
                hint="pass --partial to take a snapshot or wait for compilation",
            )
        done_marker = _transport_path(config, provider, f".solouvm/runs/{plan.run_id}/launch.done")
        launch_done = provider.transport.exists(done_marker)
        if not launch_done:
            is_local_tp = type(provider.transport) is LocalTransport
            local_hosts = _local_hostnames() if is_local_tp else set()
            for job_attempts in attempts_by_job.values():
                for att in job_attempts:
                    if att.facts is not None and att.facts.state == "running":
                        if (
                            is_local_tp
                            and att.facts.host in local_hosts
                            and att.facts.pid > 0
                            and not _local_pid_alive(att.facts.pid)
                        ):
                            continue
                        raise RunActiveError(
                            f"run {plan.run_id} is still active",
                            code="RUN_ACTIVE",
                            hint=(
                                "pass --partial to judge finished jobs, "
                                "or wait / run 'solouvm stop'"
                            ),
                        )

    job_cases: dict[str, Case] = {}
    job_checks: dict[str, Checks] = {}
    for job in plan.jobs:
        case = case_map.get(job.case)
        if case is None:
            case = Case(
                name=job.case,
                uvm_test=job.uvm_test,
                expect_fail=job.expect_fail,
                tags=job.tags,
            )
        job_cases[job.job_id] = case
        job_checks[job.job_id] = (
            Checks.from_dict(case.checks, config.checks) if case.checks else config.checks
        )

    use_local_pool = type(provider.transport) is LocalTransport
    eval_results: dict[tuple[str, int], _EvaluatedAttempt] = {}

    if use_local_pool:
        task_keys: list[tuple[str, int]] = []
        task_objs: list[_LocalEvalTask] = []
        task_records: list[_AttemptRecord] = []
        for job in plan.jobs:
            case = job_cases[job.job_id]
            checks = job_checks[job.job_id]
            bstatus = build_statuses[job.build]
            for att in attempts_by_job[job.job_id]:
                read_log = _should_read_log(
                    build_ok=bstatus.ok,
                    facts=att.facts,
                    facts_error=att.facts_error,
                    run_id=plan.run_id,
                )
                log_path = (root / att.try_rel / "sim.log") if read_log else None
                task_keys.append((job.job_id, att.try_num))
                task_records.append(att)
                task_objs.append(
                    _LocalEvalTask(
                        log_path=log_path,
                        checks=checks,
                        backend=backend,
                        expects=tuple(case.expect),
                        facts=att.facts,
                        build_ok=bstatus.ok,
                        build_facts=bstatus.facts,
                        build_key=bstatus.build_key,
                        case=case,
                        allow_missing=allow_missing,
                        run_id=plan.run_id,
                        facts_error=att.facts_error,
                        build_error_line=bstatus.error_line,
                        today=ref_date,
                        partial=partial,
                        stopped_early=stopped_early,
                    )
                )

        outputs: list[tuple[ScanResult, Verdict, Verdict, bool]]
        if len(task_objs) >= 2:
            try:
                with concurrent.futures.ProcessPoolExecutor() as executor:
                    outputs = list(executor.map(_eval_local_attempt_worker, task_objs))
            except (OSError, PermissionError, pickle.PicklingError, BrokenProcessPool):
                outputs = [_eval_local_attempt_worker(t) for t in task_objs]
        else:
            outputs = [_eval_local_attempt_worker(t) for t in task_objs]

        for key, att, (scan_res, raw_v, judged_v, log_read) in zip(
            task_keys, task_records, outputs, strict=True
        ):
            eval_results[key] = _EvaluatedAttempt(
                try_num=att.try_num,
                try_rel=att.try_rel,
                facts=att.facts,
                facts_error=att.facts_error,
                is_wave=att.is_wave,
                scan_res=scan_res,
                log_read=log_read,
                raw_verdict=raw_v,
                judged_verdict=judged_v,
            )
    else:
        for job in plan.jobs:
            case = job_cases[job.job_id]
            checks = job_checks[job.job_id]
            bstatus = build_statuses[job.build]
            for att in attempts_by_job[job.job_id]:
                scan_res, raw_v, judged_v, log_read = _eval_remote_attempt(
                    config=config,
                    provider=provider,
                    backend=backend,
                    checks=checks,
                    case=case,
                    build_status=bstatus,
                    attempt=att,
                    allow_missing=allow_missing,
                    run_id=plan.run_id,
                    today=ref_date,
                    partial=partial,
                    stopped_early=stopped_early,
                )
                eval_results[(job.job_id, att.try_num)] = _EvaluatedAttempt(
                    try_num=att.try_num,
                    try_rel=att.try_rel,
                    facts=att.facts,
                    facts_error=att.facts_error,
                    is_wave=att.is_wave,
                    scan_res=scan_res,
                    log_read=log_read,
                    raw_verdict=raw_v,
                    judged_verdict=judged_v,
                )

    judge_hook = custom_judge if custom_judge is not None else _find_plugin_judge(config)
    medians_cache: dict[str, tuple[float | None, float | None]] = {}
    used_waiver_totals: dict[str, int] = {}
    stats_by_case: dict[str, dict[str, list[float | int]]] = {}
    results: _ResultList = _ResultList()

    for job in plan.jobs:
        case = job_cases[job.job_id]
        checks = job_checks[job.job_id]
        bstatus = build_statuses[job.build]
        all_evals = [eval_results[(job.job_id, att.try_num)] for att in attempts_by_job[job.job_id]]
        non_wave = [ev for ev in all_evals if not ev.is_wave]

        attempts_out: list[dict[str, object]] = [
            {
                "try": ev.try_num,
                "status": ev.judged_verdict.status,
                "reason": ev.judged_verdict.reason,
                "duration_s": (
                    round(ev.facts.duration_s, 3)
                    if ev.facts is not None and ev.facts.duration_s is not None
                    else None
                ),
                "host": ev.facts.host if ev.facts is not None else None,
                "log": f"{ev.try_rel}/sim.log",
                "wave": ev.is_wave,
            }
            for ev in all_evals
        ]

        if non_wave:
            passing_ev = next((ev for ev in non_wave if ev.judged_verdict.status == "pass"), None)
            chosen = passing_ev if passing_ev is not None else non_wave[-1]
            retry_flaky = passing_ev is not None and len(non_wave) > 1
            raw_v = chosen.raw_verdict
            judged_v = chosen.judged_verdict
            scan_res = chosen.scan_res
            log_read = chosen.log_read
        else:
            retry_flaky = False
            scan_res = _empty_scan()
            log_read = False
            raw_v = decide(
                facts=None,
                scan=scan_res,
                build_ok=bstatus.ok,
                build_facts=bstatus.facts,
                build_key=bstatus.build_key,
                checks=checks,
                case=case,
                allow_missing=allow_missing,
                run_id=plan.run_id,
                build_error_line=bstatus.error_line,
                today=ref_date,
                partial=partial,
                stopped_early=stopped_early,
            )
            judged_v = apply_expect_fail(raw_v, case)

        first_err_dict = (
            {"line": judged_v.first_error[0], "text": judged_v.first_error[1]}
            if judged_v.first_error is not None
            else None
        )
        sig_dict = _signature_dict(judged_v, scan_res, log_read)
        uvm_dict = _build_uvm_dict(scan_res, judged_v.waived) if log_read else {}
        waivers_list: list[dict[str, object]] = []
        if log_read and judged_v.waived is not None:
            for item in judged_v.waived.used:
                lines_cnt = int(item.get("lines", 0) or 0)
                if lines_cnt > 0:
                    wid = str(item["id"])
                    waivers_list.append({"id": wid, "lines": lines_cnt})
                    used_waiver_totals[wid] = used_waiver_totals.get(wid, 0) + lines_cnt

        expect_list = [_expect_to_dict(e) for e in (judged_v.expects or ())] if log_read else []

        history_flaky = job_flaky_in_history(
            root,
            job.job_id,
            judged_v.status,
            exclude_run_id=plan.run_id,
        )
        flaky = retry_flaky or history_flaky

        repro = {
            "make": (
                f"make -f {shlex.quote(f'.solouvm/runs/{plan.run_id}/run.mk')} "
                f"TRY=r1 WAVE=1 {shlex.quote(f'run/{job.job_id}')}"
            ),
            "solouvm": f"solouvm run --case {shlex.quote(job.case)} --seed {job.seed} --wave",
        }

        res = Result(
            job_id=job.job_id,
            case=job.case,
            seed=job.seed,
            status=judged_v.status,
            judged_status=judged_v.status,
            raw_status=raw_v.status,
            category=judged_v.category,
            reason=judged_v.reason,
            first_error=first_err_dict,
            signature=sig_dict,
            uvm=uvm_dict,
            waivers=waivers_list,
            expect=expect_list,
            weak_signals=[],
            known=[],
            triage=None,
            flaky=flaky,
            attempts=attempts_out,
            repro=repro,
            chosen_try=chosen.try_num if non_wave else None,
        )

        if judge_hook is not None:
            override = judge_hook(res, case)
            if override is not None:
                new_status, new_reason = override
                if res.judged_status in ("pass", "xfail") and new_status not in (
                    "pass",
                    "xfail",
                    "skip",
                ):
                    res.judged_status = new_status
                    res.status = new_status
                    res.category = CATEGORIES.get(new_status, "design")
                    if new_reason:
                        res.reason = new_reason
                    if res.signature is None:
                        norm = normalize_signature(res.reason)
                        res.signature = {"id": signature_id(norm), "text": norm}

        if case.name not in medians_cache:
            medians_cache[case.name] = case_historical_medians(
                root, case.name, exclude_run_id=plan.run_id
            )
        res.weak_signals = _weak_signals_for(
            config=config,
            case=case,
            scan_res=scan_res,
            judged_status=res.judged_status,
            log_read=log_read,
            medians=medians_cache[case.name],
        )
        res.known = _known_bugs_for(
            case=case,
            cases_list=cases_list,
            judged_status=res.judged_status,
            signature=res.signature,
        )
        triage_dict, final_status, final_category = _triage_for(
            case, job.job_id, res.judged_status, plan.run_id
        )
        res.triage = triage_dict
        res.status = final_status
        res.category = final_category

        if res.status == "pass" and log_read:
            cstats = stats_by_case.setdefault(case.name, {"sim_end_ns": [], "log_lines": []})
            if scan_res.sim_end_ns is not None:
                cstats["sim_end_ns"].append(scan_res.sim_end_ns)
            cstats["log_lines"].append(scan_res.line_count)

        results.append(res)

    waivers_summary = _compute_waivers_summary(
        config=config,
        cases_list=list(job_cases.values()) if job_cases else cases_list,
        used_totals=used_waiver_totals,
        today=ref_date,
    )
    hints = compute_run_hints(config, cases_list, results)
    results._run_meta = _RunMeta(
        run_id=plan.run_id,
        waivers_summary=waivers_summary,
        hints=hints,
        project=config.project,
        stats_by_case=stats_by_case,
    )
    return results


def build_results_payload(
    *,
    config: Config,
    plan: Plan,
    results: list[Result],
    cases: list[Case],
    waivers_summary: dict[str, object] | None = None,
    partial: bool,
    judged_at: datetime,
) -> dict[str, object]:
    """Build the canonical ``results.json`` payload from judged ``Result`` objects."""
    cached = _get_run_meta(results, plan.run_id)
    effective_waivers = waivers_summary
    if effective_waivers is None:
        if cached is not None:
            effective_waivers = cached.waivers_summary
        else:
            totals: dict[str, int] = {}
            for res in results:
                for item in res.waivers:
                    wid = str(item.get("id", ""))
                    lines_cnt = item.get("lines", 0)
                    if wid and isinstance(lines_cnt, int) and lines_cnt > 0:
                        totals[wid] = totals.get(wid, 0) + lines_cnt
            effective_waivers = _compute_waivers_summary(
                config=config,
                cases_list=cases,
                used_totals=totals,
                today=judged_at.date(),
            )
    hints = cached.hints if cached is not None else compute_run_hints(config, cases, results)
    return results_to_payload(
        results,
        run_id=plan.run_id,
        partial=partial,
        judged_at=judged_at,
        waivers_summary=effective_waivers,
        hints=hints,
    )


def write_results(
    results_dir: Path,
    results: list[Result],
    *,
    run_id: str,
    partial: bool,
    judged_at: datetime,
    config: Config | None = None,
    cases: list[Case] | None = None,
    project: str | None = None,
    waivers_summary: dict[str, object] | None = None,
) -> dict[str, object]:
    """Write ``results.json``, ``results.txt`` and ``junit.xml`` into ``results_dir``."""
    results_dir = Path(results_dir)
    cached = _get_run_meta(results, run_id)

    eff_waivers = waivers_summary
    eff_hints: list[str] = []
    eff_project = project or (config.project if config is not None else None)

    if config is not None:
        cases_list = collect_cases(config) if cases is None else list(cases)
        if eff_waivers is None:
            if cached is not None:
                eff_waivers = cached.waivers_summary
            else:
                totals: dict[str, int] = {}
                for res in results:
                    for item in res.waivers:
                        wid = str(item.get("id", ""))
                        lines_cnt = item.get("lines", 0)
                        if wid and isinstance(lines_cnt, int) and lines_cnt > 0:
                            totals[wid] = totals.get(wid, 0) + lines_cnt
                eff_waivers = _compute_waivers_summary(
                    config=config,
                    cases_list=cases_list,
                    used_totals=totals,
                    today=judged_at.date(),
                )
        eff_hints = (
            cached.hints if cached is not None else compute_run_hints(config, cases_list, results)
        )
    elif cached is not None:
        if eff_waivers is None:
            eff_waivers = cached.waivers_summary
        eff_hints = cached.hints
        if eff_project is None:
            eff_project = cached.project

    payload = results_to_payload(
        results,
        run_id=run_id,
        partial=partial,
        judged_at=judged_at,
        waivers_summary=eff_waivers,
        hints=eff_hints,
    )
    write_reports(
        results_dir,
        payload,
        formats=("json", "text", "junit"),
        project=eff_project or "solouvm",
    )

    if cached is not None and cached.stats_by_case:
        stats_root: Path | None = None
        if config is not None:
            stats_root = Path(config.root)
        elif results_dir.parent.name == "runs" and results_dir.parent.parent.name == ".solouvm":
            stats_root = results_dir.parent.parent.parent
        if stats_root is not None:
            with contextlib.suppress(OSError):
                record_run_stats(stats_root, run_id, cached.stats_by_case)

    return payload


def judge_exit_code(results: list[Result]) -> int:
    """Return exit code per verdict.md §12: 2 on build failure, 1 on any non-pass, else 0."""
    has_non_pass = False
    for res in results:
        if res.status == "error" and res.reason.startswith(("build failed", "build not run")):
            return 2
        if res.status not in ("pass", "xfail", "skip"):
            has_non_pass = True
    return 1 if has_non_pass else 0
