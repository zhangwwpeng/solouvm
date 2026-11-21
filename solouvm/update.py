"""Online git self-update workflow for installed ``solouvm`` CLI (``solouvm update``)."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from solouvm import __version__
from solouvm.errors import ConfigError, InfraError
from solouvm.gen.shell import check_no_newlines

DEFAULT_UPDATE_REPO = "https://github.com/zhangwwpeng/solouvm"

__all__ = [
    "DEFAULT_UPDATE_REPO",
    "read_install_meta",
    "resolve_update_target",
    "run_update",
]

_OFFLINE_HINT = (
    "check network access, git URL/ref, or update offline by copying the release "
    "directory and running ./install.sh"
)


def _read_meta_file(meta_path: Path) -> dict[str, str] | None:
    if not meta_path.is_file():
        return None
    try:
        raw = meta_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    data: dict[str, str] = {}
    for line in raw.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            data[key.strip()] = value.strip()
    return data or None


def read_install_meta(package_root: Path | None = None) -> dict[str, str] | None:
    """Read ``.install-meta`` next to the installed ``solouvm/`` package, if present."""
    root = (
        Path(package_root)
        if package_root is not None
        else Path(__file__).resolve().parent.parent
    )
    return _read_meta_file(root / ".install-meta")


def resolve_update_target(
    *,
    mode: str | None = None,
    prefix: str | None = None,
    python_bin: str | None = None,
    meta: dict[str, str] | None = None,
) -> dict[str, str | None]:
    """Determine target ``mode``, ``prefix``, and ``python`` for ``install.sh``."""
    if prefix is not None and mode in ("user", "system"):
        raise ConfigError(
            f"cannot combine --prefix with --{mode}",
            code="CFG_INVALID",
            hint="pass only one of --user, --system, or --prefix",
        )
    if mode is not None and mode not in ("user", "system", "prefix"):
        raise ConfigError(f"invalid install mode {mode!r}", code="CFG_INVALID")

    resolved_python = (
        check_no_newlines(python_bin.strip(), field="python")
        if python_bin is not None
        else None
    )
    if python_bin is not None and not resolved_python:
        raise ConfigError("--python cannot be empty", code="CFG_INVALID")

    if prefix is not None:
        clean_prefix = check_no_newlines(prefix.strip(), field="prefix")
        if not clean_prefix:
            raise ConfigError("--prefix cannot be empty", code="CFG_INVALID")
        eff_mode = "prefix"
        eff_prefix = str(Path(clean_prefix).expanduser())
    elif mode == "system":
        eff_mode = "system"
        eff_prefix = "/usr/local"
    elif mode == "user":
        eff_mode = "user"
        eff_prefix = str(Path.home() / ".local")
    elif meta is not None and meta.get("mode") in ("user", "system", "prefix") and meta.get(
        "prefix"
    ):
        eff_mode = meta["mode"]
        eff_prefix = meta["prefix"]
        if resolved_python is None:
            meta_py = meta.get("python", "")
            if meta_py and Path(meta_py).is_file():
                resolved_python = meta_py
    else:
        eff_mode = "user"
        eff_prefix = str(Path.home() / ".local")

    return {"mode": eff_mode, "prefix": eff_prefix, "python": resolved_python}


def _nearest_existing_ancestor(path: Path) -> Path:
    cur = path
    while not cur.exists():
        parent = cur.parent
        if parent == cur:
            break
        cur = parent
    return cur


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _run_git_cmd(args: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    cmd = ["git", *args] if cwd is None else ["git", "-C", str(cwd), *args]
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_git_env(),
            check=False,
        )
    except OSError as exc:
        raise InfraError(
            f"failed to execute git: {exc}",
            code="PROVIDER_FAILED",
            hint=_OFFLINE_HINT,
        ) from exc


def _clone_repo(repo: str, ref: str | None, clone_dir: Path) -> str:
    if ref is None:
        proc = _run_git_cmd(["clone", "--depth", "1", "--", repo, str(clone_dir)])
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            raise InfraError(
                f"git clone {repo!r} failed: {detail or f'exit code {proc.returncode}'}",
                code="PROVIDER_FAILED",
                hint=_OFFLINE_HINT,
            )
    else:
        proc = _run_git_cmd(
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
                step_proc = _run_git_cmd(args, cwd=clone_dir)
                if step_proc.returncode != 0:
                    failed = step_proc
                    break
            if failed is not None:
                detail = (
                    failed.stderr.strip()
                    or failed.stdout.strip()
                    or proc.stderr.strip()
                    or proc.stdout.strip()
                )
                raise InfraError(
                    f"git fetch {repo!r} at {ref!r} failed: "
                    f"{detail or f'exit code {failed.returncode}'}",
                    code="PROVIDER_FAILED",
                    hint=_OFFLINE_HINT,
                )

    rev_proc = _run_git_cmd(["rev-parse", "--short", "HEAD"], cwd=clone_dir)
    if rev_proc.returncode != 0:
        raise InfraError(
            "failed to resolve fetched git commit",
            code="PROVIDER_FAILED",
            hint=_OFFLINE_HINT,
        )
    return rev_proc.stdout.strip()


def run_update(
    *,
    repo: str = DEFAULT_UPDATE_REPO,
    ref: str | None = None,
    mode: str | None = None,
    prefix: str | None = None,
    python_bin: str | None = None,
    dry_run: bool = False,
    package_root: Path | None = None,
) -> dict[str, object]:
    """Clone ``repo`` (default GitHub upstream) and run its ``install.sh``."""
    clean_repo = check_no_newlines(str(repo).strip(), field="repo") if isinstance(repo, str) else ""
    if not clean_repo or clean_repo.startswith("-"):
        raise ConfigError(
            f"invalid git repository {repo!r}",
            code="CFG_INVALID",
            hint=f"pass a git URL such as {DEFAULT_UPDATE_REPO}",
        )

    clean_ref: str | None = None
    if ref is not None:
        clean_ref = check_no_newlines(ref.strip(), field="ref")
        if not clean_ref or clean_ref.startswith("-"):
            raise ConfigError(
                f"invalid git ref {ref!r}",
                code="CFG_INVALID",
                hint="pass a branch, tag, or commit hash",
            )

    current_meta = read_install_meta(package_root)
    target = resolve_update_target(
        mode=mode,
        prefix=prefix,
        python_bin=python_bin,
        meta=current_meta,
    )
    eff_mode = str(target["mode"])
    eff_prefix = str(target["prefix"])
    eff_python = target["python"]

    install_args = (
        ["--prefix", eff_prefix]
        if eff_mode == "prefix"
        else [f"--{eff_mode}"]
    )
    if eff_python:
        install_args.extend(["--python", eff_python])

    target_lib = Path(eff_prefix) / "lib" / "solouvm"
    target_bin = Path(eff_prefix) / "bin" / "solouvm"
    target_meta = _read_meta_file(target_lib / ".install-meta")
    prev_version = (
        (target_meta or {}).get("version")
        or (current_meta or {}).get("version")
        or __version__
    )

    if dry_run:
        git_cmd = ["git", "clone", "--depth", "1"]
        if clean_ref is not None:
            git_cmd.extend(["--branch", clean_ref])
        git_cmd.extend(["--", clean_repo, "<tmp>/src"])
        return {
            "repo": clean_repo,
            "ref": clean_ref,
            "commit": None,
            "previous_version": prev_version,
            "version": None,
            "mode": eff_mode,
            "prefix": eff_prefix,
            "python": eff_python,
            "bin_path": str(target_bin),
            "lib_dir": str(target_lib),
            "dry_run": True,
            "commands": [
                " ".join(git_cmd),
                " ".join(["bash", "<tmp>/src/install.sh", *install_args]),
            ],
        }

    if shutil.which("git") is None:
        raise InfraError(
            "git executable is required for 'solouvm update'",
            code="PROVIDER_FAILED",
            hint=_OFFLINE_HINT,
        )

    anchor = _nearest_existing_ancestor(Path(eff_prefix))
    if not anchor.is_dir() or not os.access(anchor, os.W_OK):
        hint = (
            "run 'sudo solouvm update --system' or choose '--user' / '--prefix'"
            if eff_mode == "system"
            else f"ensure {anchor} is writable or choose another '--prefix'"
        )
        raise InfraError(
            f"no write permission under {eff_prefix!r} (checked {str(anchor)!r})",
            code="PROVIDER_FAILED",
            hint=hint,
        )

    with tempfile.TemporaryDirectory(prefix="solouvm-update-") as tmp_dir:
        clone_dir = Path(tmp_dir) / "src"
        commit = _clone_repo(clean_repo, clean_ref, clone_dir)
        installer = clone_dir / "install.sh"
        if not installer.is_file() or not (clone_dir / "solouvm").is_dir():
            raise InfraError(
                f"repository {clean_repo!r} is missing install.sh or solouvm/",
                code="PROVIDER_FAILED",
                hint=_OFFLINE_HINT,
            )
        try:
            proc = subprocess.run(
                ["bash", str(installer), *install_args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise InfraError(
                f"failed to run install.sh: {exc}",
                code="PROVIDER_FAILED",
            ) from exc
        combined = "\n".join(p for p in (proc.stdout.strip(), proc.stderr.strip()) if p)
        if proc.returncode != 0:
            raise InfraError(
                f"install.sh failed with exit code {proc.returncode}: {combined}",
                code="PROVIDER_FAILED",
                hint=_OFFLINE_HINT,
            )

    new_meta = _read_meta_file(target_lib / ".install-meta")
    new_version = (new_meta or {}).get("version", prev_version)
    return {
        "repo": clean_repo,
        "ref": clean_ref,
        "commit": commit,
        "previous_version": prev_version,
        "version": new_version,
        "mode": eff_mode,
        "prefix": eff_prefix,
        "python": (new_meta or {}).get("python") or eff_python,
        "bin_path": str(target_bin),
        "lib_dir": str(target_lib),
        "dry_run": False,
        "output": combined,
    }
