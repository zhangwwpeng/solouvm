"""Report rendering and multi-format output derived from ``results.json`` (K-01..K-12)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from solouvm.errors import ConfigError
from solouvm.report.html import render_html
from solouvm.report.json import (
    CATEGORY_ORDER,
    PASS_STATUSES,
    RESULTS_SCHEMA,
    STATUS_ORDER,
    atomic_write_text,
    compare_results,
    job_duration,
    read_results_json,
    render_json,
    result_to_dict,
    results_to_payload,
    validate_results_payload,
    write_results_json,
)
from solouvm.report.junit import render_junit, write_junit_xml
from solouvm.report.text import render_text

__all__ = [
    "CATEGORY_ORDER",
    "DEFAULT_FILENAMES",
    "PASS_STATUSES",
    "RESULTS_SCHEMA",
    "STATUS_ORDER",
    "SUPPORTED_FORMATS",
    "atomic_write_text",
    "compare_results",
    "job_duration",
    "read_results_json",
    "render_html",
    "render_json",
    "render_junit",
    "render_report",
    "render_text",
    "result_to_dict",
    "results_to_payload",
    "validate_results_payload",
    "write_junit_xml",
    "write_reports",
    "write_results_json",
]

SUPPORTED_FORMATS: tuple[str, ...] = ("text", "json", "junit", "html")

DEFAULT_FILENAMES: dict[str, str] = {
    "text": "results.txt",
    "json": "results.json",
    "junit": "junit.xml",
    "html": "results.html",
}


def render_report(
    payload: dict[str, object],
    *,
    fmt: str = "text",
    project: str = "solouvm",
    compare: dict[str, object] | None = None,
) -> str:
    """Render ``payload`` into one of ``text``, ``json``, ``junit``, or ``html``."""
    validate_results_payload(payload)
    if fmt == "text":
        return render_text(payload, compare=compare)
    if fmt == "json":
        if compare is None:
            return render_json(payload)
        merged = {**payload, "compare": compare}
        return render_json(merged)
    if fmt == "junit":
        return render_junit(payload, project=project)
    if fmt == "html":
        return render_html(payload, project=project, compare=compare)
    raise ConfigError(
        f"unsupported report format {fmt!r}; expected one of {', '.join(SUPPORTED_FORMATS)}",
        code="CFG_INVALID",
    )


def write_reports(
    out_dir: Path,
    payload: dict[str, object],
    *,
    formats: Sequence[str] = ("text", "json", "junit"),
    project: str = "solouvm",
    compare: dict[str, object] | None = None,
    output_path: Path | None = None,
) -> list[Path]:
    """Write requested report formats atomically and return written file paths."""
    validate_results_payload(payload)
    fmt_list = list(dict.fromkeys(formats))
    if not fmt_list:
        fmt_list = ["text"]
    for fmt in fmt_list:
        if fmt not in SUPPORTED_FORMATS:
            raise ConfigError(
                f"unsupported report format {fmt!r}; "
                f"expected one of {', '.join(SUPPORTED_FORMATS)}",
                code="CFG_INVALID",
            )

    written: list[Path] = []
    if output_path is not None:
        out_p = Path(output_path)
        if len(fmt_list) == 1 and not out_p.is_dir():
            text = render_report(payload, fmt=fmt_list[0], project=project, compare=compare)
            atomic_write_text(out_p, text)
            return [out_p]
        if out_p.exists() and not out_p.is_dir():
            raise ConfigError(
                f"cannot write multiple report formats to file {out_p}",
                code="CFG_INVALID",
                hint="pass a directory for -o when using multiple --format flags",
            )
        target_dir = out_p
    else:
        target_dir = Path(out_dir)

    for fmt in fmt_list:
        dest = target_dir / DEFAULT_FILENAMES[fmt]
        text = render_report(payload, fmt=fmt, project=project, compare=compare)
        atomic_write_text(dest, text)
        written.append(dest)
    return written
