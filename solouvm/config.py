"""Loading ``solouvm.py``: the single source of project configuration."""

from __future__ import annotations

import os
import re
import runpy
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .model import ProjectDefaults
from .util import parse_duration, parse_seeds

CONFIG_NAME = "solouvm.conf.py"
WORKDIR_NAME = ".solouvm"

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

DEFAULTS: dict[str, Any] = {
    "project": "solouvm",
    "simulator": "vcs",
    "top": None,
    "uvm_test": None,
    "workdir": WORKDIR_NAME,
    "filelist": "filelist.py",
    "case_file": "case.py",
    "incdirs": [],
    "defines": [],
    "regress": {"seeds": 1, "retry": 0, "jobs": 4},
    "run": {"timeout": "20m", "opts": []},
    "checks": {
        "pass_regex": [],
        "fail_regex": [],
        "max_uvm_error": 0,
        "max_uvm_fatal": 0,
        "max_uvm_warning": None,
    },
    "sim": {},
}


@dataclass
class Config:
    """Validated, absolute-path configuration."""

    root: Path
    config_file: Path
    project: str
    simulator: str
    top: str | None
    uvm_test: str | None
    workdir: Path
    filelist_file: Path
    case_file: Path
    incdirs: list[str]
    defines: list[str]
    seeds: list[int]
    retry: int
    jobs: int
    timeout: str | int | float
    run_opts: list[str]
    checks: dict[str, Any]
    sim: dict[str, Any]
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def out_root(self) -> Path:
        return self.workdir / "out"

    def case_defaults(self) -> ProjectDefaults:
        return ProjectDefaults(
            top=self.top,
            uvm_test=self.uvm_test,
            timeout=self.timeout,
            retry=self.retry,
        )

    def path(self, value: str) -> Path:
        """Resolve a config-supplied path against the project root."""
        candidate = Path(value).expanduser()
        return candidate if candidate.is_absolute() else (self.root / candidate)


def find_config(start: Path) -> Path:
    """Walk ``start`` then its parents looking for ``solouvm.py``."""
    current = start.resolve()
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise ConfigError(
        f"cannot find {CONFIG_NAME} in {current} or any parent directory",
        hint=f"pass -c/--config PATH or create a {CONFIG_NAME} at the project root",
    )


def resolve_config_path(cli_value: str | None, cwd: Path) -> Path:
    if cli_value is None:
        return find_config(cwd)
    target = Path(cli_value).expanduser()
    if not target.is_absolute():
        target = cwd / target
    if target.is_dir():
        target = target / CONFIG_NAME
    if not target.is_file():
        raise ConfigError(f"config file not found: {target}")
    if target.suffix != ".py":
        raise ConfigError(
            f"config must be a .py file, got {target.name}",
            hint="solouvm only accepts Python configuration",
        )
    return target


def expand_vars(value: Any, env: Mapping[str, str] | None = None) -> Any:
    """Expand ``${VAR}`` / ``${VAR:-default}`` inside strings, lists and dicts."""
    env = os.environ if env is None else env

    def one(text: str) -> str:
        def repl(match: re.Match[str]) -> str:
            name, default = match.group(1), match.group(2)
            found = env.get(name)
            if found is not None:
                return found
            if default is not None:
                return default
            raise ConfigError(
                f"environment variable {name} is not set and has no default",
                hint=f"write ${{{name}:-fallback}} to provide one",
            )

        return _VAR.sub(repl, text)

    if isinstance(value, str):
        return one(value)
    if isinstance(value, list):
        return [expand_vars(item, env) for item in value]
    if isinstance(value, tuple):
        return tuple(expand_vars(item, env) for item in value)
    if isinstance(value, dict):
        return {key: expand_vars(item, env) for key, item in value.items()}
    return value


def load_config(config_file: Path, *, env: Mapping[str, str] | None = None) -> Config:
    """Execute ``solouvm.py`` with ``runpy`` and validate its ``CFG`` dict."""
    raw = _read_cfg(config_file, env)
    merged = _merge(DEFAULTS, raw)
    root = config_file.parent

    project = _expect_str(merged, "project")
    simulator = _expect_str(merged, "simulator")
    workdir = Path(str(merged["workdir"]))
    workdir = workdir if workdir.is_absolute() else root / workdir

    regress = _expect_mapping(merged, "regress")
    run = _expect_mapping(merged, "run")
    checks = _expect_mapping(merged, "checks")
    sim = _expect_mapping(merged, "sim")

    seeds = parse_seeds(regress.get("seeds", 1))
    retry = _expect_int(regress, "retry", default=0, minimum=0)
    jobs = _expect_int(regress, "jobs", default=4, minimum=1)
    timeout = run.get("timeout", "20m")
    parse_duration(timeout)  # validate early

    return Config(
        root=root,
        config_file=config_file,
        project=project,
        simulator=simulator,
        top=merged.get("top"),
        uvm_test=merged.get("uvm_test"),
        workdir=workdir,
        filelist_file=_config_path(root, merged.get("filelist", "filelist.py")),
        case_file=_config_path(root, merged.get("case_file", "case.py")),
        incdirs=[str(item) for item in merged.get("incdirs") or []],
        defines=[str(item) for item in merged.get("defines") or []],
        seeds=seeds,
        retry=retry,
        jobs=jobs,
        timeout=timeout,
        run_opts=[str(item) for item in run.get("opts") or []],
        checks={
            "pass_regex": [str(x) for x in checks.get("pass_regex") or []],
            "fail_regex": [str(x) for x in checks.get("fail_regex") or []],
            "max_uvm_error": _expect_int(checks, "max_uvm_error", default=0, minimum=0),
            "max_uvm_fatal": _expect_int(checks, "max_uvm_fatal", default=0, minimum=0),
            "max_uvm_warning": checks.get("max_uvm_warning"),
        },
        sim={str(k): v for k, v in sim.items()},
        raw=merged,
    )


def _read_cfg(config_file: Path, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    import sys

    previous_path = list(sys.path)
    sys.path.insert(0, str(config_file.parent))
    try:
        namespace = runpy.run_path(str(config_file), run_name="solouvm_config")
    except ConfigError:
        raise
    except Exception as exc:  # pragma: no cover - surfaced verbatim to the user
        raise ConfigError(
            f"failed to load {config_file}: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        sys.path[:] = previous_path

    if "CFG" not in namespace:
        raise ConfigError(
            f"{config_file} does not define a module-level CFG dict",
            hint="add `CFG = {...}` describing your project",
        )
    cfg = namespace["CFG"]
    if not isinstance(cfg, dict):
        raise ConfigError(f"CFG in {config_file} must be a dict, got {type(cfg).__name__}")
    return expand_vars(cfg, env)


def _merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), Mapping):
            out[key] = _merge(base[key], value)
        else:
            out[key] = value
    return out


def _config_path(root: Path, value: Any) -> Path:
    candidate = Path(str(value)).expanduser()
    return candidate if candidate.is_absolute() else root / candidate


def _expect_str(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"CFG[{key!r}] must be a non-empty string, got {value!r}")
    return value


def _expect_mapping(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    value = payload.get(key) or {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"CFG[{key!r}] must be a dict, got {type(value).__name__}")
    return value


def _expect_int(
    payload: Mapping[str, Any], key: str, *, default: int, minimum: int
) -> int:
    value = payload.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"CFG[{key!r}] must be an int, got {value!r}")
    if value < minimum:
        raise ConfigError(f"CFG[{key!r}] must be >= {minimum}, got {value}")
    return value
