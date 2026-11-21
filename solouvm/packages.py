"""Git/local package fetching, priority ordering, dependency checking, and file collection."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from solouvm.collect.files import (
    DEFAULT_FILELIST_PRIORITY,
    GROUPS,
    PACKAGE_GROUPS,
    FileList,
    LoadedFilelist,
    _Collector,
    load_filelist_meta,
)
from solouvm.config.schema import Config, PackageConfig
from solouvm.errors import ConfigError, InfraError, SolouvmError
from solouvm.providers.base import log_command

PACKAGES_DIR = ".solouvm/packages"

__all__ = [
    "PACKAGES_DIR",
    "ResolvedPackage",
    "check_host_depends",
    "collect_project_files",
    "ensure_packages",
    "list_packages",
    "package_sync_files",
    "resolve_packages",
    "resolve_project_files",
    "sync_packages",
    "update_packages",
]


@dataclass(frozen=True)
class ResolvedPackage:
    """Resolved package ready for ordered file collection."""

    name: str
    config: PackageConfig
    repo_dir: Path
    root_dir: Path
    source_kind: str
    fetched: bool
    priority: int
    depends: tuple[str, ...]
    filelist: LoadedFilelist


def _run_git(args: Sequence[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["git", *args] if cwd is None else ["git", "-C", str(cwd), *args]
    log_command(cmd)
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise InfraError(
            f"failed to run git: {exc}",
            code="PROVIDER_FAILED",
            hint="ensure 'git' is installed and reachable on A",
        ) from exc


def _clone_package_repo(pkg_name: str, repo: str, ref: str | None, clone_dir: Path) -> str:
    if ref is None:
        proc = _run_git(["clone", "--depth", "1", "--", repo, str(clone_dir)])
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise InfraError(
                f"package {pkg_name!r}: git clone {repo!r} failed: "
                f"{detail or f'exit code {proc.returncode}'}",
                code="PROVIDER_FAILED",
                hint=f"check packages.{pkg_name}.git URL and git access",
            )
    else:
        proc = _run_git(
            ["clone", "--depth", "1", "--branch", ref, "--", repo, str(clone_dir)]
        )
        if proc.returncode != 0:
            if clone_dir.exists():
                shutil.rmtree(clone_dir)
            clone_dir.mkdir(parents=True, exist_ok=True)
            failed: subprocess.CompletedProcess[str] | None = None
            for args in (
                ["init"],
                ["fetch", "--depth", "1", "--", repo, ref],
                ["checkout", "--detach", "FETCH_HEAD"],
            ):
                step = _run_git(args, cwd=clone_dir)
                if step.returncode != 0:
                    failed = step
                    break
            if failed is not None:
                if clone_dir.exists():
                    shutil.rmtree(clone_dir)
                full_proc = _run_git(["clone", "--no-checkout", "--", repo, str(clone_dir)])
                co_proc = (
                    _run_git(["checkout", "--detach", ref], cwd=clone_dir)
                    if full_proc.returncode == 0
                    else None
                )
                if co_proc is None or co_proc.returncode != 0:
                    err_proc = co_proc if co_proc is not None else full_proc
                    detail = (
                        err_proc.stderr.strip()
                        or err_proc.stdout.strip()
                        or failed.stderr.strip()
                        or proc.stderr.strip()
                    )
                    raise InfraError(
                        f"package {pkg_name!r}: git fetch {repo!r} at {ref!r} failed: "
                        f"{detail or f'exit code {err_proc.returncode}'}",
                        code="PROVIDER_FAILED",
                        hint=f"check packages.{pkg_name}.git URL and packages.{pkg_name}.ref",
                    )

    rev_proc = _run_git(["rev-parse", "--short", "HEAD"], cwd=clone_dir)
    if rev_proc.returncode != 0:
        return ""
    return rev_proc.stdout.strip()


def _best_effort_commit(repo_dir: Path) -> str | None:
    if not (repo_dir / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _pkg_repo_dir(root: Path, pkg_cfg: PackageConfig) -> Path:
    if pkg_cfg.path is not None:
        raw = Path(pkg_cfg.path).expanduser()
        target = raw if raw.is_absolute() else (root / raw)
        return Path(os.path.normpath(target.absolute()))
    return root / PACKAGES_DIR / pkg_cfg.name


def _pkg_root_dir(repo_dir: Path, pkg_cfg: PackageConfig) -> Path:
    if pkg_cfg.subdir:
        return repo_dir / pkg_cfg.subdir
    return repo_dir


def _select_package_names(config: Config, names: Sequence[str] | None) -> list[str]:
    if names is None or len(names) == 0:
        return list(config.packages.keys())
    unknown = [n for n in names if n not in config.packages]
    if unknown:
        avail = ", ".join(config.packages.keys()) or "(none)"
        raise ConfigError(
            f"unknown package(s): {', '.join(repr(n) for n in unknown)} (configured: {avail})",
            code="CFG_INVALID",
        )
    deduped: list[str] = []
    for n in names:
        if n not in deduped:
            deduped.append(n)
    return deduped


def ensure_packages(
    config: Config,
    *,
    force_update: bool = False,
    names: Sequence[str] | None = None,
) -> list[dict[str, object]]:
    """Ensure configured packages exist on disk; clone git repos only when missing (or forced)."""
    root = Path(config.root)
    selected = _select_package_names(config, names)
    packages_base = root / PACKAGES_DIR
    results: list[dict[str, object]] = []

    for pkg_name in selected:
        pkg_cfg = config.packages[pkg_name]
        repo_dir = _pkg_repo_dir(root, pkg_cfg)

        if pkg_cfg.path is not None:
            if not repo_dir.is_dir():
                raise ConfigError(
                    f"local package {pkg_name!r} directory does not exist: {repo_dir}",
                    code="FILES_MISSING",
                    hint=f"check packages.{pkg_name}.path in config",
                )
            results.append(
                {
                    "name": pkg_name,
                    "source_kind": "path",
                    "path": str(repo_dir),
                    "fetched": False,
                    "status": "local",
                }
            )
            continue

        assert pkg_cfg.git is not None
        if repo_dir.exists() and not repo_dir.is_dir():
            raise ConfigError(
                f"package path {repo_dir} exists but is not a directory",
                code="CFG_INVALID",
                hint=f"remove {repo_dir} and retry",
            )

        if not force_update and repo_dir.is_dir():
            results.append(
                {
                    "name": pkg_name,
                    "source_kind": "git",
                    "path": str(repo_dir),
                    "fetched": False,
                    "status": "cached",
                }
            )
            continue

        packages_base.mkdir(parents=True, exist_ok=True)
        tmp_dir = packages_base / f".{pkg_name}.tmp.{os.getpid()}"
        backup_dir = packages_base / f".{pkg_name}.bak.{os.getpid()}"
        for candidate in (tmp_dir, backup_dir):
            if candidate.exists():
                shutil.rmtree(candidate)

        try:
            commit = _clone_package_repo(pkg_name, pkg_cfg.git, pkg_cfg.ref, tmp_dir)
            if repo_dir.is_dir():
                repo_dir.replace(backup_dir)
                try:
                    tmp_dir.replace(repo_dir)
                except OSError as exc:
                    if not repo_dir.exists() and backup_dir.exists():
                        backup_dir.replace(repo_dir)
                    raise InfraError(
                        f"failed to swap updated package {pkg_name!r}: {exc}",
                        code="PROVIDER_FAILED",
                    ) from exc
                shutil.rmtree(backup_dir, ignore_errors=True)
                status = "updated"
            else:
                tmp_dir.replace(repo_dir)
                status = "cloned"
        finally:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)

        results.append(
            {
                "name": pkg_name,
                "source_kind": "git",
                "path": str(repo_dir),
                "commit": commit,
                "fetched": True,
                "status": status,
            }
        )

    return results


def resolve_packages(config: Config, *, auto_fetch: bool = True) -> list[ResolvedPackage]:
    """Ensure packages are present, load each ``filelist.py``, sort by priority and check deps."""
    if not config.packages:
        return []

    root = Path(config.root)
    fetched_map: dict[str, bool] = {}
    if auto_fetch:
        for item in ensure_packages(config, force_update=False):
            fetched_map[str(item["name"])] = bool(item["fetched"])

    staged: list[tuple[int, int, ResolvedPackage]] = []
    for decl_idx, pkg_cfg in enumerate(config.packages.values()):
        repo_dir = _pkg_repo_dir(root, pkg_cfg)
        if not repo_dir.is_dir():
            hint = (
                f"run 'solouvm pkg sync {pkg_cfg.name}'"
                if pkg_cfg.git is not None
                else f"check packages.{pkg_cfg.name}.path in config"
            )
            raise ConfigError(
                f"package {pkg_cfg.name!r} directory does not exist: {repo_dir}",
                code="FILES_MISSING",
                hint=hint,
            )

        root_dir = _pkg_root_dir(repo_dir, pkg_cfg)
        if not root_dir.is_dir():
            raise ConfigError(
                f"package {pkg_cfg.name!r} subdir {pkg_cfg.subdir!r} not found in {repo_dir}",
                code="FILES_MISSING",
                hint=f"check packages.{pkg_cfg.name}.subdir in config",
            )

        try:
            fl_meta = load_filelist_meta(root_dir)
        except ConfigError as exc:
            if exc.code == "FILES_MISSING":
                raise ConfigError(
                    f"package {pkg_cfg.name!r} is missing required filelist.py in {root_dir}",
                    code="FILES_MISSING",
                    file=root_dir / "filelist.py",
                ) from None
            raise

        eff_priority = pkg_cfg.priority if pkg_cfg.priority is not None else fl_meta.priority
        source_kind = "path" if pkg_cfg.path is not None else "git"
        resolved = ResolvedPackage(
            name=pkg_cfg.name,
            config=pkg_cfg,
            repo_dir=repo_dir,
            root_dir=root_dir,
            source_kind=source_kind,
            fetched=fetched_map.get(pkg_cfg.name, False),
            priority=eff_priority,
            depends=fl_meta.depends,
            filelist=fl_meta,
        )
        staged.append((eff_priority, decl_idx, resolved))

    staged.sort(key=lambda item: (item[0], item[1]))
    sorted_pkgs = [item[2] for item in staged]

    loaded: set[str] = set()
    for pkg in sorted_pkgs:
        for dep in pkg.depends:
            if dep == pkg.name:
                raise ConfigError(
                    f"package {pkg.name!r} cannot depend on itself",
                    code="CFG_INVALID",
                    file=pkg.filelist.path,
                )
            if dep not in config.packages:
                raise ConfigError(
                    f"package {pkg.name!r} depends on {dep!r}, "
                    "which is not configured in CFG['packages']",
                    code="CFG_INVALID",
                    file=pkg.filelist.path,
                    hint=f"add {dep!r} to CFG['packages'] in solouvm.conf.py",
                )
            if dep not in loaded:
                raise ConfigError(
                    f"package {pkg.name!r} (priority={pkg.priority}) depends on {dep!r}, "
                    "which has not been loaded yet (lower numeric priority compiles first)",
                    code="CFG_INVALID",
                    file=pkg.filelist.path,
                    hint=(
                        f"give {dep!r} a lower numeric priority than {pkg.name!r} "
                        "or place it earlier in CFG['packages']"
                    ),
                )
        loaded.add(pkg.name)

    return sorted_pkgs


def check_host_depends(
    host_filelist: LoadedFilelist,
    loaded_packages: set[str],
) -> None:
    """Validate host ``filelist.py`` ``DEPENDS`` against loaded package names."""
    for dep in host_filelist.depends:
        if dep not in loaded_packages:
            raise ConfigError(
                f"host filelist.py depends on package {dep!r}, "
                "which is not configured in CFG['packages']",
                code="CFG_INVALID",
                file=host_filelist.path,
                hint=f"add {dep!r} to CFG['packages'] in solouvm.conf.py",
            )


def resolve_project_files(
    config: Config,
    *,
    auto_fetch: bool = True,
) -> tuple[FileList, list[ResolvedPackage], LoadedFilelist]:
    """Resolve packages + host ``filelist.py``, check ``DEPENDS``, and return merged files."""
    root = Path(config.root)
    pkgs = resolve_packages(config, auto_fetch=auto_fetch)
    host_filelist = load_filelist_meta(root)
    check_host_depends(host_filelist, {pkg.name for pkg in pkgs})

    collector = _Collector(root)
    for pkg in pkgs:
        collector.add_spec(
            pkg.filelist.spec,
            base_dir=pkg.root_dir,
            source_prefix=f"pkg:{pkg.name}:",
            allowed_groups=PACKAGE_GROUPS,
            use_base_for_dash_f=True,
        )
    collector.add_spec(
        host_filelist.spec,
        base_dir=collector.root,
        source_prefix="",
        allowed_groups=GROUPS,
        use_base_for_dash_f=False,
    )
    return collector.finalize(), pkgs, host_filelist


def collect_project_files(config: Config, *, auto_fetch: bool = True) -> FileList:
    """Collect merged compile files from configured packages and the host ``filelist.py``."""
    flist, _, _ = resolve_project_files(config, auto_fetch=auto_fetch)
    return flist


def package_sync_files(root: Path, config: Config) -> list[str]:
    """Return root-relative POSIX file paths for cloned git packages to include in B sync."""
    base_root = Path(root)
    files: list[str] = []
    seen: set[str] = set()
    for pkg_cfg in config.packages.values():
        if pkg_cfg.git is None:
            continue
        pkg_dir = base_root / PACKAGES_DIR / pkg_cfg.name
        if not pkg_dir.is_dir():
            continue
        for dirpath, dirs, filenames in os.walk(pkg_dir, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in (".git", "__pycache__"))
            for fname in sorted(filenames):
                candidate = Path(dirpath) / fname
                if not candidate.is_file():
                    continue
                rel = candidate.relative_to(base_root).as_posix()
                if rel not in seen:
                    seen.add(rel)
                    files.append(rel)
    return sorted(files)


def list_packages(config: Config) -> list[dict[str, object]]:
    """Inspect configured packages on disk without cloning or modifying them."""
    root = Path(config.root)
    rows: list[dict[str, object]] = []
    for pkg_cfg in config.packages.values():
        repo_dir = _pkg_repo_dir(root, pkg_cfg)
        root_dir = _pkg_root_dir(repo_dir, pkg_cfg)
        source_kind = "path" if pkg_cfg.path is not None else "git"
        exists = repo_dir.is_dir() and root_dir.is_dir()
        commit = _best_effort_commit(repo_dir) if (exists and source_kind == "git") else None
        priority: int | str = pkg_cfg.priority if pkg_cfg.priority is not None else "-"
        depends: list[str] = []
        filelist_ok = False
        error: str | None = None

        if not repo_dir.is_dir():
            error = "not fetched" if source_kind == "git" else "directory missing"
        elif not root_dir.is_dir():
            error = f"subdir missing: {pkg_cfg.subdir}"
        else:
            try:
                meta = load_filelist_meta(root_dir)
                filelist_ok = True
                if pkg_cfg.priority is None:
                    priority = meta.priority
                depends = list(meta.depends)
            except SolouvmError as exc:
                error = exc.message

        if priority == "-":
            priority = DEFAULT_FILELIST_PRIORITY

        rows.append(
            {
                "name": pkg_cfg.name,
                "source_kind": source_kind,
                "source": pkg_cfg.git if source_kind == "git" else pkg_cfg.path,
                "ref": pkg_cfg.ref,
                "subdir": pkg_cfg.subdir,
                "path": str(repo_dir),
                "root_dir": str(root_dir),
                "exists": exists,
                "commit": commit,
                "priority": priority,
                "depends": depends,
                "filelist_ok": filelist_ok,
                "error": error,
            }
        )
    return rows


def _post_fetch_resolve(
    config: Config,
    ensure_results: list[dict[str, object]],
    names: Sequence[str] | None,
) -> dict[str, object]:
    root = Path(config.root)
    all_present = not names or all(
        _pkg_repo_dir(root, pkg_cfg).is_dir() for pkg_cfg in config.packages.values()
    )
    if all_present:
        resolved = resolve_packages(config, auto_fetch=False)
        order = [pkg.name for pkg in resolved]
    else:
        for item in ensure_results:
            pkg_cfg = config.packages[str(item["name"])]
            root_dir = _pkg_root_dir(_pkg_repo_dir(root, pkg_cfg), pkg_cfg)
            if not root_dir.is_dir():
                raise ConfigError(
                    f"package {pkg_cfg.name!r} subdir {pkg_cfg.subdir!r} not found",
                    code="FILES_MISSING",
                )
            load_filelist_meta(root_dir)
        order = [str(item["name"]) for item in ensure_results]
    return {
        "packages": ensure_results,
        "resolved_order": order,
    }


def sync_packages(config: Config, *, names: Sequence[str] | None = None) -> dict[str, object]:
    """Clone missing configured packages (skip existing) and validate dependency order."""
    ensure_results = ensure_packages(config, force_update=False, names=names)
    return _post_fetch_resolve(config, ensure_results, names)


def update_packages(config: Config, *, names: Sequence[str] | None = None) -> dict[str, object]:
    """Re-clone configured git packages (skipping local ``path`` packages) and validate order."""
    ensure_results = ensure_packages(config, force_update=True, names=names)
    return _post_fetch_resolve(config, ensure_results, names)
