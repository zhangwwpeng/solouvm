"""JUnit XML report derived from ``results.json`` (formats.md §8, K-04)."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

from solouvm.report.json import atomic_write_text, job_duration, validate_results_payload

__all__ = ["render_junit", "write_junit_xml"]

_FAILURE_STATUSES = frozenset({"fail", "xpass", "false_pass"})
_ERROR_STATUSES = frozenset({"timeout", "killed", "incomplete", "error"})
_INVALID_XML = re.compile(r"[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def render_junit(payload: dict[str, object], *, project: str = "solouvm") -> str:
    """Render JUnit XML string from a ``results.json`` payload (formats.md §8)."""
    validate_results_payload(payload)
    jobs = [j for j in (payload.get("jobs") or []) if isinstance(j, dict)]
    summary = payload.get("summary")
    summary_dict = summary if isinstance(summary, dict) else {}
    total_time = float(summary_dict.get("duration_s", 0.0) or 0.0)

    failures = sum(1 for j in jobs if str(j.get("status", "")) in _FAILURE_STATUSES)
    errors = sum(1 for j in jobs if str(j.get("status", "")) in _ERROR_STATUSES)
    skipped = sum(1 for j in jobs if str(j.get("status", "")) == "skip")

    suite = ET.Element(
        "testsuite",
        name=str(project or "solouvm"),
        tests=str(len(jobs)),
        failures=str(failures),
        errors=str(errors),
        skipped=str(skipped),
        time=f"{total_time:.3f}",
    )

    for job in jobs:
        dur = job_duration(job)
        tc = ET.SubElement(
            suite,
            "testcase",
            classname=str(job.get("case", "")),
            name=str(job.get("job_id", "")),
            time=f"{dur:.3f}",
        )
        status = str(job.get("status", ""))
        reason = str(job.get("reason", ""))
        first_error = job.get("first_error")
        detail_lines: list[str] = []
        if isinstance(first_error, dict):
            detail_lines.append(f"line {first_error.get('line')}: {first_error.get('text')}")
        if status == "false_pass":
            triage = job.get("triage")
            if isinstance(triage, dict):
                detail_lines.append(
                    f"triage {triage.get('task')}: verdict={triage.get('verdict')} "
                    f"confidence={triage.get('confidence')}"
                )
        detail = "\n".join(detail_lines) if detail_lines else reason

        if status in _FAILURE_STATUSES:
            ET.SubElement(tc, "failure", message=reason, type=status).text = detail
        elif status in _ERROR_STATUSES:
            ET.SubElement(tc, "error", message=reason, type=status).text = detail
        elif status == "skip":
            ET.SubElement(tc, "skipped")
        elif status == "xfail":
            ET.SubElement(tc, "system-out").text = f"xfail: {reason}"

    for element in suite.iter():
        for name, value in element.attrib.items():
            element.set(name, _INVALID_XML.sub("\ufffd", value))
        if element.text is not None:
            element.text = _INVALID_XML.sub("\ufffd", element.text)
    ET.indent(suite, space="  ")
    return ET.tostring(suite, encoding="unicode", xml_declaration=True) + "\n"


def write_junit_xml(path: Path, payload: dict[str, object], *, project: str = "solouvm") -> None:
    """Atomically write JUnit XML derived from ``payload`` to ``path``."""
    atomic_write_text(path, render_junit(payload, project=project))
