"""Config schema: defaults, per-key validators and the ``Config`` dataclasses (config.md §2)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from solouvm.backends.base import override_keys
from solouvm.errors import ConfigError
from solouvm.model import Checks, Waiver
from solouvm.seeds import parse_seeds
from solouvm.units import parse_duration

# ------------------------------------------------ 默认值（与 config.md §2.2 ~ §2.10 逐项对应）

# None 表示"必填"或"无默认值"；必填键在合并后检查
DEFAULTS: dict[str, object] = {
    "project": None,  # None = 项目根目录名
    "simulator": None,  # 必填
    "top": None,  # 必填
    "uvm_test": None,
    "incdirs": [],
    "defines": [],
    "builds": {"default": {}},
    "regress": {
        "seeds": "random:1",
        "jobs": 4,
        "retry": 0,
        "retry_on": ["error"],
        "wave_on_fail": False,
        "max_fail": None,
        "budget": None,
    },
    "run": {"timeout": "20m", "hang": None, "opts": [], "env": {}, "mem_limit": None},
    "checks": {
        "max_uvm_error": 0,
        "max_uvm_fatal": 0,
        "max_uvm_warning": None,
        "fail_regex": [],
        "pass_regex": [],
        "env_fail_regex": None,  # None = 后端的 license_regex
        "sim_errors": True,
        "waivers": [],
    },
    "job": {"pre": "", "post": ""},
    "sim": {},
    "providers": {},
    "provider": "local",
    "check": {
        "slang": "slang",
        "slang_args": [],
        "uvm_src": "${UVM_HOME}/src",
        "exclude": [],
        "faketop": True,
        "where": "auto",
    },
    "case_dirs": [],
    "system_doc": [],
    "triage": {
        "pass_policy": "changed",
        "group_by": "signature",
        "packet_limit": 20000,
        "debug_dir": "docs/debug",
        "weak_signals": True,
    },
    "doc": {"lang": "zh", "out": "docs/cases"},
    "history": {"keep": 50},
    "limits": {"max_jobs": 2000},
    "plugins": [],
    "uvm": {"templates": None},
}

REQUIRED = ("simulator", "top")

BUILD_DEFAULTS: dict[str, object] = {
    "defines": [],
    "incdirs": [],
    "compile_opts": [],
    "run_opts": [],
    "coverage": False,
    "timeout": "2h",
}

# 结果状态（verdict.md §2），retry_on 只能取这些值
RESULT_STATUSES = (
    "pass",
    "xfail",
    "fail",
    "xpass",
    "timeout",
    "killed",
    "incomplete",
    "error",
    "skip",
    "false_pass",
)

# ------------------------------------------------ 校验器：(键路径, 值) -> 规范化后的值

Validator = Callable[[str, object], object]


def invalid(path: str, expected: str, value: object) -> ConfigError:
    return ConfigError(f"{path}: expected {expected}, got {value!r}", code="CFG_INVALID")


def _is_int(value: object) -> bool:
    # bool 是 int 的子类，必须排除
    return isinstance(value, int) and not isinstance(value, bool)


def v_any(path: str, value: object) -> object:
    return value


def v_str(path: str, value: object) -> object:
    if not isinstance(value, str):
        raise invalid(path, "str", value)
    return value


def v_nonempty_str(path: str, value: object) -> object:
    if not isinstance(value, str) or not value.strip():
        raise invalid(path, "non-empty str", value)
    return value


def v_bool(path: str, value: object) -> object:
    if not isinstance(value, bool):
        raise invalid(path, "bool", value)
    return value


def v_int(minimum: int) -> Validator:
    def check(path: str, value: object) -> object:
        if not _is_int(value) or value < minimum:  # type: ignore[operator]
            raise invalid(path, f"int >= {minimum}", value)
        return value

    return check


def v_optional(inner: Validator, expected: str) -> Validator:
    def check(path: str, value: object) -> object:
        if value is None:
            return None
        try:
            return inner(path, value)
        except ConfigError:
            raise invalid(path, f"{expected} or None", value) from None

    return check


def v_str_list(path: str, value: object) -> object:
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise invalid(path, "list[str]", value)
    return list(value)


def v_str_or_list(path: str, value: object) -> object:
    if isinstance(value, str):
        return value
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, str) for v in value):
        raise invalid(path, "str or list[str]", value)
    return list(value)


def v_str_dict(path: str, value: object) -> object:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise invalid(path, "dict[str, str]", value)
    return dict(value)


def v_choice(*choices: object) -> Validator:
    def check(path: str, value: object) -> object:
        # 用 type 一起比较，避免 0 == False 之类的混淆
        if not any(type(value) is type(c) and value == c for c in choices):
            raise invalid(path, "one of " + ", ".join(repr(c) for c in choices), value)
        return value

    return check


def v_duration(path: str, value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise invalid(path, "duration such as '30m' or seconds", value)
    try:
        parse_duration(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}", code="CFG_INVALID") from None
    return value


def v_seeds(path: str, value: object) -> object:
    if not isinstance(value, (str, list)):
        raise invalid(path, "list[int] or str like '1-5,42' / 'random:N'", value)
    try:
        parse_seeds(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}", code="CFG_INVALID") from None
    return list(value) if isinstance(value, list) else value


def v_regex_list(path: str, value: object) -> object:
    # 正则能否编译交给 Checks 校验；这里只看形状
    return v_str_list(path, value)


def v_status_list(path: str, value: object) -> object:
    items = v_str_list(path, value)
    check = v_choice(*RESULT_STATUSES)
    return [check(path, item) for item in items]  # type: ignore[union-attr]


def v_waivers(path: str, value: object) -> object:
    if not isinstance(value, (list, tuple)) or not all(isinstance(v, Waiver) for v in value):
        raise invalid(path, "list[Waiver]", value)
    return list(value)


# ---------------------------------------------------------------- 键规格树
# dict = 嵌套键（未知键报 CFG_UNKNOWN_KEY）；"*" = 任意名字（builds、providers）；函数 = 叶子

ANY_KEY = "*"

BUILD_SPEC: dict[str, object] = {
    "defines": v_str_list,
    "incdirs": v_str_list,
    "compile_opts": v_str_list,
    "run_opts": v_str_list,
    "coverage": v_bool,
    "timeout": v_duration,
}

PROVIDER_SPEC: dict[str, object] = {
    "transport": {
        "kind": v_choice("local", "ssh"),
        "host": v_str,
        "workdir": v_str,
        "ssh_opts": v_str_or_list,
    },
    "launcher": {
        "kind": v_choice("make", "lsf"),
        "queue": v_str,
        "resources": v_str,
        "extra": v_str_list,
    },
    "sync": {
        "kind": v_choice("none", "rsync", "git"),
        "exclude": v_str_list,
        "delete": v_bool,
        "remote": v_str,
        "branch": v_str,
    },
    "setup": v_str,
}

SPEC: dict[str, object] = {
    "project": v_optional(v_nonempty_str, "non-empty str"),
    "simulator": v_optional(v_nonempty_str, "non-empty str"),
    "top": v_optional(v_nonempty_str, "non-empty str"),
    "uvm_test": v_optional(v_nonempty_str, "non-empty str"),
    "incdirs": v_str_list,
    "defines": v_str_list,
    "builds": {ANY_KEY: BUILD_SPEC},
    "regress": {
        "seeds": v_seeds,
        "jobs": v_int(1),
        "retry": v_int(0),
        "retry_on": v_status_list,
        "wave_on_fail": v_choice(False, "per_signature", "all"),
        "max_fail": v_optional(v_int(1), "int >= 1"),
        "budget": v_optional(v_duration, "duration"),
    },
    "run": {
        "timeout": v_duration,
        "hang": v_optional(v_duration, "duration"),
        "opts": v_str_list,
        "env": v_str_dict,
        "mem_limit": v_optional(v_nonempty_str, "non-empty str"),
    },
    "checks": {
        "max_uvm_error": v_int(0),
        "max_uvm_fatal": v_int(0),
        "max_uvm_warning": v_optional(v_int(0), "int >= 0"),
        "fail_regex": v_regex_list,
        "pass_regex": v_regex_list,
        "env_fail_regex": v_optional(v_regex_list, "list[str]"),
        "sim_errors": v_bool,
        "waivers": v_waivers,
    },
    "job": {"pre": v_str, "post": v_str},
    # 命令可写成字符串或列表；类型细节在合并后交给 get_backend 校验
    "sim": dict.fromkeys(sorted(override_keys()), v_any),
    "providers": {ANY_KEY: PROVIDER_SPEC},
    "provider": v_nonempty_str,
    "check": {
        "slang": v_nonempty_str,
        "slang_args": v_str_list,
        "uvm_src": v_str,
        "exclude": v_str_list,
        "faketop": v_bool,
        "where": v_choice("local", "remote", "auto"),
    },
    "case_dirs": v_str_list,
    "system_doc": v_str_list,
    "triage": {
        "pass_policy": v_choice("none", "changed", "all"),
        "group_by": v_choice("signature", "case", "job"),
        "packet_limit": v_int(1),
        "debug_dir": v_nonempty_str,
        "weak_signals": v_bool,
    },
    "doc": {"lang": v_nonempty_str, "out": v_nonempty_str},
    "history": {"keep": v_int(1)},
    "limits": {"max_jobs": v_int(1)},
    "plugins": v_str_list,
    "uvm": {"templates": v_optional(v_nonempty_str, "non-empty str")},
}

# ------------------------------------------------ 子结构（字段与 config.md §2.3 ~ §2.10 一一对应）


@dataclass
class BuildConfig:
    defines: list[str] = field(default_factory=list)
    incdirs: list[str] = field(default_factory=list)
    compile_opts: list[str] = field(default_factory=list)
    run_opts: list[str] = field(default_factory=list)
    coverage: bool = False
    timeout: str | int = "2h"


@dataclass
class RegressConfig:
    seeds: list[int] | str = "random:1"
    jobs: int = 4
    retry: int = 0
    retry_on: list[str] = field(default_factory=lambda: ["error"])
    wave_on_fail: bool | str = False
    max_fail: int | None = None
    budget: str | int | None = None


@dataclass
class RunConfig:
    timeout: str | int = "20m"
    hang: str | int | None = None
    opts: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    mem_limit: str | None = None


@dataclass
class JobHooks:
    pre: str = ""
    post: str = ""


@dataclass
class TransportConfig:
    kind: str = "local"
    host: str | None = None
    workdir: str | None = None
    ssh_opts: list[str] | str = field(default_factory=list)
    options: dict[str, object] = field(default_factory=dict)


@dataclass
class LauncherConfig:
    kind: str = "make"
    queue: str | None = None
    resources: str | None = None
    extra: list[str] = field(default_factory=list)
    options: dict[str, object] = field(default_factory=dict)


@dataclass
class SyncConfig:
    kind: str = "none"
    exclude: list[str] = field(default_factory=list)
    delete: bool = False
    remote: str | None = None
    branch: str | None = None
    options: dict[str, object] = field(default_factory=dict)


@dataclass
class ProviderConfig:
    name: str
    transport: TransportConfig = field(default_factory=TransportConfig)
    launcher: LauncherConfig = field(default_factory=LauncherConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    setup: str = ""


@dataclass
class CheckConfig:
    slang: str = "slang"
    slang_args: list[str] = field(default_factory=list)
    uvm_src: str = "${UVM_HOME}/src"
    exclude: list[str] = field(default_factory=list)
    faketop: bool = True
    where: str = "auto"


@dataclass
class TriageConfig:
    pass_policy: str = "changed"
    group_by: str = "signature"
    packet_limit: int = 20000
    debug_dir: str = "docs/debug"
    weak_signals: bool = True


@dataclass
class DocConfig:
    lang: str = "zh"
    out: str = "docs/cases"


@dataclass
class HistoryConfig:
    keep: int = 50


@dataclass
class LimitsConfig:
    max_jobs: int = 2000


@dataclass
class UvmConfig:
    templates: str | None = None


@dataclass
class Config:
    root: Path
    project: str
    simulator: str
    top: str
    uvm_test: str | None
    incdirs: list[str]
    defines: list[str]
    builds: dict[str, BuildConfig]
    regress: RegressConfig
    run: RunConfig
    checks: Checks
    job: JobHooks
    sim: dict[str, object]
    providers: dict[str, ProviderConfig]
    provider: str
    check: CheckConfig
    case_dirs: list[str]
    system_doc: list[str]
    triage: TriageConfig
    doc: DocConfig
    history: HistoryConfig
    limits: LimitsConfig
    plugins: list[str]
    uvm: UvmConfig
    sources: dict[str, str] = field(default_factory=dict)  # 键路径 -> default|conf|local|cli
