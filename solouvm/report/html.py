"""Self-contained static HTML report derived from ``results.json`` (K-08)."""

from __future__ import annotations

import html

from solouvm.report.json import (
    CATEGORY_ORDER,
    PASS_STATUSES,
    STATUS_ORDER,
    job_duration,
    validate_results_payload,
)

__all__ = ["render_html"]

_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       margin: 24px; color: #1f2328; background: #ffffff; line-height: 1.5; }
h1, h2 { margin-top: 24px; margin-bottom: 8px; }
.meta { color: #57606a; font-size: 0.92rem; margin-bottom: 16px; }
.cards { display: flex; flex-wrap: wrap; gap: 12px; margin-bottom: 20px; }
.card { border: 1px solid #d0d7de; border-radius: 6px; padding: 10px 16px; min-width: 120px;
        background: #f6f8fa; }
.card .label { font-size: 0.8rem; color: #57606a; text-transform: uppercase; }
.card .val { font-size: 1.3rem; font-weight: 600; }
table { border-collapse: collapse; width: 100%; margin: 12px 0 24px; font-size: 0.9rem; }
th, td { border: 1px solid #d0d7de; padding: 6px 10px; text-align: left; vertical-align: top; }
th { background: #f6f8fa; font-weight: 600; }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
            font-size: 0.85rem; }
pre { background: #f6f8fa; padding: 8px; border-radius: 4px; overflow-x: auto; margin: 4px 0; }
.badge { display: inline-block; padding: 2px 7px; border-radius: 12px; font-size: 0.78rem;
         font-weight: 600; }
.st-pass, .st-xfail { background: #dafbe1; color: #116329; }
.st-skip { background: #eaeef2; color: #57606a; }
.st-fail, .st-xpass, .st-false_pass { background: #ffebe9; color: #cf222e; }
.st-timeout, .st-killed, .st-incomplete, .st-error { background: #fff8c5; color: #9a6700; }
details summary { cursor: pointer; color: #0969da; }
""".strip()


def _esc(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _badge(status: str) -> str:
    safe = _esc(status)
    cls = f"st-{safe}" if status in STATUS_ORDER else "st-error"
    return f'<span class="badge {cls}">{safe}</span>'


def render_html(
    payload: dict[str, object],
    *,
    project: str = "solouvm",
    compare: dict[str, object] | None = None,
) -> str:
    """Render a zero-external-dependency static HTML report from ``results.json`` payload."""
    validate_results_payload(payload)
    run_id = str(payload.get("run_id", ""))
    judged_at = str(payload.get("judged_at", ""))
    partial = bool(payload.get("partial", False))
    summary = payload.get("summary")
    summary_dict = summary if isinstance(summary, dict) else {}

    total = int(summary_dict.get("total", 0) or 0)
    passed = int(summary_dict.get("passed", 0) or 0)
    failed = max(0, total - passed)
    duration_s = float(summary_dict.get("duration_s", 0.0) or 0.0)
    by_status = summary_dict.get("by_status")
    by_status_dict = by_status if isinstance(by_status, dict) else {}
    by_category = summary_dict.get("by_category")
    by_category_dict = by_category if isinstance(by_category, dict) else {}
    jobs = [j for j in (payload.get("jobs") or []) if isinstance(j, dict)]
    signatures = [s for s in (payload.get("signatures") or []) if isinstance(s, dict)]

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{_esc(project)} - {_esc(run_id)}</title>",
        f"<style>{_CSS}</style>",
        "</head>",
        "<body>",
        f"<h1>{_esc(project)} Regression Report</h1>",
        f'<div class="meta">Run: <code>{_esc(run_id)}</code> &middot; '
        f"Judged: {_esc(judged_at)}"
        + (" &middot; <strong>PARTIAL</strong>" if partial else "")
        + "</div>",
        '<div class="cards">',
        f'<div class="card"><div class="label">Total</div><div class="val">{total}</div></div>',
        f'<div class="card"><div class="label">Passed</div><div class="val">{passed}</div></div>',
        f'<div class="card"><div class="label">Not Passed</div>'
        f'<div class="val">{failed}</div></div>',
        f'<div class="card"><div class="label">Duration</div>'
        f'<div class="val">{duration_s:.3f}s</div></div>',
    ]
    for cat in CATEGORY_ORDER:
        cnt = int(by_category_dict.get(cat, 0) or 0)
        parts.append(
            f'<div class="card"><div class="label">{_esc(cat)}</div>'
            f'<div class="val">{cnt}</div></div>'
        )
    parts.append("</div>")

    status_cells = " ".join(
        f"{_badge(s)} <code>{int(by_status_dict.get(s, 0) or 0)}</code>"
        for s in STATUS_ORDER
        if int(by_status_dict.get(s, 0) or 0) > 0
    )
    if status_cells:
        parts.append(f"<p><strong>By Status:</strong> {status_cells}</p>")

    if compare is not None:
        parts.append(f"<h2>Comparison vs {_esc(compare.get('baseline_run_id'))}</h2>")
        parts.append("<table><thead><tr><th>Kind</th><th>Jobs</th></tr></thead><tbody>")
        for key, label in (
            ("new_failures", "New Failures"),
            ("newly_fixed", "Newly Fixed"),
            ("still_failing", "Still Failing"),
        ):
            items = [x for x in (compare.get(key) or []) if isinstance(x, dict)]
            desc = ", ".join(_esc(item.get("job_id")) for item in items) or "none"
            parts.append(f"<tr><td>{_esc(label)} ({len(items)})</td><td>{desc}</td></tr>")
        parts.append("</tbody></table>")

    if signatures:
        parts.append("<h2>Failure Signatures</h2>")
        parts.append(
            "<table><thead><tr><th>ID</th><th>Count</th><th>Cases</th>"
            "<th>Example</th><th>Text</th></tr></thead><tbody>"
        )
        for sig in signatures:
            cases_str = ", ".join(str(c) for c in (sig.get("cases") or []))
            parts.append(
                "<tr>"
                f"<td><code>{_esc(sig.get('id'))}</code></td>"
                f"<td>{int(sig.get('count', 0) or 0)}</td>"
                f"<td>{_esc(cases_str)}</td>"
                f"<td><code>{_esc(sig.get('example'))}</code></td>"
                f"<td><code>{_esc(sig.get('text'))}</code></td>"
                "</tr>"
            )
        parts.append("</tbody></table>")

    waivers = payload.get("waivers")
    if isinstance(waivers, dict) and any(waivers.get(k) for k in ("used", "unused", "expired")):
        parts.append("<h2>Waivers</h2><ul>")
        for item in waivers.get("used") or []:
            if isinstance(item, dict):
                parts.append(
                    f"<li><strong>Used:</strong> <code>{_esc(item.get('id'))}</code> "
                    f"({int(item.get('lines', 0) or 0)} lines)</li>"
                )
        for item in waivers.get("unused") or []:
            parts.append(f"<li><strong>Unused:</strong> <code>{_esc(item)}</code></li>")
        for item in waivers.get("expired") or []:
            parts.append(f"<li><strong>Expired:</strong> <code>{_esc(item)}</code></li>")
        parts.append("</ul>")

    hints = payload.get("hints")
    if isinstance(hints, list) and hints:
        parts.append("<h2>Hints</h2><ul>")
        for h in hints:
            parts.append(f"<li>{_esc(h)}</li>")
        parts.append("</ul>")

    parts.append("<h2>Jobs</h2>")
    parts.append(
        "<table><thead><tr><th>Job</th><th>Case</th><th>Seed</th><th>Status</th>"
        "<th>Category</th><th>Duration</th><th>Reason &amp; Details</th></tr></thead><tbody>"
    )
    for job in jobs:
        status = str(job.get("status", ""))
        dur = job_duration(job)
        details: list[str] = []
        if job.get("flaky"):
            details.append("<div><strong>Flaky:</strong> true</div>")
        first_err = job.get("first_error")
        if isinstance(first_err, dict):
            details.append(
                f"<div><strong>First error (line {_esc(first_err.get('line'))}):</strong> "
                f"<code>{_esc(first_err.get('text'))}</code></div>"
            )
        known = job.get("known")
        if isinstance(known, list) and known:
            details.append(
                f"<div><strong>Known bugs:</strong> {_esc(', '.join(str(k) for k in known))}</div>"
            )
        weak = job.get("weak_signals")
        if isinstance(weak, list) and weak:
            details.append(
                f"<div><strong>Weak signals:</strong> {_esc('; '.join(str(w) for w in weak))}</div>"
            )
        triage = job.get("triage")
        if isinstance(triage, dict):
            details.append(
                f"<div><strong>Triage:</strong> <code>{_esc(triage.get('task'))}</code> "
                f"verdict={_esc(triage.get('verdict'))} "
                f"review={_esc(triage.get('review'))} "
                f"stale={_esc(triage.get('stale'))}</div>"
            )
        repro = job.get("repro")
        if isinstance(repro, dict) and (repro.get("make") or repro.get("solouvm")):
            cmds = "\n".join(str(repro[k]) for k in ("make", "solouvm") if repro.get(k))
            details.append(f"<div><strong>Repro:</strong><pre>{_esc(cmds)}</pre></div>")

        detail_html = ""
        if details:
            open_attr = " open" if status not in PASS_STATUSES else ""
            detail_html = (
                f"<details{open_attr}><summary>Details</summary>{''.join(details)}</details>"
            )

        parts.append(
            "<tr>"
            f"<td><code>{_esc(job.get('job_id'))}</code></td>"
            f"<td>{_esc(job.get('case'))}</td>"
            f"<td>{_esc(job.get('seed'))}</td>"
            f"<td>{_badge(status)}</td>"
            f"<td>{_esc(job.get('category') or '-')}</td>"
            f"<td>{dur:.3f}s</td>"
            f"<td>{_esc(job.get('reason'))}{detail_html}</td>"
            "</tr>"
        )
    parts.append("</tbody></table>")
    parts.extend(["</body>", "</html>", ""])
    return "\n".join(parts)
