"""Deciding pass/fail from a log plus the process outcome.

Rule order is fixed and non-negotiable; ``pass_regex`` can only rescue the
"missing UVM Report Summary" heuristic, never a hard failure:

1. killed by signal      -> killed
2. wall-clock timeout    -> timeout
3. non-zero exit code    -> fail
4. ``fail_regex`` hit    -> fail
5. UVM_ERROR > limit     -> fail
6. UVM_FATAL > limit     -> fail
7. UVM log without Report Summary and no ``pass_regex`` -> fail
8. non-UVM: ``SOLOUVM_DONE ... status=fail``            -> fail
9. non-UVM: no ``SOLOUVM_DONE``                         -> fail
10. otherwise            -> pass
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

SUMMARY = re.compile(r"^\s*UVM_(ERROR|FATAL|WARNING)\s*:\s*(\d+)")
AT_LINE = re.compile(r"^\s*UVM_(ERROR|FATAL)\s+@")
UVM_MARK = re.compile(r"\bUVM_(?:INFO|ERROR|FATAL|WARNING)\b")
DONE = re.compile(r"\bSOLOUVM_DONE\b.*\bstatus=(pass|fail)\b")

MAX_FIRED = 20


class Judge:
    """Single-pass, O(1) memory log scanner."""

    def __init__(self, checks: Mapping[str, Any]) -> None:
        self.max_error = int(checks.get("max_uvm_error", 0))
        self.max_fatal = int(checks.get("max_uvm_fatal", 0))
        limit_warning = checks.get("max_uvm_warning")
        self.max_warning = int(limit_warning) if isinstance(limit_warning, int) else None
        self.fail_patterns = _compile(checks.get("fail_regex") or [])
        self.pass_patterns = _compile(checks.get("pass_regex") or [])

        self.summary: dict[str, int] = {"ERROR": 0, "FATAL": 0, "WARNING": 0}
        self.summary_seen = False
        self.at_counts: dict[str, int] = {"ERROR": 0, "FATAL": 0}
        self.uvm_seen = False
        self.done_status: str | None = None
        self.fired: list[dict[str, Any]] = []
        self.pass_hit: re.Pattern[str] | None = None

    def feed(self, line: str) -> None:
        if UVM_MARK.search(line):
            self.uvm_seen = True
            match = SUMMARY.match(line)
            if match:
                self.summary[match.group(1)] = int(match.group(2))
                self.summary_seen = True
            else:
                match = AT_LINE.match(line)
                if match:
                    self.at_counts[match.group(1)] += 1

        if "SOLOUVM_DONE" in line:
            match = DONE.search(line)
            if match:
                self.done_status = match.group(1)

        for pattern in self.fail_patterns:
            if pattern.search(line):
                self._fire("fail_regex", pattern.pattern, line)

        if self.pass_hit is None:
            for pattern in self.pass_patterns:
                if pattern.search(line):
                    self.pass_hit = pattern
                    self._fire("pass_regex", pattern.pattern, line)
                    break

    def verdict(
        self,
        *,
        rc: int | None,
        signal: str | None = None,
        timed_out: bool = False,
    ) -> dict[str, Any]:
        uvm = self.counts()
        status, reason = self._decide(rc=rc, signal=signal, timed_out=timed_out, uvm=uvm)
        return {
            "status": status,
            "raw_status": status,
            "reason": reason,
            "uvm": {**uvm, "info": 0},
            "checks_fired": self.fired,
        }

    def counts(self) -> dict[str, int]:
        if self.summary_seen:
            error, fatal, warning = (
                self.summary["ERROR"],
                self.summary["FATAL"],
                self.summary["WARNING"],
            )
        else:
            error, fatal, warning = self.at_counts["ERROR"], self.at_counts["FATAL"], 0
        return {"error": error, "fatal": fatal, "warning": warning}

    def _decide(
        self,
        *,
        rc: int | None,
        signal: str | None,
        timed_out: bool,
        uvm: Mapping[str, int],
    ) -> tuple[str, str]:
        if signal:
            return "killed", f"terminated by signal {signal}"
        if timed_out:
            return "timeout", "wall-clock timeout"
        if rc is not None and rc != 0:
            return "fail", f"non-zero exit code {rc}"

        if self.fired and self.fired[0]["kind"] == "fail_regex":
            detail = next(item for item in self.fired if item["kind"] == "fail_regex")
            return "fail", f"fail_regex matched {detail['pattern']!r} at line {detail['line']}"

        if uvm["error"] > self.max_error:
            return "fail", f"UVM_ERROR={uvm['error']} > {self.max_error}"
        if uvm["fatal"] > self.max_fatal:
            return "fail", f"UVM_FATAL={uvm['fatal']} > {self.max_fatal}"
        if self.max_warning is not None and uvm["warning"] > self.max_warning:
            return "fail", f"UVM_WARNING={uvm['warning']} > {self.max_warning}"

        if self.uvm_seen:
            if not self.summary_seen and self.pass_hit is None:
                return "fail", "UVM log without a Report Summary"
            return "pass", "UVM report summary clean"

        if self.done_status == "pass":
            return "pass", "SOLOUVM_DONE status=pass"
        if self.done_status == "fail":
            return "fail", "SOLOUVM_DONE status=fail"
        return "fail", "missing SOLOUVM_DONE marker (simulation may have died early)"

    def _fire(self, kind: str, pattern: str, line: str) -> None:
        if len(self.fired) >= MAX_FIRED:
            return
        self.fired.append({"kind": kind, "pattern": pattern, "line": _lineno(self)})


def _compile(patterns: Any) -> list[re.Pattern[str]]:
    out: list[re.Pattern[str]] = []
    for item in patterns:
        try:
            out.append(re.compile(str(item)))
        except re.error as exc:
            from .errors import ConfigError

            raise ConfigError(f"invalid regex {item!r} in checks: {exc}") from None
    return out


def _lineno(judge: Judge) -> int:
    return getattr(judge, "_lines", 0)


def judge_file(
    path: Path,
    *,
    checks: Mapping[str, Any],
    rc: int | None,
    signal: str | None = None,
    timed_out: bool = False,
    tail: int = 0,
) -> dict[str, Any]:
    """Stream ``path`` through :class:`Judge` and return the verdict."""
    judge = Judge(checks)
    lines = 0
    if path.is_file():
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle, start=1):
                lines = index
                judge._lines = index  # type: ignore[attr-defined]
                judge.feed(line)
    judge._lines = lines  # type: ignore[attr-defined]
    verdict = judge.verdict(rc=rc, signal=signal, timed_out=timed_out)
    if tail > 0:
        verdict["log_tail"] = _tail(path, tail)
    return verdict


def _tail(path: Path, count: int) -> list[str]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return [line.rstrip("\n") for line in handle.readlines()[-count:]]
