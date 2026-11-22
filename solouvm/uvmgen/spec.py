"""Data model and loader for ``tbspec.py`` (config.md §6, U-02)."""

from __future__ import annotations

import contextlib
import re
import runpy
import sys
import traceback
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from solouvm.errors import ConfigError

TBSPEC_NAME = "tbspec.py"

_IDENT_RE = re.compile(r"[a-z][a-z0-9_]*\Z", re.ASCII)
_SIGNAL_DIRS = ("m2s", "s2m")
_AGENT_ROLES = ("master", "slave", "monitor")
_RESET_ACTIVES = ("low", "high")

__all__ = [
    "TBSPEC_NAME",
    "Agent",
    "Env",
    "Field",
    "Interface",
    "Signal",
    "Tbspec",
    "load_tbspec",
    "resolve_agent_fields",
]


def _spec_error(message: str, *, file: Path | None = None, line: int | None = None) -> ConfigError:
    return ConfigError(message, code="CFG_INVALID", file=file, line=line)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_ident(owner: str, field_name: str, value: object) -> str:
    if not isinstance(value, str) or not _IDENT_RE.fullmatch(value):
        raise _spec_error(
            f"{owner}: field {field_name!r} has invalid identifier {value!r} "
            "(must match [a-z][a-z0-9_]*)"
        )
    return value


def _check_bool(owner: str, field_name: str, value: object) -> bool:
    if not isinstance(value, bool):
        raise _spec_error(f"{owner}: field {field_name!r} must be a bool, got {value!r}")
    return value


def _to_tuple(owner: str, field_name: str, value: object, elem_type: type, label: str) -> tuple:
    if isinstance(value, (str, bytes)) or not isinstance(value, (tuple, list)):
        raise _spec_error(
            f"{owner}: field {field_name!r} must be a sequence of {label}, got {value!r}"
        )
    items = tuple(value)
    for item in items:
        if not isinstance(item, elem_type):
            raise _spec_error(f"{owner}: field {field_name!r} contains non-{label} item {item!r}")
    return items


@dataclass(frozen=True)
class Signal:
    """Protocol signal declared inside an :class:`Interface`."""

    name: str
    width: int | str
    dir: str  # "m2s" or "s2m"

    def __post_init__(self) -> None:
        _check_ident("Signal", "name", self.name)
        owner = f"Signal {self.name!r}"
        if _is_int(self.width):
            if self.width < 1:  # type: ignore[operator]
                raise _spec_error(
                    f"{owner}: field 'width' must be a positive int, got {self.width!r}"
                )
        elif isinstance(self.width, str):
            cleaned_w = self.width.strip()
            if not cleaned_w or any(ch in cleaned_w for ch in ("\n", "\r", ";")):
                raise _spec_error(
                    f"{owner}: string 'width' must be a non-empty expression, got {self.width!r}"
                )
            object.__setattr__(self, "width", cleaned_w)
        else:
            raise _spec_error(
                f"{owner}: field 'width' must be a positive int or str, got {self.width!r}"
            )
        if self.dir not in _SIGNAL_DIRS:
            raise _spec_error(
                f"{owner}: field 'dir' has invalid value {self.dir!r}; "
                f"allowed: {', '.join(_SIGNAL_DIRS)}"
            )


@dataclass(frozen=True)
class Interface:
    """SystemVerilog interface specification shared across agents."""

    name: str
    clock: str
    reset: tuple[str, str]
    signals: tuple[Signal, ...]
    params: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_ident("Interface", "name", self.name)
        owner = f"Interface {self.name!r}"
        _check_ident(owner, "clock", self.clock)
        if (
            isinstance(self.reset, (str, bytes))
            or not isinstance(self.reset, (tuple, list))
            or len(self.reset) != 2
        ):
            raise _spec_error(
                f"{owner}: field 'reset' must be a (signal, 'low'|'high') pair, got {self.reset!r}"
            )
        rst_name, rst_active = self.reset
        _check_ident(owner, "reset[0]", rst_name)
        if rst_active not in _RESET_ACTIVES:
            raise _spec_error(
                f"{owner}: field 'reset[1]' has invalid polarity {rst_active!r}; "
                f"allowed: {', '.join(_RESET_ACTIVES)}"
            )
        object.__setattr__(self, "reset", (rst_name, rst_active))

        signals = _to_tuple(owner, "signals", self.signals, Signal, "Signal")
        if self.clock == rst_name:
            raise _spec_error(f"{owner}: clock and reset must have different names")
        generated = {"drv_cb", "mon_cb", "drv_mp", "mon_mp", "slv_drv_cb", "slv_drv_mp"}
        if {self.clock, rst_name} & generated:
            raise _spec_error(f"{owner}: clock/reset collides with a generated name")
        seen_sigs = generated | {self.clock, rst_name}
        for sig in signals:
            if sig.name in seen_sigs:
                raise _spec_error(f"{owner}: duplicate signal name {sig.name!r}")
            seen_sigs.add(sig.name)
        object.__setattr__(self, "signals", signals)

        params = _to_tuple(owner, "params", self.params, str, "str")
        for param in params:
            if not param.strip():
                raise _spec_error(f"{owner}: field 'params' must not contain blank strings")
        object.__setattr__(self, "params", params)


@dataclass(frozen=True)
class Field:
    """Transaction field declared inside an :class:`Agent`."""

    name: str
    type: int | str
    rand: bool = True
    constraint: str | None = None

    def __post_init__(self) -> None:
        _check_ident("Field", "name", self.name)
        owner = f"Field {self.name!r}"
        if _is_int(self.type):
            if self.type < 1:  # type: ignore[operator]
                raise _spec_error(f"{owner}: integer 'type' width must be >= 1, got {self.type!r}")
        elif isinstance(self.type, str):
            if not self.type.strip():
                raise _spec_error(f"{owner}: string 'type' must be non-empty, got {self.type!r}")
        else:
            raise _spec_error(
                f"{owner}: field 'type' must be a positive int width or str, got {self.type!r}"
            )
        _check_bool(owner, "rand", self.rand)
        if self.constraint is not None and (
            not isinstance(self.constraint, str) or not self.constraint.strip()
        ):
            raise _spec_error(
                f"{owner}: field 'constraint' must be a non-empty str or None, "
                f"got {self.constraint!r}"
            )


@dataclass(frozen=True)
class Agent:
    """UVM agent specification bound to an :class:`Interface`."""

    name: str
    interface: Interface
    role: str
    fields: tuple[Field, ...] = ()
    coverage: bool = False
    like: str | None = None

    def __post_init__(self) -> None:
        _check_ident("Agent", "name", self.name)
        owner = f"Agent {self.name!r}"
        if not isinstance(self.interface, Interface):
            raise _spec_error(
                f"{owner}: field 'interface' must be an Interface, got {self.interface!r}"
            )
        if self.role not in _AGENT_ROLES:
            raise _spec_error(
                f"{owner}: field 'role' has invalid value {self.role!r}; "
                f"allowed: {', '.join(_AGENT_ROLES)}"
            )
        fields_tuple = _to_tuple(owner, "fields", self.fields, Field, "Field")
        seen_fields: set[str] = set()
        for fld in fields_tuple:
            if fld.name in seen_fields:
                raise _spec_error(f"{owner}: duplicate field name {fld.name!r}")
            seen_fields.add(fld.name)
        object.__setattr__(self, "fields", fields_tuple)
        _check_bool(owner, "coverage", self.coverage)
        if self.like is not None:
            _check_ident(owner, "like", self.like)
            if self.like == self.name:
                raise _spec_error(f"{owner}: field 'like' cannot reference itself")


def resolve_agent_fields(
    agent: Agent,
    agents_by_name: Mapping[str, Agent] | None = None,
) -> tuple[Field, ...]:
    """Return effective fields for ``agent``, following ``Agent.like`` chains."""
    seen: set[str] = set()
    chain: list[Agent] = []
    cur: Agent | None = agent
    while cur is not None:
        if cur.name in seen:
            raise _spec_error(f"Agent {agent.name!r}: cyclic 'like' reference via {cur.name!r}")
        seen.add(cur.name)
        chain.append(cur)
        if cur.like is None:
            break
        if agents_by_name is None or cur.like not in agents_by_name:
            raise _spec_error(
                f"Agent {cur.name!r}: field 'like' references unknown agent {cur.like!r}"
            )
        cur = agents_by_name[cur.like]

    merged: dict[str, Field] = {}
    for item in reversed(chain):
        for fld in item.fields:
            merged[fld.name] = fld
    return tuple(merged.values())


@dataclass(frozen=True)
class Env:
    """UVM environment specification grouping agents and environment options."""

    name: str
    agents: tuple[Agent, ...]
    scoreboard: bool = True
    ref_model: bool = False
    coverage: bool = False

    def __post_init__(self) -> None:
        _check_ident("Env", "name", self.name)
        owner = f"Env {self.name!r}"
        agents = _to_tuple(owner, "agents", self.agents, Agent, "Agent")
        by_name: dict[str, Agent] = {}
        reserved = {"cfg", "vsqr", "has_scoreboard", "build_sub_cfgs"}
        reserved.update(name for name, enabled in (
            ("scb", self.scoreboard), ("ref_model", self.ref_model), ("cov", self.coverage),
        ) if enabled)
        for ag in agents:
            if ag.name in reserved:
                raise _spec_error(f"{owner}: agent name {ag.name!r} is reserved")
            if ag.name in by_name:
                raise _spec_error(f"{owner}: duplicate agent name {ag.name!r}")
            by_name[ag.name] = ag
        for ag in agents:
            if ag.like is not None:
                if ag.like not in by_name:
                    raise _spec_error(
                        f"{owner}: agent {ag.name!r} has like={ag.like!r}, "
                        "which is not defined in this Env"
                    )
                resolve_agent_fields(ag, by_name)
        object.__setattr__(self, "agents", agents)
        _check_bool(owner, "scoreboard", self.scoreboard)
        _check_bool(owner, "ref_model", self.ref_model)
        _check_bool(owner, "coverage", self.coverage)


@dataclass(frozen=True)
class Tbspec:
    """Loaded ``tbspec.py`` specification."""

    envs: tuple[Env, ...] = ()
    interfaces: tuple[Interface, ...] = ()
    agents: tuple[Agent, ...] = ()

    @property
    def ENVS(self) -> tuple[Env, ...]:
        return self.envs

    @property
    def INTERFACES(self) -> tuple[Interface, ...]:
        return self.interfaces

    @property
    def AGENTS(self) -> tuple[Agent, ...]:
        return self.agents

    def get_env(self, name: str) -> Env | None:
        return next((e for e in self.envs if e.name == name), None)

    def get_agent(self, name: str) -> Agent | None:
        return next((a for a in self.agents if a.name == name), None)

    def get_interface(self, name: str) -> Interface | None:
        return next((i for i in self.interfaces if i.name == name), None)


def _user_line(exc: BaseException, path: Path) -> int | None:
    resolved = path.resolve()
    if (
        isinstance(exc, SyntaxError)
        and exc.lineno
        and exc.filename
        and (exc.filename == str(path) or Path(exc.filename).resolve() == resolved)
    ):
        return exc.lineno
    line = None
    for frame in traceback.extract_tb(exc.__traceback__):
        if frame.filename == str(path) or Path(frame.filename).resolve() == resolved:
            line = frame.lineno
    return line


def _extract_group(
    namespace: Mapping[str, object],
    plural: str,
    singular: str,
    cls: type,
    path: Path,
) -> tuple[list, bool]:
    items: list = []
    found = False
    if plural in namespace and namespace[plural] is not None:
        found = True
        raw = namespace[plural]
        if callable(raw):
            raw = raw()
        if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
            raise _spec_error(
                f"{path.name}: {plural} must be a list or tuple of {cls.__name__}, "
                f"got {type(raw).__name__}",
                file=path,
            )
        for item in raw:
            if not isinstance(item, cls):
                raise _spec_error(
                    f"{path.name}: {plural} items must be {cls.__name__}, got {item!r}",
                    file=path,
                )
            items.append(item)
    if singular in namespace and namespace[singular] is not None:
        found = True
        single = namespace[singular]
        if not isinstance(single, cls):
            raise _spec_error(
                f"{path.name}: {singular} must be {cls.__name__}, got {single!r}",
                file=path,
            )
        items.append(single)
    return items, found


def load_tbspec(root: Path) -> Tbspec:
    """Execute ``tbspec.py`` under ``root`` (or direct file path) and return a :class:`Tbspec`."""
    target = Path(root)
    path = target if target.is_file() else target / TBSPEC_NAME
    if not path.is_file():
        raise ConfigError(f"{TBSPEC_NAME} not found in {root}", code="CFG_NOT_FOUND")

    root_dir = str(path.parent)
    sys.path.insert(0, root_dir)
    try:
        namespace = runpy.run_path(str(path), run_name="__solouvm_tbspec__")
    except ConfigError as exc:
        if exc.file is None:
            exc.file = str(path)
            if exc.line is None:
                exc.line = _user_line(exc, path)
        raise
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        last = traceback.format_exception_only(type(exc), exc)[-1].strip()
        raise ConfigError(
            f"{path.name} raised an exception: {last}",
            code="CFG_LOAD_FAILED",
            file=path,
            line=_user_line(exc, path),
        ) from exc
    finally:
        with contextlib.suppress(ValueError):
            sys.path.remove(root_dir)

    envs_list, has_env = _extract_group(namespace, "ENVS", "ENV", Env, path)
    ifaces_list, has_iface = _extract_group(namespace, "INTERFACES", "INTERFACE", Interface, path)
    agents_list, has_agent = _extract_group(namespace, "AGENTS", "AGENT", Agent, path)

    if not (has_env or has_iface or has_agent):
        for name, val in namespace.items():
            if name.startswith("_"):
                continue
            if isinstance(val, Env):
                envs_list.append(val)
                has_env = True
            elif isinstance(val, Interface):
                ifaces_list.append(val)
                has_iface = True
            elif isinstance(val, Agent):
                agents_list.append(val)
                has_agent = True

    if not (has_env or has_iface or has_agent):
        raise _spec_error(
            f"{path.name} must define ENVS, INTERFACES, or AGENTS",
            file=path,
        )

    env_by_name: dict[str, Env] = {}
    for env in envs_list:
        if env.name in env_by_name:
            raise _spec_error(f"{path.name}: duplicate env name {env.name!r}", file=path)
        env_by_name[env.name] = env

    agent_by_name: dict[str, Agent] = {}
    for env in envs_list:
        for ag in env.agents:
            if ag.name in agent_by_name and agent_by_name[ag.name] != ag:
                raise _spec_error(
                    f"{path.name}: conflicting agent definitions for {ag.name!r}",
                    file=path,
                )
            agent_by_name.setdefault(ag.name, ag)
    for ag in agents_list:
        if ag.name in agent_by_name and agent_by_name[ag.name] != ag:
            raise _spec_error(
                f"{path.name}: conflicting agent definitions for {ag.name!r}",
                file=path,
            )
        agent_by_name.setdefault(ag.name, ag)

    for ag in agent_by_name.values():
        if ag.like is not None:
            resolve_agent_fields(ag, agent_by_name)

    iface_by_name: dict[str, Interface] = {}
    for ag in agent_by_name.values():
        iface = ag.interface
        if iface.name in iface_by_name and iface_by_name[iface.name] != iface:
            raise _spec_error(
                f"{path.name}: conflicting interface definitions for {iface.name!r}",
                file=path,
            )
        iface_by_name.setdefault(iface.name, iface)
    for iface in ifaces_list:
        if iface.name in iface_by_name and iface_by_name[iface.name] != iface:
            raise _spec_error(
                f"{path.name}: conflicting interface definitions for {iface.name!r}",
                file=path,
            )
        iface_by_name.setdefault(iface.name, iface)

    return Tbspec(
        envs=tuple(env_by_name.values()),
        interfaces=tuple(iface_by_name.values()),
        agents=tuple(agent_by_name.values()),
    )
