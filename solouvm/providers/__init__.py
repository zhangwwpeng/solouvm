"""Provider lookup, registry bootstrap and exports (architecture.md §6)."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING, Any

from solouvm.errors import ConfigError
from solouvm.providers.base import (
    Launcher,
    Process,
    Provider,
    ProviderRegistry,
    Sync,
    Transport,
    build_scoped_registry,
    default_registry,
    instantiate_plugin,
    known_kinds,
    load_plugins,
    register_launcher,
    register_sync,
    register_transport,
)
from solouvm.providers.launcher_lsf import LsfLauncher
from solouvm.providers.launcher_make import MakeLauncher
from solouvm.providers.sync_git import GitSync
from solouvm.providers.sync_none import NoneSync
from solouvm.providers.sync_rsync import RsyncSync
from solouvm.providers.transport_local import LocalTransport
from solouvm.providers.transport_ssh import SshTransport

if TYPE_CHECKING:
    from solouvm.config.schema import (
        Config,
        LauncherConfig,
        ProviderConfig,
        SyncConfig,
        TransportConfig,
    )

__all__ = [
    "GitSync",
    "Launcher",
    "LocalTransport",
    "LsfLauncher",
    "MakeLauncher",
    "NoneSync",
    "Process",
    "Provider",
    "ProviderRegistry",
    "RsyncSync",
    "SshTransport",
    "Sync",
    "Transport",
    "build_scoped_registry",
    "get_provider",
    "known_kinds",
    "load_plugins",
    "register_launcher",
    "register_sync",
    "register_transport",
]

_reg = default_registry()
_reg.register_transport("local", LocalTransport, allow_builtin=True)
_reg.register_transport("ssh", SshTransport, allow_builtin=True)
_reg.register_launcher("make", MakeLauncher, allow_builtin=True)
_reg.register_launcher("lsf", LsfLauncher, allow_builtin=True)
_reg.register_sync("none", NoneSync, allow_builtin=True)
_reg.register_sync("rsync", RsyncSync, allow_builtin=True)
_reg.register_sync("git", GitSync, allow_builtin=True)


def _transport_dict(tc: TransportConfig) -> dict[str, object]:
    data: dict[str, object] = {"kind": tc.kind, **getattr(tc, "options", {})}
    if tc.host is not None:
        data.setdefault("host", tc.host)
    if tc.workdir is not None:
        data.setdefault("workdir", tc.workdir)
    if tc.ssh_opts:
        data.setdefault("ssh_opts", tc.ssh_opts)
    return data


def _launcher_dict(lc: LauncherConfig) -> dict[str, object]:
    data: dict[str, object] = {"kind": lc.kind, **getattr(lc, "options", {})}
    if lc.queue is not None:
        data.setdefault("queue", lc.queue)
    if lc.resources is not None:
        data.setdefault("resources", lc.resources)
    if lc.extra:
        data.setdefault("extra", list(lc.extra))
    return data


def _sync_dict(sc: SyncConfig) -> dict[str, object]:
    data: dict[str, object] = {"kind": sc.kind, **getattr(sc, "options", {})}
    if sc.exclude:
        data.setdefault("exclude", list(sc.exclude))
    if sc.delete:
        data.setdefault("delete", sc.delete)
    if sc.remote is not None:
        data.setdefault("remote", sc.remote)
    if sc.branch is not None:
        data.setdefault("branch", sc.branch)
    return data


def _build_transport(tc: TransportConfig, config: Config, registry: ProviderRegistry) -> Transport:
    if tc.kind == "local":
        return LocalTransport(root=config.root)
    if tc.kind == "ssh":
        if not tc.host or not tc.workdir:
            raise ConfigError(
                "ssh transport requires both 'host' and 'workdir'",
                code="CFG_INVALID",
            )
        opts = shlex.split(tc.ssh_opts) if isinstance(tc.ssh_opts, str) else list(tc.ssh_opts)
        return SshTransport(
            host=tc.host,
            workdir=tc.workdir,
            ssh_opts=opts,
            root=config.root,
        )
    cls: type[Any] | None = registry.transport_cls(tc.kind)
    if cls is None:
        raise ConfigError(
            f"unknown transport kind {tc.kind!r}",
            code="CFG_INVALID",
        )
    return instantiate_plugin(cls, _transport_dict(tc))  # type: ignore[no-any-return]


def _build_launcher(lc: LauncherConfig, registry: ProviderRegistry) -> Launcher:
    if lc.kind == "make":
        return MakeLauncher()
    if lc.kind == "lsf":
        return LsfLauncher(queue=lc.queue, resources=lc.resources, extra=lc.extra)
    cls = registry.launcher_cls(lc.kind)
    if cls is None:
        raise ConfigError(
            f"unknown launcher kind {lc.kind!r}",
            code="CFG_INVALID",
        )
    return instantiate_plugin(cls, _launcher_dict(lc))  # type: ignore[no-any-return]


def _build_sync(sc: SyncConfig, tc: TransportConfig, registry: ProviderRegistry) -> Sync:
    if sc.kind == "none":
        return NoneSync()
    if sc.kind == "rsync":
        opts = shlex.split(tc.ssh_opts) if isinstance(tc.ssh_opts, str) else list(tc.ssh_opts)
        return RsyncSync(
            exclude=sc.exclude,
            delete=sc.delete,
            remote_workdir=tc.workdir,
            host=tc.host,
            ssh_opts=opts,
        )
    if sc.kind == "git":
        return GitSync(remote=sc.remote or "origin", branch=sc.branch)
    cls = registry.sync_cls(sc.kind)
    if cls is None:
        raise ConfigError(
            f"unknown sync kind {sc.kind!r}",
            code="CFG_INVALID",
        )
    return instantiate_plugin(cls, _sync_dict(sc))  # type: ignore[no-any-return]


def get_provider(config: Config, name: str | None = None) -> Provider:
    """Resolve and instantiate provider ``name`` (or ``config.provider``)."""
    target_name = name if name is not None else config.provider
    pcfg: ProviderConfig | None = config.providers.get(target_name)
    if pcfg is None:
        if target_name == "local":
            return Provider(
                name="local",
                transport=LocalTransport(root=config.root),
                launcher=MakeLauncher(),
                sync=NoneSync(),
                setup="",
            )
        known = ", ".join(sorted({*config.providers.keys(), "local"}))
        raise ConfigError(
            f"unknown provider {target_name!r}; defined providers: {known}",
            code="CFG_INVALID",
        )

    registry = build_scoped_registry(config.plugins, root=config.root)
    transport = _build_transport(pcfg.transport, config, registry)
    launcher = _build_launcher(pcfg.launcher, registry)
    sync = _build_sync(pcfg.sync, pcfg.transport, registry)
    return Provider(
        name=target_name,
        transport=transport,
        launcher=launcher,
        sync=sync,
        setup=pcfg.setup,
    )
