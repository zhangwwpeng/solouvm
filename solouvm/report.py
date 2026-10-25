"""Human-readable reporting over ``results.json``."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .model import BAD_STATUSES

ORDER = ("pass", "xfail", "fail", "timeout", "error", "killed", "xpass", "skip")


def render_text(results: Mapping[str, Any], *, verbose: bool = False) -> str:
    counts = results.get("counts") or {}
    head = (
        f"{results.get('project', 'solouvm')}  run {results.get('run_id', '?')}"
        f"  simulator={results.get('simulator', '?')}"
    )
    parts = [head, ""]

    summary = "  ".join(f"{key}={counts.get(key, 0)}" for key in ORDER)
    parts.append("  " + summary)
    parts.append("")

    cases = results.get("cases") or []
    bad = [item for item in cases if (item.get("status") or "error") in BAD_STATUSES]
    if bad:
        parts.append(f"  failing ({len(bad)}/{len(cases)}):")
        for item in bad:
            parts.append(f"    {_line(item, verbose=verbose)}")
    else:
        parts.append(f"  no failures across {len(cases)} case(s)")

    xfail = [item for item in cases if item.get("status") == "xfail"]
    if xfail:
        parts.append("")
        parts.append(f"  expected failures ({len(xfail)}):")
        parts.extend(f"    {item.get('case_id')}" for item in xfail)
    parts.append("")
    return "\n".join(parts)


def _line(item: Mapping[str, Any], *, verbose: bool) -> str:
    status = str(item.get("status") or "error").upper().ljust(7)
    reason = item.get("reason") or item.get("checks_fired") or ""
    if isinstance(reason, list):
        reason = "; ".join(
            str(entry.get("message") or entry.get("pattern") or entry) for entry in reason
        )
    text = f"{status} {item.get('case_id')}  seed={item.get('seed')}"
    if reason:
        text += f"  [{reason}]"
    if verbose:
        text += f"\n            log: {item.get('log')}  cwd: {item.get('cwd')}"
        if item.get("cmd"):
            text += "\n            cmd: " + " ".join(str(part) for part in item["cmd"])
    return text


def render_summary(results: Mapping[str, Any]) -> str:
    counts = results.get("counts") or {}
    total = sum(counts.get(key, 0) for key in ORDER)
    return f"{counts.get('pass', 0)}/{total} passed, {counts.get('fail', 0)} failed"
