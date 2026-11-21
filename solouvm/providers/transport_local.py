"""Local transport executing commands and reading/writing files directly on A."""

from __future__ import annotations

import contextlib
import glob
import os
import shlex
import shutil
import signal
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path

from solouvm.errors import InfraError
from solouvm.providers.base import Process, log_command

__all__ = ["LocalTransport"]


class LocalTransport:
    """Transport implementation for local execution where A and B are the same host."""

    kind: str = "local"

    def __init__(self, root: str | Path | None = None) -> None:
        self.root: Path | None = Path(root) if root is not None else None

    def _resolve(self, path: str | Path) -> Path:
        target = Path(path)
        if self.root is None or target.is_absolute():
            return target
        return self.root / target

    def exec(self, argv: Sequence[str], *, cwd: str = ".", check: bool = True) -> int:
        """Run ``argv`` synchronously in ``cwd``; raise ``InfraError`` on failure if ``check``."""
        if not argv:
            raise InfraError("cannot execute empty command", code="PROVIDER_FAILED")
        cmd = [str(part) for part in argv]
        work_dir = str(self._resolve(cwd))
        log_command(cmd)
        try:
            proc = subprocess.run(cmd, cwd=work_dir, check=False)
        except OSError as exc:
            raise InfraError(
                f"failed to execute {cmd[0]!r}: {exc}",
                code="PROVIDER_FAILED",
            ) from exc
        if check and proc.returncode != 0:
            raise InfraError(
                f"command {shlex.join(cmd)!r} failed with exit code {proc.returncode}",
                code="PROVIDER_FAILED",
            )
        return proc.returncode

    def exec_output(
        self, argv: Sequence[str], *, cwd: str = ".", check: bool = True
    ) -> tuple[int, str]:
        """Run ``argv`` capturing combined stdout/stderr text."""
        if not argv:
            raise InfraError("cannot execute empty command", code="PROVIDER_FAILED")
        cmd = [str(part) for part in argv]
        work_dir = str(self._resolve(cwd))
        log_command(cmd)
        try:
            proc = subprocess.run(
                cmd,
                cwd=work_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise InfraError(
                f"failed to execute {cmd[0]!r}: {exc}",
                code="PROVIDER_FAILED",
            ) from exc
        if check and proc.returncode != 0:
            raise InfraError(
                f"command {shlex.join(cmd)!r} failed with exit code {proc.returncode}",
                code="PROVIDER_FAILED",
            )
        return proc.returncode, proc.stdout

    def exec_capture(
        self, argv: Sequence[str], *, cwd: str = ".", check: bool = True
    ) -> tuple[int, str, str]:
        """Run ``argv`` capturing separate stdout and stderr text."""
        if not argv:
            raise InfraError("cannot execute empty command", code="PROVIDER_FAILED")
        cmd = [str(part) for part in argv]
        work_dir = str(self._resolve(cwd))
        log_command(cmd)
        try:
            proc = subprocess.run(
                cmd,
                cwd=work_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise InfraError(
                f"failed to execute {cmd[0]!r}: {exc}",
                code="PROVIDER_FAILED",
            ) from exc
        if check and proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            msg = f"command {shlex.join(cmd)!r} failed with exit code {proc.returncode}"
            if detail:
                msg += f": {detail}"
            raise InfraError(msg, code="PROVIDER_FAILED")
        return proc.returncode, proc.stdout, proc.stderr

    def spawn(self, argv: Sequence[str], *, cwd: str = ".") -> Process:
        """Spawn ``argv`` in a new session so the process tree can be terminated cleanly."""
        if not argv:
            raise InfraError("cannot spawn empty command", code="PROVIDER_FAILED")
        cmd = [str(part) for part in argv]
        work_dir = str(self._resolve(cwd))
        log_command(cmd)
        try:
            return subprocess.Popen(cmd, cwd=work_dir, start_new_session=True)
        except OSError as exc:
            raise InfraError(
                f"failed to spawn {cmd[0]!r}: {exc}",
                code="PROVIDER_FAILED",
            ) from exc

    def read_lines(
        self,
        path: str,
        *,
        keywords: Sequence[str] | None = None,
        lines: tuple[int, int] | None = None,
    ) -> Iterator[tuple[int, str]]:
        """Stream ``(1-based lineno, line)`` pairs from ``path`` without loading the whole file."""
        target = self._resolve(path)
        kw_list = list(keywords) if keywords else None
        with contextlib.ExitStack() as stack:
            try:
                handle = stack.enter_context(open(target, encoding="utf-8", errors="replace"))
            except OSError as exc:
                raise InfraError(f"cannot read {target}: {exc}", code="PROVIDER_FAILED") from exc
            for lineno, raw in enumerate(handle, 1):
                if lines is not None:
                    start, end = lines
                    if lineno < start:
                        continue
                    if lineno > end:
                        break
                clean = raw.rstrip("\r\n")
                if kw_list and not any(kw in clean for kw in kw_list):
                    continue
                yield lineno, clean

    def line_count(self, path: str) -> int | None:
        """Return total line count of ``path``, or ``None`` if it cannot be read."""
        target = self._resolve(path)
        try:
            with open(target, "rb") as handle:
                return sum(1 for _ in handle)
        except OSError:
            return None

    def read_many(self, pattern: str) -> dict[str, str]:
        """Read all files matching ``pattern`` and return ``{path: content}``."""
        search = (
            str(self.root / pattern)
            if self.root is not None and not os.path.isabs(pattern)
            else pattern
        )
        out: dict[str, str] = {}
        for matched in sorted(glob.glob(search)):
            if not os.path.isfile(matched):
                continue
            try:
                out[matched] = Path(matched).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                raise InfraError(f"cannot read {matched}: {exc}", code="PROVIDER_FAILED") from exc
        return out

    def put(self, path: str, data: bytes) -> None:
        """Write ``data`` to ``path``, creating parent directories if missing."""
        target = self._resolve(path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            raise InfraError(f"cannot write {target}: {exc}", code="PROVIDER_FAILED") from exc

    def exists(self, path: str) -> bool:
        """Return True if ``path`` exists on disk."""
        return self._resolve(path).exists()

    def rm_tree(self, path: str) -> None:
        """Recursively remove ``path`` if it exists."""
        target = self._resolve(path)
        if not target.exists():
            return
        try:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
        except OSError as exc:
            raise InfraError(f"cannot remove {target}: {exc}", code="PROVIDER_FAILED") from exc

    def kill_pgid(self, pgid: int, sig: int = signal.SIGTERM) -> None:
        """Send signal ``sig`` to local process group ``pgid``."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pgid, sig)

    def stop_pgid(self, pgid: int) -> None:
        """Terminate local process group ``pgid`` with SIGTERM."""
        self.kill_pgid(pgid, signal.SIGTERM)
