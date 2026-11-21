"""Git sync: require a clean working tree, sync code via git, push artifacts via transport."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from pathlib import Path

from solouvm.errors import InfraError
from solouvm.gen.shell import check_no_newlines
from solouvm.providers.base import Transport, log_command

__all__ = ["GitSync"]


def _run_git(tree: str, args: list[str]) -> str:
    cmd = ["git", "-C", tree, *args]
    log_command(cmd)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        raise InfraError(f"git failed: {exc}", code="PROVIDER_FAILED") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        msg = f"git {' '.join(args)} failed with exit code {proc.returncode}"
        if detail:
            msg += f": {detail}"
        raise InfraError(msg, code="PROVIDER_FAILED")
    return proc.stdout


def _rel_to_tree(tree: Path, raw: str) -> str:
    text = check_no_newlines(os.fspath(raw), field="sync file")
    candidate = Path(text)
    if candidate.is_absolute():
        resolved_tree = tree.resolve()
        if candidate.is_relative_to(tree):
            return candidate.relative_to(tree).as_posix()
        if candidate.resolve().is_relative_to(resolved_tree):
            return candidate.resolve().relative_to(resolved_tree).as_posix()
        raise InfraError(
            f"generated file {text!r} is outside project root {str(tree)!r}",
            code="PROVIDER_FAILED",
        )
    rel = text.replace(os.sep, "/") if os.sep != "/" else text
    while rel.startswith("./"):
        rel = rel[2:]
    if rel in ("", ".") or rel.startswith("../") or "/../" in rel or rel == "..":
        raise InfraError(
            f"generated file {text!r} escapes project root {str(tree)!r}",
            code="PROVIDER_FAILED",
        )
    return rel


class GitSync:
    """Sync source tree through git (clean tree required) and generated files via ``put``."""

    kind: str = "git"

    def __init__(self, remote: str = "origin", branch: str | None = None) -> None:
        self.remote: str = check_no_newlines(remote or "origin", field="sync.remote")
        self.branch: str | None = (
            check_no_newlines(branch, field="sync.branch") if branch is not None else None
        )

    def push(self, transport: Transport, tree: str, files: Iterable[str]) -> None:
        """Check clean tree, sync git commit on B, then transfer ``files`` via ``transport.put``."""
        tree_str = os.fspath(tree)
        status_out = _run_git(tree_str, ["status", "--porcelain"])
        if status_out.strip():
            raise InfraError(
                "working tree has uncommitted changes; commit or use rsync sync",
                code="GIT_DIRTY",
                hint="set sync.kind = 'rsync' in solouvm.local.py",
            )

        commit = _run_git(tree_str, ["rev-parse", "HEAD"]).strip()

        if self.branch:
            branch_commit = _run_git(
                tree_str,
                ["rev-parse", "--verify", f"refs/heads/{self.branch}^{{commit}}"],
            ).strip()
            if branch_commit != commit:
                raise InfraError(
                    f"HEAD ({commit[:12]}) does not match configured sync.branch "
                    f"{self.branch!r} ({branch_commit[:12]})",
                    code="PROVIDER_FAILED",
                    hint=(
                        f"checkout {self.branch!r}, unset sync.branch, or use sync.kind = 'rsync'"
                    ),
                )
            _run_git(tree_str, ["push", self.remote, self.branch])
            target_checkout = f"{self.remote}/{self.branch}"
            transport.exec(["git", "fetch", self.remote, self.branch], cwd=".")
            transport.exec(["git", "checkout", "-B", self.branch, target_checkout], cwd=".")
        else:
            remote_probe = subprocess.run(
                ["git", "-C", tree_str, "remote", "get-url", self.remote],
                capture_output=True,
                text=True,
                check=False,
            )
            if remote_probe.returncode == 0:
                branch_probe = subprocess.run(
                    ["git", "-C", tree_str, "symbolic-ref", "--short", "-q", "HEAD"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if branch_probe.returncode == 0 and branch_probe.stdout.strip():
                    _run_git(tree_str, ["push", self.remote, branch_probe.stdout.strip()])
            transport.exec(["git", "fetch", self.remote], cwd=".")
            transport.exec(["git", "checkout", commit], cwd=".")

        verify_rc = transport.exec(
            ["sh", "-c", 'test "$(git rev-parse HEAD)" = "$1"', "_", commit],
            cwd=".",
            check=False,
        )
        if verify_rc != 0:
            raise InfraError(
                f"remote checkout HEAD does not match local commit {commit[:12]}",
                code="PROVIDER_FAILED",
            )

        tree_path = Path(tree_str)
        seen: set[str] = set()
        for raw_file in files:
            rel = _rel_to_tree(tree_path, raw_file)
            if rel in seen:
                continue
            seen.add(rel)
            local_file = tree_path / rel
            try:
                data = local_file.read_bytes()
            except OSError as exc:
                raise InfraError(
                    f"cannot read generated file {local_file}: {exc}",
                    code="PROVIDER_FAILED",
                ) from exc
            transport.put(rel, data)
