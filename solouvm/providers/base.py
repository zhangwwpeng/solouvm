"""Provider interfaces (Transport / Launcher / Sync), dataclass and plugin registry."""

from __future__ import annotations

import contextlib
import importlib
import inspect
import re
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from solouvm.errors import ConfigError

if TYPE_CHECKING:
    from solouvm.model import Plan

__all__ = [
    "BUILTIN_LAUNCHER_KINDS",
    "BUILTIN_SYNC_KINDS",
    "BUILTIN_TRANSPORT_KINDS",
    "Launcher",
    "Process",
    "Provider",
    "ProviderRegistry",
    "Sync",
    "Transport",
    "build_scoped_registry",
    "default_registry",
    "instantiate_plugin",
    "known_kinds",
    "load_plugins",
    "log_command",
    "register_launcher",
    "register_sync",
    "register_transport",
]

BUILTIN_TRANSPORT_KINDS: frozenset[str] = frozenset({"local", "ssh"})
BUILTIN_LAUNCHER_KINDS: frozenset[str] = frozenset({"make", "lsf"})
BUILTIN_SYNC_KINDS: frozenset[str] = frozenset({"none", "rsync", "git"})

_KIND_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*\Z")

_TRANSPORT_METHODS = ("exec", "spawn", "read_lines", "read_many", "put", "exists")
_LAUNCHER_METHODS = ("make_vars", "jobs", "stop")
_SYNC_METHODS = ("push",)


class Process(Protocol):
    """Handle to a spawned background or foreground subprocess."""

    pid: int

    def wait(self, timeout: float | None = None) -> int: ...

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...


class Transport(Protocol):
    """Execute commands and read/write files on execution host B."""

    def exec(self, argv: list[str], *, cwd: str, check: bool = True) -> int: ...

    def spawn(self, argv: list[str], *, cwd: str) -> Process: ...

    def read_lines(
        self,
        path: str,
        *,
        keywords: list[str] | None = None,
        lines: tuple[int, int] | None = None,
    ) -> Iterator[tuple[int, str]]: ...

    def read_many(self, pattern: str) -> dict[str, str]: ...

    def put(self, path: str, data: bytes) -> None: ...

    def exists(self, path: str) -> bool: ...


class Launcher(Protocol):
    """Schedule jobs on B and terminate an active run."""

    def make_vars(self, plan: Plan) -> dict[str, str]: ...

    def jobs(self, requested: int) -> int: ...

    def stop(self, transport: Transport, run_id: str) -> None: ...


class Sync(Protocol):
    """Transfer source tree and generated artifacts from A to B."""

    def push(self, transport: Transport, tree: str, files: Iterable[str]) -> None: ...


@dataclass
class Provider:
    """Resolved execution provider combining Transport, Launcher, Sync and setup snippet."""

    name: str
    transport: Transport
    launcher: Launcher
    sync: Sync
    setup: str = ""


def log_command(cmd: Sequence[str] | str) -> None:
    """Trace an external command via ``solouvm.ui.command_log`` when ``ui`` is loaded."""
    ui_mod = sys.modules.get("solouvm.ui")
    if ui_mod is None:
        return
    fn = getattr(ui_mod, "command_log", None)
    if callable(fn):
        fn(cmd)


def _check_kind_name(kind: object, role: str) -> str:
    if not isinstance(kind, str) or not _KIND_RE.match(kind):
        raise ConfigError(
            f"invalid {role} plugin kind {kind!r}: expected a non-empty identifier",
            code="CFG_INVALID",
        )
    return kind


def _has_methods(cls: type[Any], methods: tuple[str, ...]) -> bool:
    return all(callable(getattr(cls, name, None)) for name in methods)


def _detect_role(cls: type[Any], spec: str) -> str:
    matches: list[str] = []
    if _has_methods(cls, _TRANSPORT_METHODS):
        matches.append("transport")
    if _has_methods(cls, _LAUNCHER_METHODS):
        matches.append("launcher")
    if _has_methods(cls, _SYNC_METHODS):
        matches.append("sync")
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        explicit = getattr(cls, "plugin_role", None)
        if explicit in matches:
            return str(explicit)
        raise ConfigError(
            f"plugin {spec!r} matches multiple provider roles ({', '.join(matches)})",
            code="CFG_INVALID",
        )
    raise ConfigError(
        f"plugin {spec!r} ({cls.__name__}) does not implement Transport, Launcher, or Sync",
        code="CFG_INVALID",
    )


def _infer_kind(cls: type[Any], role: str) -> str:
    if hasattr(cls, "kind"):
        raw = cls.kind
        return _check_kind_name(raw, role)
    suffix = role.capitalize()
    name = cls.__name__
    if name.endswith(suffix) and len(name) > len(suffix):
        name = name[: -len(suffix)]
    return _check_kind_name(name.lower(), role)


def _parse_plugin_spec(spec: object) -> tuple[str, str]:
    if not isinstance(spec, str) or not spec.strip():
        raise ConfigError(
            f"invalid plugin path {spec!r}: expected 'module.Class'", code="CFG_INVALID"
        )
    text = spec.strip()
    if ":" in text:
        mod_name, attr_name = text.split(":", 1)
    elif "." in text:
        mod_name, attr_name = text.rsplit(".", 1)
    else:
        raise ConfigError(
            f"invalid plugin path {spec!r}: expected 'module.Class'",
            code="CFG_INVALID",
        )
    if not mod_name or not attr_name or "." in attr_name or ":" in attr_name:
        raise ConfigError(
            f"invalid plugin path {spec!r}: expected 'module.Class'",
            code="CFG_INVALID",
        )
    return mod_name, attr_name


def _root_module_exists(root: Path, mod_name: str) -> bool:
    rel = Path(*mod_name.split("."))
    return (root / f"{rel}.py").is_file() or (root / rel / "__init__.py").is_file()


def _import_plugin_attr(spec: str, root: Path | None) -> Any:
    mod_name, attr_name = _parse_plugin_spec(spec)
    resolved_root = root.resolve() if root is not None else None
    root_str = str(resolved_root) if resolved_root is not None else None

    if root_str is not None:
        sys.path.insert(0, root_str)
    try:
        # If an earlier test or project cached a module of the same name from another root,
        # drop stale entries so this project's local module is imported cleanly.
        if (
            resolved_root is not None
            and mod_name in sys.modules
            and _root_module_exists(resolved_root, mod_name)
        ):
            cached = sys.modules[mod_name]
            cached_file = getattr(cached, "__file__", None)
            if cached_file is not None:
                try:
                    in_root = Path(cached_file).resolve().is_relative_to(resolved_root)
                except OSError:
                    in_root = False
                if not in_root:
                    sys.modules.pop(mod_name, None)
                    importlib.invalidate_caches()
        try:
            module = importlib.import_module(mod_name)
        except Exception as exc:
            raise ConfigError(
                f"failed to import plugin {spec!r}: {exc}",
                code="CFG_INVALID",
            ) from exc
    finally:
        if root_str is not None:
            with contextlib.suppress(ValueError):
                sys.path.remove(root_str)

    if not hasattr(module, attr_name):
        raise ConfigError(
            f"plugin {spec!r}: module {mod_name!r} has no attribute {attr_name!r}",
            code="CFG_INVALID",
        )
    return getattr(module, attr_name)


class ProviderRegistry:
    """Registry mapping transport, launcher and sync kind names to implementation classes."""

    def __init__(self) -> None:
        self._transports: dict[str, type[Any]] = {}
        self._launchers: dict[str, type[Any]] = {}
        self._syncs: dict[str, type[Any]] = {}

    def copy(self) -> ProviderRegistry:
        clone = ProviderRegistry()
        clone._transports = dict(self._transports)
        clone._launchers = dict(self._launchers)
        clone._syncs = dict(self._syncs)
        return clone

    def _register(
        self,
        table: dict[str, type[Any]],
        builtins: frozenset[str],
        role: str,
        required_methods: tuple[str, ...],
        kind: str,
        cls: type[Any],
        *,
        allow_builtin: bool = False,
    ) -> None:
        norm = _check_kind_name(kind, role)
        if not allow_builtin and norm in builtins:
            raise ConfigError(
                f"cannot override built-in {role} kind {norm!r}",
                code="CFG_INVALID",
            )
        if not isinstance(cls, type) or not _has_methods(cls, required_methods):
            raise ConfigError(
                f"invalid {role} plugin for kind {norm!r}: "
                f"must implement {', '.join(required_methods)}",
                code="CFG_INVALID",
            )
        table[norm] = cls

    def register_transport(self, kind: str, cls: type[Any], *, allow_builtin: bool = False) -> None:
        self._register(
            self._transports,
            BUILTIN_TRANSPORT_KINDS,
            "transport",
            _TRANSPORT_METHODS,
            kind,
            cls,
            allow_builtin=allow_builtin,
        )

    def register_launcher(self, kind: str, cls: type[Any], *, allow_builtin: bool = False) -> None:
        self._register(
            self._launchers,
            BUILTIN_LAUNCHER_KINDS,
            "launcher",
            _LAUNCHER_METHODS,
            kind,
            cls,
            allow_builtin=allow_builtin,
        )

    def register_sync(self, kind: str, cls: type[Any], *, allow_builtin: bool = False) -> None:
        self._register(
            self._syncs,
            BUILTIN_SYNC_KINDS,
            "sync",
            _SYNC_METHODS,
            kind,
            cls,
            allow_builtin=allow_builtin,
        )

    def transport_cls(self, kind: str) -> type[Any] | None:
        return self._transports.get(kind)

    def launcher_cls(self, kind: str) -> type[Any] | None:
        return self._launchers.get(kind)

    def sync_cls(self, kind: str) -> type[Any] | None:
        return self._syncs.get(kind)

    def known_kinds(self) -> dict[str, tuple[str, ...]]:
        return {
            "transport": tuple(sorted(BUILTIN_TRANSPORT_KINDS | self._transports.keys())),
            "launcher": tuple(sorted(BUILTIN_LAUNCHER_KINDS | self._launchers.keys())),
            "sync": tuple(sorted(BUILTIN_SYNC_KINDS | self._syncs.keys())),
        }

    def load_plugins(
        self, plugins: Iterable[str], root: Path | None = None
    ) -> list[tuple[str, str, type[Any]]]:
        """Import and register each plugin class from ``plugins``."""
        loaded: list[tuple[str, str, type[Any]]] = []
        for spec in plugins:
            obj = _import_plugin_attr(spec, root)
            if not isinstance(obj, type):
                raise ConfigError(
                    f"plugin {spec!r} must be a class, got {type(obj).__name__}",
                    code="CFG_INVALID",
                )
            role = _detect_role(obj, spec)
            kind = _infer_kind(obj, role)
            if role == "transport":
                self.register_transport(kind, obj)
            elif role == "launcher":
                self.register_launcher(kind, obj)
            else:
                self.register_sync(kind, obj)
            loaded.append((role, kind, obj))
        return loaded


_DEFAULT_REGISTRY = ProviderRegistry()


def default_registry() -> ProviderRegistry:
    """Return the global provider registry."""
    return _DEFAULT_REGISTRY


def register_transport(kind: str, cls: type[Any]) -> None:
    """Register a custom ``Transport`` class under ``kind``."""
    _DEFAULT_REGISTRY.register_transport(kind, cls)


def register_launcher(kind: str, cls: type[Any]) -> None:
    """Register a custom ``Launcher`` class under ``kind``."""
    _DEFAULT_REGISTRY.register_launcher(kind, cls)


def register_sync(kind: str, cls: type[Any]) -> None:
    """Register a custom ``Sync`` class under ``kind``."""
    _DEFAULT_REGISTRY.register_sync(kind, cls)


def known_kinds() -> dict[str, tuple[str, ...]]:
    """Return registered kind names for transport, launcher, and sync."""
    return _DEFAULT_REGISTRY.known_kinds()


def load_plugins(
    plugins: Iterable[str], root: Path | None = None
) -> list[tuple[str, str, type[Any]]]:
    """Load and register plugins into the global registry."""
    return _DEFAULT_REGISTRY.load_plugins(plugins, root=root)


def build_scoped_registry(plugins: Iterable[str], root: Path | None = None) -> ProviderRegistry:
    """Return a registry copy with ``plugins`` loaded without mutating global state."""
    scoped = _DEFAULT_REGISTRY.copy()
    plugin_list = list(plugins)
    if plugin_list:
        scoped.load_plugins(plugin_list, root=root)
    return scoped


def instantiate_plugin(cls: type[Any], config_dict: Mapping[str, object]) -> Any:
    """Instantiate a plugin class supporting no-arg, single-dict, or keyword signatures."""
    cfg = dict(config_dict)
    sig = inspect.signature(cls)
    params = list(sig.parameters.values())
    try:
        if not params:
            return cls()
        if len(params) == 1:
            param = params[0]
            if param.kind == inspect.Parameter.VAR_KEYWORD:
                return cls(**cfg)
            if param.kind == inspect.Parameter.VAR_POSITIONAL:
                return cls(cfg)
            if param.kind == inspect.Parameter.KEYWORD_ONLY:
                if param.name in cfg:
                    return cls(**{param.name: cfg[param.name]})
                if param.default is not inspect.Parameter.empty:
                    return cls()
                return cls(**{param.name: cfg})
            if param.name in cfg and param.name != "kind":
                return cls(**{param.name: cfg[param.name]})
            return cls(cfg)

        has_varkw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
        first = params[0]
        rest_optional = all(
            p.default is not inspect.Parameter.empty
            or p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            for p in params[1:]
        )
        if (
            first.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
            and first.name in ("config", "cfg", "options", "settings", "data")
            and rest_optional
        ):
            return cls(cfg)
        if has_varkw:
            return cls(**cfg)
        accepted = {
            p.name: cfg[p.name]
            for p in params
            if p.name in cfg
            and p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        }
        return cls(**accepted)
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(
            f"failed to initialize plugin {cls.__name__}: {exc}",
            code="CFG_INVALID",
        ) from exc
