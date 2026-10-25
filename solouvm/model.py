"""Data model shared by collection, planning, execution and reporting."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .errors import ConfigError
from .util import case_id as make_case_id
from .util import slug

#: Terminal states written into ``status.json``.
STATUSES = (
    "pass",
    "fail",
    "timeout",
    "error",
    "killed",
    "skip",
    "xfail",
    "xpass",
    "pending",
)

#: States a raw execution may produce (before ``expect_fail`` is applied).
EXEC_STATUSES = ("pass", "fail", "timeout", "error", "killed")

#: Statuses that make the aggregate run fail.
BAD_STATUSES = ("fail", "timeout", "error", "killed", "xpass", "missing", "stale")


@dataclass
class Case:
    """A test case as declared by the user in ``case.py``.

    Only ``name`` is mandatory; everything else falls back to project config.
    """

    name: str
    uvm_test: str | None = None
    top: str | None = None
    params: Mapping[str, Any] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    timeout: str | int | float | None = None
    retry: int | None = None
    expect_fail: bool = False
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ConfigError(f"Case.name must be a non-empty string, got {self.name!r}")
        self.name = self.name.strip()
        if self.retry is not None and (not isinstance(self.retry, int) or self.retry < 0):
            raise ConfigError(f"Case({self.name}): retry must be an int >= 0")
        if not isinstance(self.tags, tuple):
            self.tags = tuple(self.tags)
        if not isinstance(self.params, Mapping) or not isinstance(self.env, Mapping):
            raise ConfigError(f"Case({self.name}): params/env must be mappings")

    def with_defaults(self, cfg: ProjectDefaults) -> Case:
        """Return the case with project defaults filled in (never mutates self)."""
        return Case(
            name=self.name,
            uvm_test=self.uvm_test if self.uvm_test is not None else cfg.uvm_test,
            top=self.top if self.top is not None else cfg.top,
            params=dict(self.params),
            env=dict(self.env),
            timeout=self.timeout if self.timeout is not None else cfg.timeout,
            retry=self.retry if self.retry is not None else cfg.retry,
            expect_fail=self.expect_fail,
            tags=self.tags,
        )


@dataclass(frozen=True)
class ProjectDefaults:
    """Per-case defaults coming from ``solouvm.py``."""

    top: str | None = None
    uvm_test: str | None = None
    timeout: str | int | float | None = None
    retry: int = 0


@dataclass(frozen=True)
class CaseSpec:
    """One (case, seed) pair — the unit of work for ``_exec``."""

    case_id: str
    case: str
    seed: int
    uvm_test: str | None
    top: str | None
    params: Mapping[str, Any]
    env: Mapping[str, str]
    timeout_s: float
    retry: int
    expect_fail: bool
    tags: tuple[str, ...]
    outdir: str

    @classmethod
    def create(
        cls,
        case: Case,
        seed: int,
        *,
        timeout_s: float,
        outdir: str,
    ) -> CaseSpec:
        return cls(
            case_id=make_case_id(case.name, seed),
            case=case.name,
            seed=seed,
            uvm_test=case.uvm_test,
            top=case.top,
            params=dict(case.params),
            env=dict(case.env),
            timeout_s=timeout_s,
            retry=case.retry or 0,
            expect_fail=case.expect_fail,
            tags=case.tags,
            outdir=outdir,
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "case": self.case,
            "seed": self.seed,
            "uvm_test": self.uvm_test,
            "top": self.top,
            "params": dict(self.params),
            "env": dict(self.env),
            "timeout_s": self.timeout_s,
            "retry": self.retry,
            "expect_fail": self.expect_fail,
            "tags": list(self.tags),
            "outdir": self.outdir,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> CaseSpec:
        return cls(
            case_id=payload["case_id"],
            case=payload["case"],
            seed=int(payload["seed"]),
            uvm_test=payload.get("uvm_test"),
            top=payload.get("top"),
            params=dict(payload.get("params") or {}),
            env=dict(payload.get("env") or {}),
            timeout_s=float(payload["timeout_s"]),
            retry=int(payload.get("retry") or 0),
            expect_fail=bool(payload.get("expect_fail")),
            tags=tuple(payload.get("tags") or ()),
            outdir=payload["outdir"],
        )


def sort_specs(specs: list[CaseSpec]) -> list[CaseSpec]:
    """Stable order: ``(case name, seed)`` so shards and reports agree."""
    return sorted(specs, key=lambda s: (slug(s.case), s.seed))
