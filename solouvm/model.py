"""Case model: Case / Expect / Waiver / Checks and the note record types (config.md §5)."""

from __future__ import annotations

import re
import textwrap
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime
from enum import Enum

from solouvm.errors import ConfigError
from solouvm.units import parse_duration, parse_sim_time_ns

_CASE_NAME = re.compile(r"[A-Za-z0-9_.-]+", re.ASCII)
_PARAM_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
# fromisoformat 在 3.11+ 还接受 20261102 等写法，先用正则把格式钉死为 YYYY-MM-DD
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)
_SEVERITIES = frozenset({"INFO", "WARNING", "ERROR", "FATAL", "SIM"})


def _case_error(message: str) -> ConfigError:
    return ConfigError(message, code="CASE_INVALID")


def _is_int(value: object) -> bool:
    # bool 是 int 的子类，必须排除
    return isinstance(value, int) and not isinstance(value, bool)


def _check_regex(owner: str, name: str, value: object) -> None:
    if not isinstance(value, str):
        raise _case_error(f"{owner}: field {name!r} must be a regex string, got {value!r}")
    try:
        re.compile(value)
    except re.error as exc:
        raise _case_error(f"{owner}: field {name!r} has invalid regex {value!r}: {exc}") from None


def _is_date(value: object) -> bool:
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


class Stage(str, Enum):
    PLANNED = "planned"
    WIP = "wip"
    READY = "ready"
    SIGNOFF = "signoff"
    DEPRECATED = "deprecated"


# ---------------------------------------------------------------- Waiver / Expect


@dataclass
class Waiver:
    reason: str
    id: str | None = None
    regex: str | None = None
    severity: tuple[str, ...] = ("ERROR", "WARNING")
    max: int | None = None
    until: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise _case_error(
                f"Waiver: field 'reason' must be a non-empty string, got {self.reason!r}"
            )
        if self.id is None and self.regex is None:
            raise _case_error("Waiver: at least one of 'id' or 'regex' is required")
        if self.id is not None and (not isinstance(self.id, str) or not self.id):
            raise _case_error(f"Waiver: field 'id' must be a non-empty string, got {self.id!r}")
        if self.regex is not None:
            _check_regex("Waiver", "regex", self.regex)
        if isinstance(self.severity, str) or not isinstance(self.severity, (tuple, list)):
            raise _case_error(
                f"Waiver: field 'severity' must be a tuple of strings, got {self.severity!r}"
            )
        bad = [s for s in self.severity if s not in _SEVERITIES]
        if bad or not self.severity:
            raise _case_error(
                f"Waiver: field 'severity' has invalid value {self.severity!r}; "
                f"allowed: {', '.join(sorted(_SEVERITIES))}"
            )
        self.severity = tuple(self.severity)
        if self.max is not None and (not _is_int(self.max) or self.max < 0):
            raise _case_error(f"Waiver: field 'max' must be a non-negative int, got {self.max!r}")
        if self.until is not None and not _is_date(self.until):
            raise _case_error(
                f"Waiver: field 'until' has invalid date {self.until!r} (expected YYYY-MM-DD)"
            )


@dataclass
class Expect:
    scoreboard: str | None = None
    regex: str | None = None
    min: int | None = None
    max: int | None = None
    count_min: int | None = None
    count_max: int | None = None
    sim_time_min: str | None = None
    uvm_info_min: int | None = None
    id: str | None = None
    reason: str | None = None
    # 构造时由 sim_time_min 换算而来，单位 ns
    sim_time_ns: float | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if all(
            v is None for v in (self.scoreboard, self.regex, self.sim_time_min, self.uvm_info_min)
        ):
            raise _case_error(
                "Expect: at least one of 'scoreboard', 'regex', 'sim_time_min', "
                "'uvm_info_min' is required"
            )
        if self.scoreboard is not None and self.min is None and self.max is None:
            raise _case_error(
                f"Expect: field 'scoreboard' ({self.scoreboard!r}) requires 'min' or 'max'"
            )
        if self.uvm_info_min is not None and self.id is None:
            raise _case_error(f"Expect: field 'uvm_info_min' ({self.uvm_info_min!r}) requires 'id'")
        if self.id is not None and self.uvm_info_min is None:
            raise _case_error(f"Expect: field 'id' ({self.id!r}) requires 'uvm_info_min'")
        if (self.count_min is not None or self.count_max is not None) and self.regex is None:
            raise _case_error("Expect: 'count_min'/'count_max' require 'regex'")
        if (
            (self.min is not None or self.max is not None)
            and self.scoreboard is None
            and self.regex is None
        ):
            raise _case_error("Expect: 'min'/'max' require 'scoreboard' or 'regex'")
        if self.regex is not None:
            _check_regex("Expect", "regex", self.regex)
        for name in ("min", "max", "count_min", "count_max", "uvm_info_min"):
            value = getattr(self, name)
            if value is not None and not _is_int(value):
                raise _case_error(f"Expect: field {name!r} must be an int, got {value!r}")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise _case_error(f"Expect: 'min' ({self.min}) cannot exceed 'max' ({self.max})")
        if (
            self.count_min is not None
            and self.count_max is not None
            and self.count_min > self.count_max
        ):
            raise _case_error(
                f"Expect: 'count_min' ({self.count_min}) cannot exceed "
                f"'count_max' ({self.count_max})"
            )
        if self.sim_time_min is not None:
            try:
                self.sim_time_ns = parse_sim_time_ns(self.sim_time_min)
            except (TypeError, ValueError) as exc:
                raise _case_error(
                    f"Expect: field 'sim_time_min' has invalid value {self.sim_time_min!r}: {exc}"
                ) from None


# ---------------------------------------------------------------- Checks


@dataclass(frozen=True)
class Checks:
    max_uvm_error: int = 0
    max_uvm_fatal: int = 0
    max_uvm_warning: int | None = None
    fail_regex: tuple[str, ...] = ()
    pass_regex: tuple[str, ...] = ()
    env_fail_regex: tuple[str, ...] | None = None  # None = 用后端的 license_regex
    sim_errors: bool = True
    waivers: tuple[Waiver, ...] = ()

    def __post_init__(self) -> None:
        for name in ("max_uvm_error", "max_uvm_fatal", "max_uvm_warning"):
            value = getattr(self, name)
            if value is None and name == "max_uvm_warning":
                continue
            if not _is_int(value) or value < 0:
                raise _case_error(
                    f"checks: field {name!r} must be a non-negative int, got {value!r}"
                )
        if not isinstance(self.sim_errors, bool):
            raise _case_error(f"checks: field 'sim_errors' must be a bool, got {self.sim_errors!r}")
        for name in ("fail_regex", "pass_regex", "env_fail_regex"):
            value = getattr(self, name)
            if value is None and name == "env_fail_regex":
                continue
            if isinstance(value, str) or not isinstance(value, (tuple, list)):
                raise _case_error(f"checks: field {name!r} must be a list of regex, got {value!r}")
            for item in value:
                _check_regex("checks", name, item)
            # 配置里写的是 list；frozen 类里统一存 tuple
            object.__setattr__(self, name, tuple(value))
        if isinstance(self.waivers, Waiver) or not isinstance(self.waivers, (tuple, list)):
            raise _case_error(
                f"checks: field 'waivers' must be a list of Waiver, got {self.waivers!r}"
            )
        for item in self.waivers:
            if not isinstance(item, Waiver):
                raise _case_error(f"checks: field 'waivers' contains a non-Waiver {item!r}")
        object.__setattr__(self, "waivers", tuple(self.waivers))

    @classmethod
    def from_dict(cls, overrides: dict[str, object], base: Checks) -> Checks:
        """Override ``base`` with ``overrides``; ``waivers`` are appended, not replaced."""
        names = [f.name for f in fields(cls)]
        unknown = [k for k in overrides if k not in names]
        if unknown:
            raise _case_error(f"unknown checks key {unknown[0]!r}; valid keys: {', '.join(names)}")
        merged = {name: getattr(base, name) for name in names}
        merged.update({k: v for k, v in overrides.items() if k != "waivers"})
        extra = overrides.get("waivers", ())
        if isinstance(extra, Waiver) or not isinstance(extra, (tuple, list)):
            raise _case_error(f"checks: field 'waivers' must be a list of Waiver, got {extra!r}")
        merged["waivers"] = base.waivers + tuple(extra)
        return cls(**merged)


# ---------------------------------------------------------------- 记录类型（字段只增不改，M-23）


@dataclass
class Bug:
    id: int
    title: str
    status: str = "open"
    found: str | None = None
    fixed: str | None = None
    verified: str | None = None
    seed: int | None = None
    signature: str | None = None
    run: str | None = None
    job: str | None = None
    reporter: str | None = None
    owner: str | None = None
    link: str | None = None
    body: str = ""
    author: str | None = None
    at: str | None = None


@dataclass
class Todo:
    title: str
    owner: str | None = None
    due: str | None = None
    done: bool = False
    id: int | None = None
    author: str | None = None
    at: str | None = None


@dataclass
class Log:
    date: str
    text: str
    author: str | None = None
    at: str | None = None


@dataclass
class Note:
    text: str
    date: str | None = None
    author: str | None = None
    at: str | None = None


@dataclass
class Triage:
    task: str
    verdict: str
    confidence: str
    summary: str
    evidence: list[str]
    judged_status: dict[str, str]
    doc: str | None = None
    bugs: list[str] = field(default_factory=list)
    review: dict[str, object] | None = None
    author: str | None = None
    at: str | None = None


_REQUIRED = {
    "open": ("found",),
    "fixed": ("found", "fixed"),
    "verified": ("found", "fixed", "verified"),
    "wontfix": ("found",),
    "invalid": ("found",),
    "duplicate": ("found", "link"),
}
_VERDICTS = (
    "real_pass",
    "false_pass",
    "design_bug",
    "tb_bug",
    "spec_unclear",
    "env_issue",
    "unknown",
)
_CONFIDENCES = ("high", "medium", "low")
# J-07 的状态集合
_JOB_STATUSES = frozenset(
    {
        "pass",
        "fail",
        "timeout",
        "killed",
        "error",
        "xfail",
        "xpass",
        "skip",
        "incomplete",
        "false_pass",
    }
)
_EVIDENCE = (
    re.compile(r"log:\d+", re.ASCII),
    re.compile(r"[\w./-]+:\d+", re.ASCII),
    re.compile(r"wave:\S+ \S+"),
)
_REVIEW_KEYS = frozenset({"decision", "reason", "author", "at"})
_SUMMARY_MAX = 200

RECORD_TYPES = (Bug, Todo, Log, Note, Triage)


def _check_date(label: str, name: str, value: object, *, required: bool = False) -> list[str]:
    if value is None:
        return [f"{label}: field {name!r} is required"] if required else []
    if not _is_date(value):
        return [f"{label}: field {name!r} has invalid date {value!r} (expected YYYY-MM-DD)"]
    return []


def _check_text(label: str, name: str, value: object) -> list[str]:
    if not isinstance(value, str) or not value.strip():
        return [f"{label}: field {name!r} must be a non-empty string, got {value!r}"]
    return []


def _check_bug(label: str, bug: Bug) -> list[str]:
    problems: list[str] = []
    if not _is_int(bug.id) or bug.id <= 0:
        problems.append(f"{label}: field 'id' must be a positive int, got {bug.id!r}")
    problems += _check_text(label, "title", bug.title)
    required = _REQUIRED.get(bug.status) if isinstance(bug.status, str) else None
    if required is None:
        problems.append(
            f"{label}: field 'status' has invalid value {bug.status!r}; "
            f"allowed: {', '.join(_REQUIRED)}"
        )
        required = ()
    for name in required:
        if getattr(bug, name, None) is None:
            problems.append(f"{label}: status {bug.status!r} requires field {name!r}")
    for name in ("found", "fixed", "verified"):
        problems += _check_date(label, name, getattr(bug, name))
    if bug.seed is not None and (not _is_int(bug.seed) or bug.seed < 0):
        problems.append(f"{label}: field 'seed' must be a non-negative int, got {bug.seed!r}")
    return problems


def _check_todo(label: str, todo: Todo) -> list[str]:
    problems = _check_text(label, "title", todo.title)
    problems += _check_date(label, "due", todo.due)
    if not isinstance(todo.done, bool):
        problems.append(f"{label}: field 'done' must be a bool, got {todo.done!r}")
    if todo.id is not None and (not _is_int(todo.id) or todo.id <= 0):
        problems.append(f"{label}: field 'id' must be a positive int, got {todo.id!r}")
    return problems


def _check_log(label: str, log: Log) -> list[str]:
    return _check_date(label, "date", log.date, required=True) + _check_text(
        label, "text", log.text
    )


def _check_note(label: str, note: Note) -> list[str]:
    return _check_text(label, "text", note.text) + _check_date(label, "date", note.date)


def _check_review(label: str, review: object) -> list[str]:
    if review is None:
        return []
    if not isinstance(review, dict):
        return [f"{label}: field 'review' must be a dict or None, got {review!r}"]
    problems: list[str] = []
    keys = set(review)
    if keys != _REVIEW_KEYS:
        missing = sorted(_REVIEW_KEYS - keys)
        extra = sorted(map(str, keys - _REVIEW_KEYS))
        problems.append(f"{label}: field 'review' keys mismatch; missing={missing} extra={extra}")
    decision = review.get("decision")
    if decision not in ("accepted", "rejected"):
        problems.append(
            f"{label}: review 'decision' has invalid value {decision!r}; "
            "allowed: accepted, rejected"
        )
    reason = review.get("reason")
    if reason is not None and not isinstance(reason, str):
        problems.append(f"{label}: review 'reason' must be a string or None, got {reason!r}")
    for name in ("author", "at"):
        if name in review:
            problems += _check_text(label, f"review.{name}", review[name])
    return problems


def _check_triage(label: str, tri: Triage) -> list[str]:
    problems = _check_text(label, "task", tri.task)
    if tri.verdict not in _VERDICTS:
        problems.append(
            f"{label}: field 'verdict' has invalid value {tri.verdict!r}; "
            f"allowed: {', '.join(_VERDICTS)}"
        )
    if tri.confidence not in _CONFIDENCES:
        problems.append(
            f"{label}: field 'confidence' has invalid value {tri.confidence!r}; "
            f"allowed: {', '.join(_CONFIDENCES)}"
        )
    if not isinstance(tri.summary, str) or not tri.summary.strip():
        problems.append(f"{label}: field 'summary' must be a non-empty string, got {tri.summary!r}")
    elif len(tri.summary) > _SUMMARY_MAX:
        problems.append(
            f"{label}: field 'summary' is {len(tri.summary)} characters (max {_SUMMARY_MAX})"
        )
    if not isinstance(tri.evidence, list) or not tri.evidence:
        problems.append(f"{label}: field 'evidence' must be a non-empty list, got {tri.evidence!r}")
    else:
        for item in tri.evidence:
            if not isinstance(item, str) or not any(p.fullmatch(item) for p in _EVIDENCE):
                problems.append(
                    f"{label}: field 'evidence' has invalid item {item!r} "
                    "(expected 'log:<line>', '<file>:<line>' or 'wave:<time> <signal>')"
                )
    if not isinstance(tri.judged_status, dict):
        problems.append(f"{label}: field 'judged_status' must be a dict, got {tri.judged_status!r}")
    else:
        for job_id, status in tri.judged_status.items():
            if not isinstance(job_id, str) or not job_id:
                problems.append(f"{label}: field 'judged_status' has invalid job id {job_id!r}")
            if status not in _JOB_STATUSES:
                problems.append(
                    f"{label}: field 'judged_status' has invalid status {status!r} for {job_id!r}"
                )
    if not isinstance(tri.bugs, list) or not all(isinstance(b, str) for b in tri.bugs):
        problems.append(f"{label}: field 'bugs' must be a list of strings, got {tri.bugs!r}")
    problems += _check_review(label, tri.review)
    return problems


def _check_record(label: str, record: object) -> list[str]:
    match record:
        case Bug():
            return _check_bug(label, record)
        case Todo():
            return _check_todo(label, record)
        case Log():
            return _check_log(label, record)
        case Note():
            return _check_note(label, record)
        case Triage():
            return _check_triage(label, record)
        case _:
            names = ", ".join(t.__name__ for t in RECORD_TYPES)
            return [f"{label}: unknown record type {type(record).__name__} (expected {names})"]


def validate_records(case: Case) -> None:
    """Validate every record in ``case.notes``; report all problems at once."""
    problems: list[str] = []
    seen: dict[object, int] = {}
    for index, record in enumerate(case.notes):
        label = f"case {case.name!r} notes[{index}] ({type(record).__name__})"
        problems += _check_record(label, record)
        if isinstance(record, (Bug, Todo)) and record.id is not None:
            if record.id in seen:
                problems.append(
                    f"{label}: duplicate record id {record.id!r} (also notes[{seen[record.id]}])"
                )
            else:
                seen[record.id] = index
    if problems:
        raise ConfigError("\n".join(problems), code="RECORD_INVALID")


# ---------------------------------------------------------------- Case / Suite 共享校验


def _validate_case_name(kind: str, name: object) -> None:
    if not isinstance(name, str) or not _CASE_NAME.fullmatch(name) or "__" in name:
        raise _case_error(
            f"{kind} field 'name' has invalid value {name!r}: only letters, digits, "
            "'_', '-', '.' are allowed and '__' is reserved"
        )


def _validate_stage(owner: str, stage: object) -> str:
    try:
        return Stage(stage).value  # type: ignore[arg-type]
    except ValueError:
        allowed = ", ".join(s.value for s in Stage)
        raise _case_error(
            f"{owner}: field 'stage' has invalid value {stage!r}; allowed: {allowed}"
        ) from None


def _validate_durations(owner: str, timeout: object, hang: object) -> None:
    for name, value in (("timeout", timeout), ("hang", hang)):
        if value is None:
            continue
        try:
            parse_duration(value)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise _case_error(
                f"{owner}: field {name!r} has invalid value {value!r}: {exc}"
            ) from None


def _validate_retry(owner: str, retry: object) -> None:
    if retry is not None and (not _is_int(retry) or retry < 0):  # type: ignore[operator]
        raise _case_error(f"{owner}: field 'retry' must be a non-negative int, got {retry!r}")


def _validate_params(owner: str, params: object) -> None:
    if not isinstance(params, dict):
        raise _case_error(f"{owner}: field 'params' must be a dict, got {params!r}")
    for key, value in params.items():
        if not isinstance(key, str) or not _PARAM_KEY.fullmatch(key):
            raise _case_error(f"{owner}: field 'params' has invalid key {key!r}")
        if not isinstance(value, (str, int, float, bool)):
            raise _case_error(
                f"{owner}: field 'params' key {key!r} has invalid value {value!r} "
                "(expected str, int, float or bool)"
            )


def _validate_str_seq(owner: str, field_name: str, value: object) -> list[str]:
    if (
        isinstance(value, str)
        or not isinstance(value, (list, tuple))
        or not all(isinstance(item, str) for item in value)
    ):
        raise _case_error(f"{owner}: field {field_name!r} must be a sequence of str, got {value!r}")
    return list(value)


def _validate_str_dict(owner: str, field_name: str, value: object) -> None:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise _case_error(f"{owner}: field {field_name!r} must be a dict[str, str], got {value!r}")


# ---------------------------------------------------------------- Case


@dataclass
class Case:
    name: str
    uvm_test: str | None = None
    params: dict[str, object] = field(default_factory=dict)
    opts: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    build: str = "default"
    seeds: str | list[int] | None = None
    timeout: str | None = None
    hang: str | None = None
    retry: int | None = None
    checks: dict[str, object] = field(default_factory=dict)
    expect_fail: bool | str = False
    tags: tuple[str, ...] = ()
    owner: str | None = None
    stage: str = "wip"
    intent: str = ""
    method: str = ""
    spec_refs: list[str] = field(default_factory=list)
    expect: list[Expect] = field(default_factory=list)
    doc: str = ""
    notes: list[object] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._check_name()
        self._check_stage()
        self._check_expect_fail()
        self._check_durations()
        self._check_retry()
        self._check_params()
        self._check_checks()
        if isinstance(self.doc, str):
            # 去掉首尾空白与公共缩进（config.md §5.1）
            self.doc = textwrap.dedent(self.doc).strip()
        validate_records(self)

    def _error(self, message: str) -> ConfigError:
        return _case_error(f"case {self.name!r}: {message}")

    def _check_name(self) -> None:
        _validate_case_name("case", self.name)

    def _check_stage(self) -> None:
        self.stage = _validate_stage(f"case {self.name!r}", self.stage)

    def _check_expect_fail(self) -> None:
        value = self.expect_fail
        if isinstance(value, bool):
            return
        if not isinstance(value, str) or not value:
            raise self._error(
                f"field 'expect_fail' must be a bool or a non-empty regex, got {value!r}"
            )
        try:
            re.compile(value)
        except re.error as exc:
            raise self._error(f"field 'expect_fail' has invalid regex {value!r}: {exc}") from None

    def _check_durations(self) -> None:
        _validate_durations(f"case {self.name!r}", self.timeout, self.hang)

    def _check_retry(self) -> None:
        _validate_retry(f"case {self.name!r}", self.retry)

    def _check_params(self) -> None:
        _validate_params(f"case {self.name!r}", self.params)

    def _check_checks(self) -> None:
        if not isinstance(self.checks, dict):
            raise self._error(f"field 'checks' must be a dict, got {self.checks!r}")
        try:
            Checks.from_dict(self.checks, Checks())
        except ConfigError as exc:
            raise self._error(exc.message) from None


# ---------------------------------------------------------------- Suite（T-14）


@dataclass
class Suite:
    """Group of cases sharing default parameters (T-14)."""

    name: str
    cases: list[Case] = field(default_factory=list)
    tags: tuple[str, ...] = ()
    build: str | None = None
    timeout: str | None = None
    hang: str | None = None
    retry: int | None = None
    seeds: str | list[int] | None = None
    params: dict[str, object] = field(default_factory=dict)
    opts: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    stage: str | None = None
    owner: str | None = None

    def __post_init__(self) -> None:
        _validate_case_name("suite", self.name)
        owner = f"suite {self.name!r}"
        if isinstance(self.cases, str) or not isinstance(self.cases, (list, tuple)):
            raise _case_error(f"{owner}: field 'cases' must be a list of Case, got {self.cases!r}")
        for item in self.cases:
            if not isinstance(item, Case):
                raise _case_error(f"{owner}: field 'cases' contains a non-Case {item!r}")
        self.cases = list(self.cases)
        self.tags = tuple(_validate_str_seq(owner, "tags", self.tags))
        if self.build is not None and (not isinstance(self.build, str) or not self.build.strip()):
            raise _case_error(f"{owner}: field 'build' must be a non-empty str, got {self.build!r}")
        _validate_durations(owner, self.timeout, self.hang)
        _validate_retry(owner, self.retry)
        _validate_params(owner, self.params)
        self.opts = _validate_str_seq(owner, "opts", self.opts)
        _validate_str_dict(owner, "env", self.env)
        if self.stage is not None:
            self.stage = _validate_stage(owner, self.stage)
        if self.owner is not None and not isinstance(self.owner, str):
            raise _case_error(f"{owner}: field 'owner' must be a str or None, got {self.owner!r}")

    def expand(self) -> list[Case]:
        """Return new ``Case`` instances with suite defaults merged into each case.

        - ``tags``: suite and case tags unioned cleanly, preserving first-seen order.
        - ``params`` / ``env``: merged with case entries overriding suite entries.
        - ``opts``: suite opts followed by case opts.
        - ``build``: inherits ``suite.build`` when set and ``case.build == "default"``.
        - ``stage``: inherits ``suite.stage`` when set and ``case.stage == "wip"``.
        - ``seeds`` / ``timeout`` / ``hang`` / ``retry`` / ``owner``: inherited when ``None``.
        """
        expanded: list[Case] = []
        for case in self.cases:
            suite_seeds = list(self.seeds) if isinstance(self.seeds, list) else self.seeds
            expanded.append(
                replace(
                    case,
                    params={**self.params, **case.params},
                    opts=[*self.opts, *case.opts],
                    env={**self.env, **case.env},
                    build=(
                        self.build
                        if self.build is not None and case.build == "default"
                        else case.build
                    ),
                    seeds=case.seeds if case.seeds is not None else suite_seeds,
                    timeout=case.timeout if case.timeout is not None else self.timeout,
                    hang=case.hang if case.hang is not None else self.hang,
                    retry=case.retry if case.retry is not None else self.retry,
                    checks=dict(case.checks),
                    tags=tuple(dict.fromkeys([*self.tags, *case.tags])),
                    owner=case.owner if case.owner is not None else self.owner,
                    stage=(
                        self.stage if self.stage is not None and case.stage == "wip" else case.stage
                    ),
                    spec_refs=list(case.spec_refs),
                    expect=list(case.expect),
                    notes=list(case.notes),
                )
            )
        return expanded


# ---------------------------------------------------------------------------
# job.facts（任务 7，formats.md §2）：B 交给 A 的唯一结构化数据，由 judge/facts.py 解析


@dataclass
class Facts:
    schema: int
    generator: str
    kind: str
    run_id: str
    job_id: str
    try_: int  # 文件里的键名是 try（Python 关键字）
    host: str
    pid: int
    timeout_impl: str
    started_at: datetime
    state: str
    rc: int | None = None  # None = 结束段还没写，job 没跑完
    signal: str | None = None
    timed_out: bool = False
    hung: bool = False
    finished_at: datetime | None = None
    duration_s: float | None = None


# ---------------------------------------------------------------------------
# 计划（任务 12，formats.md §4）：本轮回归的唯一依据，写成 plan.json


@dataclass
class Job:
    job_id: str
    case: str
    seed: int
    build: str
    uvm_test: str | None
    params: dict[str, object]
    opts: list[str]
    env: dict[str, str]
    timeout_s: float
    hang_s: float | None
    retry: int
    expect_fail: bool | str
    tags: tuple[str, ...]
    dir: str  # job 根目录（相对项目根）；每次尝试在其下的 try<N>/


@dataclass
class Plan:
    run_id: str
    created_at: datetime
    parent_run: str | None
    provider: str
    selection: dict[str, object]
    builds: dict[str, str]  # build mode -> 编译键
    jobs: list[Job]


# ---------------------------------------------------------------------------
# 结果（任务 18，formats.md §6）：单个 job 的最终判定与报告信息


@dataclass
class Result:
    job_id: str
    case: str
    seed: int
    status: str
    judged_status: str
    raw_status: str
    category: str | None
    reason: str
    first_error: dict[str, object] | None = None
    signature: dict[str, str] | None = None
    uvm: dict[str, object] = field(default_factory=dict)
    waivers: list[dict[str, object]] = field(default_factory=list)
    expect: list[dict[str, object]] = field(default_factory=list)
    weak_signals: list[str] = field(default_factory=list)
    known: list[str] = field(default_factory=list)
    triage: dict[str, object] | None = None
    flaky: bool = False
    attempts: list[dict[str, object]] = field(default_factory=list)
    repro: dict[str, str] = field(default_factory=dict)
    chosen_try: int | None = None
