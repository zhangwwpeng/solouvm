"""Plain-text report derived from ``results.json`` (K-01, K-03, K-05, K-06, K-11, K-12)."""

from __future__ import annotations

from solouvm.report.json import (
    CATEGORY_ORDER,
    PASS_STATUSES,
    STATUS_ORDER,
    job_duration,
    validate_results_payload,
)

__all__ = ["render_text"]


def _last_log(job: dict[str, object]) -> str | None:
    attempts = job.get("attempts")
    if not isinstance(attempts, list) or not attempts:
        return None
    non_wave = [a for a in attempts if isinstance(a, dict) and not a.get("wave")]
    chosen = job.get("chosen_try")
    target = next(
        (attempt for attempt in non_wave if attempt.get("try") == chosen),
        non_wave[-1] if non_wave else attempts[-1],
    )
    if isinstance(target, dict) and isinstance(target.get("log"), str):
        return target["log"]
    return None


def render_text(
    payload: dict[str, object],
    *,
    compare: dict[str, object] | None = None,
    slow_n: int = 5,
) -> str:
    """Render ``results.txt`` strictly from the ``results.json`` payload."""
    validate_results_payload(payload)
    run_id = str(payload.get("run_id", ""))
    judged_at = str(payload.get("judged_at", ""))
    partial = bool(payload.get("partial", False))
    summary = payload.get("summary")
    summary_dict = summary if isinstance(summary, dict) else {}

    total = int(summary_dict.get("total", 0) or 0)
    passed = int(summary_dict.get("passed", 0) or 0)
    duration_s = float(summary_dict.get("duration_s", 0.0) or 0.0)
    by_status = summary_dict.get("by_status")
    by_status_dict = by_status if isinstance(by_status, dict) else {}
    by_category = summary_dict.get("by_category")
    by_category_dict = by_category if isinstance(by_category, dict) else {}

    lines: list[str] = []
    partial_tag = " [PARTIAL]" if partial else ""
    lines.append(f"Run: {run_id}{partial_tag}  (judged {judged_at})")
    pct = f" ({100.0 * passed / total:.1f}%)" if total else ""
    lines.append(f"Summary: {passed}/{total} passed{pct}  duration={duration_s:.3f}s")

    status_parts = [f"{s}={int(by_status_dict.get(s, 0) or 0)}" for s in STATUS_ORDER]
    lines.append(f"Status: {'  '.join(status_parts)}")
    cat_parts = [f"{c}={int(by_category_dict.get(c, 0) or 0)}" for c in CATEGORY_ORDER]
    lines.append(f"Categories: {'  '.join(cat_parts)}")

    jobs = [j for j in (payload.get("jobs") or []) if isinstance(j, dict)]
    failures = [j for j in jobs if str(j.get("status", "")) not in PASS_STATUSES]
    if failures:
        lines.append("")
        lines.append(f"Failures ({len(failures)}):")
        for job in failures:
            jid = job.get("job_id", "")
            case = job.get("case", "")
            seed = job.get("seed", "")
            status = job.get("status", "")
            cat = job.get("category") or "-"
            reason = job.get("reason", "")
            flaky_tag = " [flaky]" if job.get("flaky") else ""
            lines.append(
                f"  - {jid} [{status}/{cat}]{flaky_tag} (case={case}, seed={seed}): {reason}"
            )
            log_path = _last_log(job)
            if log_path:
                lines.append(f"    log: {log_path}")
            first_err = job.get("first_error")
            if isinstance(first_err, dict):
                lines.append(
                    f"    first_error: line {first_err.get('line')}: {first_err.get('text')}"
                )
            known = job.get("known")
            if isinstance(known, list) and known:
                lines.append(f"    known: {', '.join(str(k) for k in known)}")
            triage = job.get("triage")
            if isinstance(triage, dict):
                stale_tag = " (stale)" if triage.get("stale") else ""
                lines.append(
                    f"    triage: {triage.get('task')} {triage.get('verdict')} "
                    f"[{triage.get('review')}]{stale_tag}"
                )
            repro = job.get("repro")
            if isinstance(repro, dict):
                if repro.get("make"):
                    lines.append(f"    repro (make): {repro['make']}")
                if repro.get("solouvm"):
                    lines.append(f"    repro (solouvm): {repro['solouvm']}")

    signatures = [s for s in (payload.get("signatures") or []) if isinstance(s, dict)]
    if signatures:
        lines.append("")
        lines.append(f"Signatures ({len(signatures)}):")
        for sig in signatures:
            cases = ", ".join(str(c) for c in (sig.get("cases") or []))
            lines.append(
                f"  - {sig.get('id')} (x{sig.get('count')}, cases=[{cases}], "
                f"example={sig.get('example')}): {sig.get('text')}"
            )

    waivers = payload.get("waivers")
    if isinstance(waivers, dict):
        used = waivers.get("used") or []
        unused = waivers.get("unused") or []
        expired = waivers.get("expired") or []
        if used or unused or expired:
            lines.append("")
            lines.append("Waivers:")
            if used:
                used_str = ", ".join(
                    f"{w.get('id')} ({w.get('lines')} lines)" for w in used if isinstance(w, dict)
                )
                lines.append(f"  used: {used_str}")
            if unused:
                lines.append(f"  unused: {', '.join(str(u) for u in unused)}")
            if expired:
                lines.append(f"  expired: {', '.join(str(e) for e in expired)}")

    weak_jobs = [
        j for j in jobs if isinstance(j.get("weak_signals"), list) and j.get("weak_signals")
    ]
    if weak_jobs:
        lines.append("")
        lines.append(f"Weak signals ({len(weak_jobs)}):")
        for job in weak_jobs:
            sigs = "; ".join(str(s) for s in job["weak_signals"])  # type: ignore[attr-defined]
            lines.append(f"  - {job.get('job_id')}: {sigs}")

    if jobs and slow_n > 0:
        ranked = sorted(
            jobs,
            key=lambda j: (-job_duration(j), str(j.get("job_id", ""))),
        )[:slow_n]
        lines.append("")
        lines.append(f"Slowest jobs (top {len(ranked)}):")
        for job in ranked:
            dur = job_duration(job)
            lines.append(f"  - {job.get('job_id')}: {dur:.3f}s ({job.get('status')})")

    hints = payload.get("hints")
    if isinstance(hints, list) and hints:
        lines.append("")
        lines.append("Hints:")
        for hint in hints:
            lines.append(f"  - {hint}")

    if compare is not None:
        lines.append("")
        lines.append(f"Compare vs {compare.get('baseline_run_id')}:")
        for key, label in (
            ("new_failures", "New failures"),
            ("newly_fixed", "Newly fixed"),
            ("still_failing", "Still failing"),
        ):
            items = [x for x in (compare.get(key) or []) if isinstance(x, dict)]
            lines.append(f"  {label} ({len(items)}):")
            for item in items:
                prev = item.get("prev_status")
                trans = f"{prev} -> {item.get('status')}" if prev else str(item.get("status"))
                lines.append(f"    - {item.get('job_id')}: {trans} ({item.get('reason', '')})")

    return "\n".join(lines) + "\n"
