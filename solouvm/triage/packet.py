"""Triage context packet rendering and budget fitting (AI-06, ai-workflow.md §5.2)."""

from __future__ import annotations

import contextlib
import re
import shlex
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.collect.cases import collect_cases
from solouvm.errors import ConfigError, InfraError
from solouvm.explain import (
    explain_data,
    find_previous_pass,
    find_signature_history,
    git_commits_since_pass,
)
from solouvm.history import resolve_run_id, run_dir
from solouvm.model import Bug, Case, Expect, Job, Log, Note, Plan, Todo, Triage
from solouvm.plan import read_plan
from solouvm.report.json import read_results_json
from solouvm.triage.plan import (
    _job_case_from_id,
    primary_case_for_task,
    read_tasks_json,
    tasks_json_path,
)
from solouvm.wave import find_waveform

if TYPE_CHECKING:
    from solouvm.config.schema import Config
    from solouvm.providers.base import Provider

__all__ = ["PACKET_SECTIONS", "render_packet"]

PACKET_SECTIONS: tuple[str, ...] = (
    "## 1. Task Instructions",
    "## 2. System Documentation",
    "## 3. Case Documentation",
    "## 4. Current Run Result",
    "## 5. Source Code Snippets",
    "## 6. Historical Context",
    "## 7. Repro & Wave Instructions",
)

_HEADING_RE = re.compile(r"^(#{1,3}\s+.+)$", re.MULTILINE)


@dataclass
class _Segment:
    """One renderable piece of the packet, optionally truncatable at a given priority."""

    text: str
    priority: int | None = None
    trunc_ref: str = ""
    keep_heading: str = ""


def _safe_collect_cases(config: Config) -> dict[str, Case]:
    try:
        return {c.name: c for c in collect_cases(config)}
    except ConfigError:
        return {}


def _synthetic_plan(
    *,
    run_id: str,
    results_jobs: list[dict[str, object]],
    task_jobs: list[dict[str, object]],
    cases_list: list[str],
) -> Plan:
    res_by_id = {
        str(j["job_id"]): j
        for j in results_jobs
        if isinstance(j, dict) and isinstance(j.get("job_id"), str)
    }
    jobs: list[Job] = []
    seen: set[str] = set()
    for item in task_jobs:
        if not isinstance(item, dict):
            continue
        jid = str(item.get("job_id", ""))
        if not jid or jid in seen:
            continue
        seen.add(jid)
        rj = res_by_id.get(jid, {})
        cname = (
            rj.get("case")
            if isinstance(rj.get("case"), str)
            else _job_case_from_id(jid, cases_list)
        )
        raw_seed = rj.get("seed", 0)
        seed = int(raw_seed) if isinstance(raw_seed, int) and not isinstance(raw_seed, bool) else 0
        jobs.append(
            Job(
                job_id=jid,
                case=str(cname),
                seed=seed,
                build="default",
                uvm_test=None,
                params={},
                opts=[],
                env={},
                timeout_s=1200.0,
                hang_s=None,
                retry=0,
                expect_fail=False,
                tags=(),
                dir=f".solouvm/runs/{run_id}/{jid}",
            )
        )
    return Plan(
        run_id=run_id,
        created_at=datetime.now(timezone.utc),
        parent_run=None,
        provider="local",
        selection={},
        builds={"default": "default"},
        jobs=jobs,
    )


def _format_expect(exp: Expect) -> str:
    parts: list[str] = []
    if exp.scoreboard is not None:
        desc = f"scoreboard `{exp.scoreboard}`"
        if exp.min is not None:
            desc += f" min={exp.min}"
        if exp.max is not None:
            desc += f" max={exp.max}"
        parts.append(desc)
    if exp.regex is not None:
        desc = f"regex `{exp.regex}`"
        if exp.count_min is not None:
            desc += f" count_min={exp.count_min}"
        if exp.count_max is not None:
            desc += f" count_max={exp.count_max}"
        if exp.min is not None:
            desc += f" min={exp.min}"
        if exp.max is not None:
            desc += f" max={exp.max}"
        parts.append(desc)
    if exp.sim_time_min is not None:
        parts.append(f"sim_time_min `{exp.sim_time_min}`")
    if exp.uvm_info_min is not None:
        parts.append(f"uvm_info_min={exp.uvm_info_min} id=`{exp.id}`")
    text = "; ".join(parts)
    if exp.reason:
        text += f" ({exp.reason})"
    return text


def _format_note_record(case_name: str, rec: object) -> str:
    if isinstance(rec, Bug):
        seed_str = f", seed={rec.seed}" if rec.seed is not None else ""
        sig_str = f", sig={rec.signature}" if rec.signature else ""
        owner_str = f", owner={rec.owner}" if rec.owner else ""
        body_str = f" — {rec.body.strip()}" if rec.body.strip() else ""
        return (
            f"  - Bug `{case_name}#{rec.id}` [{rec.status}{owner_str}{seed_str}{sig_str}]: "
            f"{rec.title}{body_str}"
        )
    if isinstance(rec, Todo):
        ident = f"{case_name}#{rec.id}" if rec.id is not None else case_name
        state = "done" if rec.done else "open"
        due_str = f", due={rec.due}" if rec.due else ""
        return f"  - Todo `{ident}` [{state}{due_str}]: {rec.title}"
    if isinstance(rec, Log):
        return f"  - Log ({rec.date}): {rec.text}"
    if isinstance(rec, Note):
        date_str = f" ({rec.date})" if rec.date else ""
        return f"  - Note{date_str}: {rec.text}"
    if isinstance(rec, Triage):
        rev = (
            str(rec.review.get("decision", "pending"))
            if isinstance(rec.review, dict)
            else "pending"
        )
        return (
            f"  - Triage `{rec.task}`: verdict=`{rec.verdict}`, "
            f"confidence=`{rec.confidence}`, review=`{rev}` — {rec.summary}"
        )
    return f"  - Record: {rec!r}"


def _build_section_1(
    *,
    task: dict[str, object],
    ordered_cases: list[str],
    primary_case: str,
    case_counts: dict[str, int],
) -> str:
    task_id = str(task.get("id", ""))
    kind = str(task.get("kind", ""))
    sig = task.get("signature")
    reason = str(task.get("reason", ""))
    jobs = [j for j in (task.get("jobs") or []) if isinstance(j, dict)]
    example_job = str(jobs[0].get("job_id", "<job_id>")) if jobs else "<job_id>"

    lines: list[str] = [
        PACKET_SECTIONS[0],
        f"- Task ID: `{task_id}`",
        f"- Kind: `{kind}`",
        f"- Signature: `{sig}`" if sig else "- Signature: none",
        f"- Reason: {reason or '-'}",
        "- Affected cases (descending job count): "
        + ", ".join(f"`{c}` ({case_counts[c]})" for c in ordered_cases),
        f"- Jobs ({len(jobs)}): " + ", ".join(f"`{j.get('job_id')}`" for j in jobs),
        "",
        "### Questions to Answer",
    ]
    if kind == "pass":
        lines.extend(
            [
                "- Did this pass genuinely verify the case `intent` "
                "and exercise the DUT/scoreboard?",
                "- Allowed verdicts for `kind=pass`: `real_pass`, `false_pass`.",
            ]
        )
    else:
        lines.extend(
            [
                "- What is the root cause of this failure "
                "(RTL design, testbench/case, spec, or env)?",
                "- Allowed verdicts for `kind=fail`: `design_bug`, `tb_bug`, `spec_unclear`, "
                "`env_issue`, `unknown`, `false_pass` (`real_pass` is NOT allowed).",
            ]
        )

    lines.extend(
        [
            "",
            "### Allowed Commands (`--role triage`)",
            f"- Inspect log: `solouvm --role triage log {example_job} --around <line>`",
            f"- Full explain: `solouvm --role triage explain {example_job}`",
            f"- Query wave: `solouvm --role triage wave {example_job} "
            "--signals <sig> --from <T1> --to <T2>`",
            "- Query notes: `solouvm --role triage notes`",
            f"- Register bug: `solouvm --role triage bug add {primary_case} --job {example_job} "
            '--title "..." --body -`',
            f"- Append note (if open/fixed bug with same signature already exists): "
            f"`solouvm --role triage note add {primary_case} --text -`",
            f"- Write verdict: `solouvm --role triage triage write {task_id} --verdict <verdict> "
            '--confidence <high|medium|low> --summary "<=200 chars" '
            '--evidence "log:<line>" --doc -`',
            "",
            "### Output Constraints",
            "- Do NOT modify RTL, testbench, or `case.py` directly; "
            "use `--role triage` commands only.",
            "- `--summary` must be non-empty and <= 200 characters.",
            "- Provide at least one `--evidence` matching "
            "`log:<line>`, `<file>:<line>`, or `wave:<time> <signal>`.",
            "- `--doc` must contain all 5 non-empty markdown sections: "
            "`## 现象`, `## 排查过程`, `## 证据`, `## 结论`, `## 建议`.",
        ]
    )
    if len(ordered_cases) > 1:
        lines.append(
            f"- Multi-case task: register any new bug under `{primary_case}` "
            f"(most affected jobs: {case_counts[primary_case]})."
        )
    return "\n".join(lines)


def _build_section_2(config: Config) -> tuple[str, str]:
    root = Path(config.root)
    doc_paths = list(config.system_doc)
    if not doc_paths:
        return f"{PACKET_SECTIONS[1]}\nNo `system_doc` files configured.", "solouvm.conf.py"

    parts: list[str] = []
    refs: list[str] = []
    for raw in doc_paths:
        p = Path(raw)
        abs_p = p if p.is_absolute() else root / p
        rel_label = raw
        refs.append(rel_label)
        if not abs_p.is_file():
            parts.append(f"### `{rel_label}`\n(file not found: `{rel_label}`)")
            continue
        content = abs_p.read_text(encoding="utf-8", errors="replace").strip()
        headings = _HEADING_RE.findall(content)
        if headings:
            toc = ", ".join(h.lstrip("# ").strip() for h in headings[:8])
            refs[-1] = f"{rel_label} (sections: {toc})"
        parts.append(f"### `{rel_label}`\n{content}" if content else f"### `{rel_label}`\n(empty)")

    body = "\n\n".join(parts)
    return body, "; ".join(refs)


def _build_section_3(
    *,
    ordered_cases: list[str],
    primary_case: str,
    case_counts: dict[str, int],
    case_map: dict[str, Case],
) -> str:
    lines: list[str] = [PACKET_SECTIONS[2]]
    if len(ordered_cases) > 1:
        lines.append(
            f"- Bug registration target: `{primary_case}` "
            f"(highest affected job count: {case_counts[primary_case]})."
        )

    for cname in ordered_cases:
        count = case_counts[cname]
        primary_tag = (
            " [primary case for bug registration]"
            if len(ordered_cases) > 1 and cname == primary_case
            else ""
        )
        lines.append("")
        lines.append(f"### Case `{cname}` ({count} job(s)){primary_tag}")
        case = case_map.get(cname)
        if case is None:
            lines.append("- Case definition not found in current case files.")
            continue
        lines.append(
            f"- Stage: `{case.stage}` | Owner: `{case.owner or '-'}` | "
            f"Build: `{case.build}` | UVM Test: `{case.uvm_test or '-'}` | "
            f"Expect fail: `{case.expect_fail}`"
        )
        lines.append(f"- Intent: {case.intent or '-'}")
        lines.append(f"- Method: {case.method or '-'}")
        lines.append("- Spec refs: " + (", ".join(case.spec_refs) if case.spec_refs else "none"))
        if case.expect:
            lines.append("- Expect:")
            for exp in case.expect:
                lines.append(f"  - {_format_expect(exp)}")
        else:
            lines.append("- Expect: none")
        if case.doc:
            lines.extend(["- Doc:", case.doc])
        if case.notes:
            lines.append("- Records:")
            for rec in case.notes:
                lines.append(_format_note_record(cname, rec))
        else:
            lines.append("- Records: none")

    return "\n".join(lines)


def _job_wave_info(
    *,
    root: Path,
    run_id: str,
    job_id: str,
    job_data: dict[str, object],
    provider: Provider | None,
) -> str | None:
    for att in job_data.get("attempts") or ():
        if isinstance(att, dict) and att.get("wave"):
            return f"try{att.get('try', '?')}"
    if provider is not None:
        with contextlib.suppress(ConfigError, InfraError):
            wave_path = find_waveform(provider, run_id, job_id)
            return wave_path
    job_dir = root / ".solouvm" / "runs" / run_id / job_id
    if job_dir.is_dir():
        for entry in sorted(job_dir.iterdir()):
            if entry.is_dir() and entry.name.startswith("try") and (entry / ".wave").exists():
                return entry.name
    return None


def _truncate_body(body: str, keep: int, ref: str) -> str:
    if len(body) <= keep:
        return body
    min_marker = f"... (truncated {len(body)} chars, see {ref})"
    if keep <= len(min_marker) + 40:
        return min_marker
    probe_marker = f"\n... (truncated {len(body)} chars, see {ref})\n"
    avail = max(0, keep - len(probe_marker))
    if avail < 20:
        return min_marker
    head_len = (avail * 2) // 3
    tail_len = avail - head_len
    omitted = len(body) - (head_len + tail_len)
    marker = f"\n... (truncated {omitted} chars, see {ref})\n"
    tail_part = body[len(body) - tail_len :] if tail_len > 0 else ""
    return body[:head_len] + marker + tail_part


def _fit_segments(segments: list[_Segment], limit: int) -> str:
    if limit == 0:
        return "\n\n".join(seg.text for seg in segments if seg.text)

    def _total_len() -> int:
        active = [seg.text for seg in segments if seg.text]
        if not active:
            return 0
        return sum(len(t) for t in active) + 2 * (len(active) - 1)

    total = _total_len()
    if total <= limit:
        return "\n\n".join(seg.text for seg in segments if seg.text)

    for prio in (1, 2, 3, 4):
        for seg in segments:
            total = _total_len()
            if total <= limit:
                break
            if seg.priority != prio or not seg.text:
                continue
            prefix = f"{seg.keep_heading}\n" if seg.keep_heading else ""
            raw_body = (
                seg.text[len(prefix) :] if prefix and seg.text.startswith(prefix) else seg.text
            )
            excess = total - limit
            target_total = max(0, len(seg.text) - excess)
            target_body = max(0, target_total - len(prefix))
            truncated_body = _truncate_body(raw_body, target_body, seg.trunc_ref)
            seg.text = f"{prefix}{truncated_body}" if prefix else truncated_body
        if _total_len() <= limit:
            break

    return "\n\n".join(seg.text for seg in segments if seg.text)


def render_packet(
    *,
    config: Config,
    run_id: str,
    task_id: str,
    limit: int | None = None,
    provider: Provider | None = None,
) -> str:
    """Render the 7-section triage context packet for ``task_id`` under ``limit`` budget."""
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
        raise ConfigError(
            f"invalid packet limit {limit!r}; expected non-negative int",
            code="CFG_INVALID",
        )
    eff_limit = config.triage.packet_limit if limit is None else limit

    root = Path(config.root)
    actual_run = resolve_run_id(root, run_id)
    tasks_data = read_tasks_json(tasks_json_path(root, actual_run))
    task = next(
        (t for t in tasks_data["tasks"] if isinstance(t, dict) and t.get("id") == task_id),
        None,
    )
    if task is None:
        raise ConfigError(
            f"task {task_id!r} not found in run {actual_run!r}",
            code="TASK_NOT_FOUND",
        )

    results_data = read_results_json(run_dir(root, actual_run) / "results.json")
    results_jobs = [j for j in (results_data.get("jobs") or []) if isinstance(j, dict)]
    res_by_id = {str(j["job_id"]): j for j in results_jobs if isinstance(j.get("job_id"), str)}

    task_cases = [str(c) for c in (task.get("cases") or []) if isinstance(c, str) and c]
    task_jobs = [j for j in (task.get("jobs") or []) if isinstance(j, dict)]

    # Count affected jobs per case and order cases DESCENDING by affected count.
    case_counts: dict[str, int] = defaultdict(int)
    for cname in task_cases:
        case_counts[cname] = 0
    job_to_case: dict[str, str] = {}
    for jentry in task_jobs:
        jid = str(jentry.get("job_id", ""))
        rj = res_by_id.get(jid, {})
        cname = (
            rj.get("case")
            if isinstance(rj.get("case"), str)
            else _job_case_from_id(jid, task_cases)
        )
        cname_str = str(cname)
        job_to_case[jid] = cname_str
        case_counts[cname_str] += 1

    ordered_cases = sorted(case_counts, key=lambda c: (-case_counts[c], c))
    primary_case = (
        primary_case_for_task(task) if task_cases else (ordered_cases[0] if ordered_cases else "")
    )
    case_rank = {cname: idx for idx, cname in enumerate(ordered_cases)}
    ordered_job_ids = sorted(
        [str(j.get("job_id", "")) for j in task_jobs if j.get("job_id")],
        key=lambda jid: (case_rank.get(job_to_case.get(jid, ""), 999999), jid),
    )

    plan_path = run_dir(root, actual_run) / "plan.json"
    if plan_path.is_file():
        plan = read_plan(plan_path)
    else:
        plan = _synthetic_plan(
            run_id=actual_run,
            results_jobs=results_jobs,
            task_jobs=task_jobs,
            cases_list=ordered_cases,
        )
    plan_job_ids = {j.job_id for j in plan.jobs}

    case_map = _safe_collect_cases(config)

    # Gather explain_data per job in order.
    job_explains: dict[str, dict[str, object]] = {}
    for jid in ordered_job_ids:
        if jid in plan_job_ids and jid in res_by_id:
            with contextlib.suppress(ConfigError):
                job_explains[jid] = explain_data(
                    config=config,
                    plan=plan,
                    run_id=actual_run,
                    job_id=jid,
                    provider=provider,
                )

    segments: list[_Segment] = []

    # Header + Section 1 (Protected)
    sec1 = _build_section_1(
        task=task,
        ordered_cases=ordered_cases,
        primary_case=primary_case,
        case_counts=case_counts,
    )
    segments.append(_Segment(text=f"# Triage Context Packet: {task_id}\n\n{sec1}", priority=None))

    # Section 2: System Documentation (Priority 1 to truncate)
    sys_body, sys_ref = _build_section_2(config)
    if config.system_doc:
        segments.append(
            _Segment(
                text=f"{PACKET_SECTIONS[1]}\n{sys_body}",
                priority=1,
                trunc_ref=sys_ref,
                keep_heading=PACKET_SECTIONS[1],
            )
        )
    else:
        segments.append(_Segment(text=sys_body, priority=None))

    # Section 3: Case Documentation (Protected)
    sec3 = _build_section_3(
        ordered_cases=ordered_cases,
        primary_case=primary_case,
        case_counts=case_counts,
        case_map=case_map,
    )
    segments.append(_Segment(text=sec3, priority=None))

    # Section 4: Current Run Result (Metadata protected, log context priority 2)
    segments.append(_Segment(text=PACKET_SECTIONS[3], priority=None))
    if not ordered_job_ids:
        segments.append(_Segment(text="No jobs in this task.", priority=None))
    for jid in ordered_job_ids:
        edata = job_explains.get(jid)
        rj = res_by_id.get(jid, {})
        meta_lines: list[str] = []
        if edata is not None:
            meta_lines.extend(
                [
                    f"### Job `{jid}` (case `{edata['case']}`, seed `{edata['seed']}`)",
                    f"- Status: `{edata['status']}` (judged=`{edata['judged_status']}`, "
                    f"raw=`{edata['raw_status']}`)",
                    f"- Reason: {edata['reason'] or '-'}",
                ]
            )
            first_err = edata.get("first_error")
            if isinstance(first_err, dict):
                meta_lines.append(
                    f"- First error (line {first_err.get('line')}): `{first_err.get('text')}`"
                )
            else:
                meta_lines.append("- First error: none")
            expects = edata.get("expect")
            if isinstance(expects, list) and expects:
                meta_lines.append("- Expect checks:")
                for exp in expects:
                    if isinstance(exp, dict):
                        mark = "PASS" if exp.get("ok") else "FAIL"
                        line = f"  - [{mark}] {exp.get('rule')} (observed: {exp.get('value')!r})"
                        if not exp.get("ok") and exp.get("detail"):
                            line += f" - {exp.get('detail')}"
                        meta_lines.append(line)
            else:
                meta_lines.append("- Expect checks: none")
            weak = edata.get("weak_signals")
            if isinstance(weak, list) and weak:
                meta_lines.append("- Weak signals: " + "; ".join(str(w) for w in weak))
            else:
                meta_lines.append("- Weak signals: none")
            meta_lines.append(f"- Log path: `{edata['log_path']}`")
            segments.append(_Segment(text="\n".join(meta_lines), priority=None))

            log_ctx = str(edata.get("log_context", ""))
            log_block = (
                log_ctx if log_ctx.startswith("log not available:") else f"```text\n{log_ctx}\n```"
            )
            segments.append(
                _Segment(
                    text=log_block,
                    priority=2,
                    trunc_ref=f"{edata['log_path']} (or run: solouvm log {jid})",
                )
            )
        else:
            jstatus = rj.get("status", task.get("kind", ""))
            jreason = rj.get("reason", task.get("reason", ""))
            meta_lines.extend(
                [
                    f"### Job `{jid}`",
                    f"- Status: `{jstatus}`",
                    f"- Reason: {jreason or '-'}",
                    f"- Log command: `solouvm log {jid} --run {actual_run}`",
                ]
            )
            segments.append(_Segment(text="\n".join(meta_lines), priority=None))

    # Section 5: Source Code Snippets (Priority 4 - truncated after System Doc, Logs, and History)
    unique_snippets: dict[tuple[str, int], str] = {}
    for jid in ordered_job_ids:
        edata = job_explains.get(jid)
        if edata is None:
            continue
        for snip_item in edata.get("snippets") or ():
            if not isinstance(snip_item, dict):
                continue
            fpath = str(snip_item.get("file", ""))
            lineno = snip_item.get("line")
            if isinstance(lineno, int):
                unique_snippets.setdefault((fpath, lineno), str(snip_item.get("snippet", "")))

    if unique_snippets:
        snip_parts: list[str] = []
        src_files: list[str] = []
        for (fpath, lineno), snippet_text in unique_snippets.items():
            if fpath not in src_files:
                src_files.append(fpath)
            if snippet_text.startswith("source not available:"):
                snip_parts.append(f"### `{fpath}:{lineno}`\n{snippet_text}")
            else:
                snip_parts.append(f"### `{fpath}:{lineno}`\n```systemverilog\n{snippet_text}\n```")
        segments.append(
            _Segment(
                text=f"{PACKET_SECTIONS[4]}\n" + "\n\n".join(snip_parts),
                priority=4,
                trunc_ref=", ".join(src_files),
                keep_heading=PACKET_SECTIONS[4],
            )
        )
    else:
        segments.append(
            _Segment(text=f"{PACKET_SECTIONS[4]}\nNo source locations found.", priority=None)
        )

    # Section 6: Historical Context (Priority 3 - truncated after System Doc and Logs)
    hist_lines: list[str] = []
    raw_sig = task.get("signature")
    sig_id = str(raw_sig) if isinstance(raw_sig, str) and raw_sig else ""
    if sig_id:
        sig_hist = find_signature_history(root, sig_id, exclude_run_id=actual_run)
        if sig_hist:
            hist_lines.append(f"- History for signature `{sig_id}` ({len(sig_hist)} match(es)):")
            for h in sig_hist:
                hist_lines.append(
                    f"  - run `{h.get('run_id')}` job `{h.get('job_id')}` "
                    f"[{h.get('status')}] ({h.get('created_at') or '-'})"
                )
        else:
            hist_lines.append(f"- History for signature `{sig_id}`: none")
    else:
        hist_lines.append("- Signature history: none")

    for cname in ordered_cases:
        prev_pass = find_previous_pass(root, cname, actual_run)
        commits = git_commits_since_pass(root, prev_pass)
        hist_lines.append(
            f"- Case `{cname}` previous pass: `{prev_pass}`"
            if prev_pass
            else f"- Case `{cname}` previous pass: none"
        )
        no_log_statuses = (
            "git unavailable",
            "no previous passing run",
            "no new commits since previous pass",
        )
        if commits in no_log_statuses or commits.startswith(("no git commit", "git commit ")):
            hist_lines.append(f"  - Git commits: {commits}")
        else:
            hist_lines.extend(["  - Git commits since previous pass:", "```text", commits, "```"])

    first_case = ordered_cases[0] if ordered_cases else ""
    hist_ref = (
        f"solouvm history {first_case}"
        if first_case
        else f"solouvm explain {ordered_job_ids[0] if ordered_job_ids else ''}"
    )
    segments.append(
        _Segment(
            text=f"{PACKET_SECTIONS[5]}\n" + "\n".join(hist_lines),
            priority=3,
            trunc_ref=hist_ref,
            keep_heading=PACKET_SECTIONS[5],
        )
    )

    # Section 7: Repro & Wave Instructions (Protected)
    repro_lines: list[str] = [PACKET_SECTIONS[6]]
    for jid in ordered_job_ids:
        edata = job_explains.get(jid)
        rj = res_by_id.get(jid, {})
        raw_repro = (edata.get("repro") if edata is not None else rj.get("repro")) or {}
        repro_dict = raw_repro if isinstance(raw_repro, dict) else {}
        cname = job_to_case.get(jid, primary_case)
        seed_val = rj.get("seed", 0)
        run_mk = shlex.quote(f".solouvm/runs/{actual_run}/run.mk")
        run_target = shlex.quote(f"run/{jid}")
        make_cmd = str(repro_dict.get("make") or f"make -f {run_mk} TRY=r1 WAVE=1 {run_target}")
        solouvm_cmd = str(
            repro_dict.get("solouvm")
            or f"solouvm run --case {shlex.quote(cname)} --seed {seed_val} --wave"
        )
        wave_info = _job_wave_info(
            root=root,
            run_id=actual_run,
            job_id=jid,
            job_data=rj,
            provider=provider,
        )
        wave_line = (
            f"- Wave attempt: available (`{wave_info}`)"
            if wave_info
            else f"- Wave attempt: none (generate via `solouvm rerun --run {actual_run} --wave`)"
        )
        repro_lines.extend(
            [
                f"### Job `{jid}`",
                f"- Make repro: `{make_cmd}`",
                f"- Solouvm repro: `{solouvm_cmd}`",
                wave_line,
                f"- Wave query example: `solouvm wave {jid} "
                "--signals tb_top.dut.* --from 0ns --to 1000ns`",
            ]
        )
    segments.append(_Segment(text="\n".join(repro_lines), priority=None))

    return _fit_segments(segments, eff_limit) + "\n"
