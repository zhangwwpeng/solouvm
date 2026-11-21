"""解析 B 端 run_job.sh 写出的 job.facts（formats.md §2）。"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from solouvm.model import Facts

__all__ = ["FactsError", "parse_facts", "read_facts"]

SUPPORTED_SCHEMA = 1

_REQUIRED = (
    "schema",
    "generator",
    "kind",
    "run_id",
    "job_id",
    "try",
    "host",
    "pid",
    "timeout_impl",
    "started_at",
    "state",
)
_TAIL_REQUIRED = ("rc", "finished_at", "duration_s")
_KINDS = ("job", "build", "check")
_STATES = ("running", "done", "interrupted")
_GENERATOR_PREFIX = "solouvm "


class FactsError(ValueError):
    """job.facts 内容不合法；judge 把它转成规则 0d 的 error。"""


def parse_facts(text: str) -> Facts:
    """Parse the text of a job.facts file."""
    raw = _to_dict(text)
    missing = [key for key in _REQUIRED if key not in raw]
    if missing:
        raise FactsError(f"facts missing required keys: {', '.join(missing)}")

    schema = raw["schema"].strip()
    if schema != str(SUPPORTED_SCHEMA):
        raise FactsError(f"facts schema {schema} not supported; regenerate")

    generator = raw["generator"]
    if not generator.startswith(_GENERATOR_PREFIX):
        raise FactsError(f"facts generator {generator!r} is not solouvm")

    kind = raw["kind"]
    if kind not in _KINDS:
        raise FactsError(f"facts kind {kind!r} must be one of {', '.join(_KINDS)}")

    state = raw["state"]
    if state not in _STATES:
        raise FactsError(f"facts state {state!r} must be one of {', '.join(_STATES)}")

    facts = Facts(
        schema=SUPPORTED_SCHEMA,
        generator=generator,
        kind=kind,
        run_id=raw["run_id"],
        job_id=raw["job_id"],
        try_=_positive_int(raw, "try"),
        host=raw["host"],
        pid=_positive_int(raw, "pid"),
        timeout_impl=raw["timeout_impl"],
        started_at=_timestamp(raw, "started_at"),
        state=state,
    )
    if state == "running":
        # 只有开始段：结束段字段即使出现也不采信，保持 incomplete 语义
        return facts

    # rc=0 是合法值，所以只判断键是否存在
    missing = [key for key in _TAIL_REQUIRED if key not in raw]
    if missing:
        raise FactsError(f"facts state={state} but missing {', '.join(missing)}")
    facts.rc = _int(raw, "rc")
    facts.finished_at = _timestamp(raw, "finished_at")
    facts.duration_s = _float(raw, "duration_s")
    facts.signal = raw.get("signal") or None
    facts.timed_out = _flag(raw, "timed_out")
    facts.hung = _flag(raw, "hung")
    return facts


def read_facts(path: Path) -> Facts | None:
    """Read and parse a job.facts file; return None when it does not exist."""
    path = Path(path)
    if not path.exists():
        return None
    return parse_facts(path.read_text(encoding="utf-8"))


def _to_dict(text: str) -> dict[str, str]:
    raw: dict[str, str] = {}
    # splitlines 同时认 \n、\r\n、\r，行尾的 \r 不会残留在值里
    for line in text.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        # 同名键后者覆盖前者：结束段的 state 覆盖开始段
        raw[key.strip()] = value
    return raw


def _int(raw: dict[str, str], key: str) -> int:
    value = raw[key]
    try:
        return int(value)
    except ValueError:
        raise FactsError(f"facts {key}={value!r} is not an integer") from None


def _positive_int(raw: dict[str, str], key: str) -> int:
    value = raw[key]
    if not value.isdigit() or int(value) <= 0:
        raise FactsError(f"facts {key}={value!r} is not a positive integer")
    return int(value)


def _float(raw: dict[str, str], key: str) -> float:
    value = raw[key]
    try:
        return float(value)
    except ValueError:
        raise FactsError(f"facts {key}={value!r} is not a number") from None


def _flag(raw: dict[str, str], key: str) -> bool:
    value = raw.get(key, "0")
    if value not in ("0", "1", ""):
        raise FactsError(f"facts {key}={value!r} must be 0 or 1")
    return value == "1"


def _timestamp(raw: dict[str, str], key: str) -> datetime:
    value = raw[key]
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise FactsError(f"facts {key}={value!r} is not an ISO 8601 timestamp") from None
    if stamp.tzinfo is None:
        raise FactsError(f"facts {key}={value!r} has no timezone offset")
    return stamp
