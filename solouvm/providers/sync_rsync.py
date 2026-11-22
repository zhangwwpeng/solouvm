"""Two-step ``rsync`` sync for source tree plus explicit generated file manifest (H-08, H-09)."""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Iterable, Sequence
from pathlib import Path

from solouvm.errors import InfraError
from solouvm.gen.shell import check_no_newlines
from solouvm.providers.base import Transport, log_command

__all__ = ["RsyncSync"]


def _normalize_manifest(tree: Path, files: Iterable[str]) -> list[str]:
    """Convert generated file paths to deduplicated POSIX paths relative to ``tree``."""
    resolved_tree = tree.resolve()
    rel_files: list[str] = []
    seen: set[str] = set()
    for raw in files:
        text = check_no_newlines(os.fspath(raw), field="sync file")
        candidate = Path(text)
        if candidate.is_absolute():
            if candidate.is_relative_to(tree):
                rel = candidate.relative_to(tree).as_posix()
            elif candidate.resolve().is_relative_to(resolved_tree):
                rel = candidate.resolve().relative_to(resolved_tree).as_posix()
            else:
                raise InfraError(
                    f"generated file {text!r} is outside project root {str(tree)!r}",
                    code="PROVIDER_FAILED",
                )
        else:
            rel = text.replace(os.sep, "/") if os.sep != "/" else text
            while rel.startswith("./"):
                rel = rel[2:]
            if rel in ("", ".") or rel.startswith("../") or "/../" in rel or rel == "..":
                raise InfraError(
                    f"generated file {text!r} escapes project root {str(tree)!r}",
                    code="PROVIDER_FAILED",
                )
        if rel not in seen:
            seen.add(rel)
            rel_files.append(rel)
    return rel_files


class RsyncSync:
    """Sync source tree (excluding ``.git/`` and ``.solouvm/``) then explicit generated files."""

    kind: str = "rsync"

    def __init__(
        self,
        exclude: Sequence[str] = (),
        delete: bool = False,
        remote_workdir: str | None = None,
        host: str | None = None,
        ssh_opts: Sequence[str] | str = (),
    ) -> None:
        self.exclude: tuple[str, ...] = tuple(
            check_no_newlines(str(pat), field="sync.exclude") for pat in exclude
        )
        self.delete: bool = bool(delete)
        self.remote_workdir: str | None = (
            check_no_newlines(remote_workdir, field="sync.remote_workdir")
            if remote_workdir is not None
            else None
        )
        self.host: str | None = (
            check_no_newlines(host, field="sync.host") if host is not None else None
        )
        raw_opts = shlex.split(ssh_opts) if isinstance(ssh_opts, str) else list(ssh_opts)
        self.ssh_opts: tuple[str, ...] = tuple(
            check_no_newlines(str(opt), field="ssh_opts") for opt in raw_opts
        )

    def _dest_and_rsh(self, transport: Transport) -> tuple[str, list[str]]:
        host = self.host if self.host is not None else getattr(transport, "host", None)
        workdir = (
            self.remote_workdir
            if self.remote_workdir is not None
            else getattr(transport, "workdir", None)
        )
        if not workdir:
            raise InfraError(
                "rsync sync requires remote workdir on transport or sync configuration",
                code="PROVIDER_FAILED",
            )
        workdir_clean = workdir.rstrip("/") or "/"
        dest_dir = f"{workdir_clean}/" if workdir_clean != "/" else "/"
        if host:
            ssh_opts = self.ssh_opts or tuple(getattr(transport, "ssh_opts", ()))
            rsh_cmd = shlex.join(["ssh", *ssh_opts]) if ssh_opts else "ssh"
            return f"{host}:{dest_dir}", ["-e", rsh_cmd]
        return dest_dir, []

    def _run_rsync(self, cmd: list[str], *, input_text: str | None = None) -> None:
        log_command(cmd)
        try:
            proc = subprocess.run(
                cmd,
                input=input_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise InfraError(f"rsync failed: {exc}", code="PROVIDER_FAILED") from exc
        if proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            msg = f"rsync failed with exit code {proc.returncode}"
            if detail:
                msg += f": {detail}"
            raise InfraError(msg, code="PROVIDER_FAILED")

    def push(self, transport: Transport, tree: str, files: Iterable[str]) -> None:
        """Push source tree (step 1) and explicit generated files (step 2) via ``rsync``."""
        tree_path = Path(tree)
        manifest = _normalize_manifest(tree_path, files)
        src = f"{str(tree_path).rstrip('/')}/"
        dest, rsh_args = self._dest_and_rsh(transport)

        tree_cmd = ["rsync", "-a", "--checksum", *rsh_args]
        if self.delete:
            tree_cmd.append("--delete")
        tree_cmd.extend(["--exclude=.git/", "--exclude=.solouvm/"])
        for pat in self.exclude:
            tree_cmd.append(f"--exclude={pat}")
        tree_cmd.extend([src, dest])
        self._run_rsync(tree_cmd)

        if manifest:
            files_cmd = ["rsync", "-a", "--checksum", *rsh_args, "--files-from=-", src, dest]
            self._run_rsync(files_cmd, input_text="\n".join(manifest) + "\n")
