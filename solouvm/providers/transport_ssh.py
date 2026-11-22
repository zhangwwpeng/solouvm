"""SSH transport running commands and streaming files via the system ``ssh`` binary."""

from __future__ import annotations

import contextlib
import io
import math
import os
import posixpath
import shlex
import subprocess
import threading
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from solouvm.errors import ConfigError, InfraError
from solouvm.gen.shell import check_no_newlines, sh_quote
from solouvm.providers.base import Process, log_command

__all__ = ["SshTransport"]

_SAFE_GLOB_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789/_.-*?[]"
)
_DEFAULT_SSH_OPTS: tuple[str, ...] = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=10",
    "-o",
    "ServerAliveInterval=15",
    "-o",
    "ServerAliveCountMax=3",
)
_DEFAULT_OP_TIMEOUT = 60.0


class _UnsetType:
    pass


_UNSET = _UnsetType()


def _validate_timeout(value: float | None, *, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(
            f"{field} must be a finite number > 0 or None, got {value!r}",
            code="CFG_INVALID",
        )
    num = float(value)
    if not math.isfinite(num) or num <= 0:
        raise ConfigError(
            f"{field} must be a finite number > 0 or None, got {value!r}",
            code="CFG_INVALID",
        )
    return num


def _glob_quote(pattern: str) -> str:
    """Escape POSIX shell metacharacters while keeping glob wildcards active."""
    check_no_newlines(pattern, field="pattern")
    out: list[str] = []
    for ch in pattern:
        if ch in _SAFE_GLOB_CHARS:
            out.append(ch)
        else:
            out.append(f"\\{ch}")
    return "".join(out)


class SshTransport:
    """Transport delegating execution and file I/O to host B over system ``ssh``."""

    kind: str = "ssh"

    def __init__(
        self,
        host: str,
        workdir: str,
        ssh_opts: Sequence[str] | str = (),
        root: str | Path | None = None,
        op_timeout: float | None = _DEFAULT_OP_TIMEOUT,
    ) -> None:
        if not isinstance(host, str) or not host.strip():
            raise ConfigError("ssh transport requires non-empty 'host'", code="CFG_INVALID")
        if not isinstance(workdir, str) or not workdir.strip():
            raise ConfigError("ssh transport requires non-empty 'workdir'", code="CFG_INVALID")
        self.host: str = check_no_newlines(host.strip(), field="ssh.host")
        self.workdir: str = posixpath.normpath(
            check_no_newlines(workdir.strip(), field="ssh.workdir")
        )
        raw_opts = shlex.split(ssh_opts) if isinstance(ssh_opts, str) else list(ssh_opts)
        self.ssh_opts: tuple[str, ...] = tuple(
            check_no_newlines(str(opt), field="ssh_opts") for opt in raw_opts
        )
        self.root: Path | None = Path(root) if root is not None else None
        self.op_timeout: float | None = _validate_timeout(op_timeout, field="ssh.op_timeout")

    def _resolve_timeout(self, timeout: float | _UnsetType | None) -> float | None:
        raw = self.op_timeout if isinstance(timeout, _UnsetType) else timeout
        return _validate_timeout(raw, field="timeout")

    def _run(
        self,
        full_argv: list[str],
        *,
        op: str,
        timeout: float | None,
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[Any]:
        log_command(full_argv)
        if "input" not in kwargs and "stdin" not in kwargs:
            kwargs["stdin"] = subprocess.DEVNULL
        try:
            return subprocess.run(full_argv, timeout=timeout, check=False, **kwargs)
        except subprocess.TimeoutExpired as exc:
            raise InfraError(
                f"ssh {op} timed out after {timeout:g}s on {self.host!r}",
                code="PROVIDER_FAILED",
                hint="check network connectivity or ssh_opts",
            ) from exc
        except OSError as exc:
            raise InfraError(
                f"failed to run ssh: {exc}",
                code="PROVIDER_FAILED",
                hint="check network connectivity or ssh_opts",
            ) from exc

    def _ssh_base(self, *, compress: bool = False) -> list[str]:
        cmd = ["ssh"]
        if compress and "-C" not in self.ssh_opts:
            cmd.append("-C")
        # User ssh_opts must precede defaults because OpenSSH applies first-match-wins.
        cmd.extend(self.ssh_opts)
        cmd.extend(_DEFAULT_SSH_OPTS)
        cmd.append(self.host)
        return cmd

    def _remote_path(self, path: str | Path) -> str:
        raw = check_no_newlines(os.fspath(path), field="path")
        if self.root is not None:
            local_p = Path(raw)
            if local_p.is_absolute():
                for candidate_p, candidate_root in (
                    (local_p, self.root),
                    (local_p.resolve(), self.root.resolve()),
                ):
                    if candidate_p.is_relative_to(candidate_root):
                        rel = candidate_p.relative_to(candidate_root).as_posix()
                        if rel == ".":
                            return self.workdir
                        return posixpath.normpath(posixpath.join(self.workdir, rel))
        posix_path = raw.replace(os.sep, "/") if os.sep != "/" else raw
        if posixpath.isabs(posix_path):
            return posixpath.normpath(posix_path)
        if posix_path in ("", "."):
            return self.workdir
        return posixpath.normpath(posixpath.join(self.workdir, posix_path))

    def _remote_cd_cmd(self, argv: Sequence[str], *, cwd: str) -> str:
        if not argv:
            raise InfraError("cannot execute empty command", code="PROVIDER_FAILED")
        remote_cwd = self._remote_path(cwd)
        cmd_str = " ".join(sh_quote(str(part), field="ssh command") for part in argv)
        return f"cd {sh_quote(remote_cwd, field='cwd')} && {cmd_str}"

    def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        check: bool = True,
        timeout: float | None = None,
    ) -> int:
        """Execute ``argv`` inside remote ``cwd`` via ``ssh``."""
        eff_timeout = _validate_timeout(timeout, field="timeout")
        full_argv = [*self._ssh_base(), self._remote_cd_cmd(argv, cwd=cwd)]
        proc = self._run(full_argv, op="exec", timeout=eff_timeout)
        if check and proc.returncode != 0:
            raise InfraError(
                f"remote command {shlex.join([str(a) for a in argv])!r} on {self.host!r} "
                f"failed with exit code {proc.returncode}",
                code="PROVIDER_FAILED",
            )
        return proc.returncode

    def exec_output(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        check: bool = True,
        timeout: float | None = None,
    ) -> tuple[int, str]:
        """Execute ``argv`` over ``ssh`` capturing combined stdout/stderr text."""
        eff_timeout = _validate_timeout(timeout, field="timeout")
        full_argv = [*self._ssh_base(), self._remote_cd_cmd(argv, cwd=cwd)]
        proc = self._run(
            full_argv,
            op="exec_output",
            timeout=eff_timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and proc.returncode != 0:
            raise InfraError(
                f"remote command {shlex.join([str(a) for a in argv])!r} on {self.host!r} "
                f"failed with exit code {proc.returncode}",
                code="PROVIDER_FAILED",
            )
        return proc.returncode, proc.stdout

    def exec_capture(
        self,
        argv: Sequence[str],
        *,
        cwd: str = ".",
        check: bool = True,
        timeout: float | None = None,
    ) -> tuple[int, str, str]:
        """Execute ``argv`` over ``ssh`` capturing separate stdout and stderr text."""
        eff_timeout = _validate_timeout(timeout, field="timeout")
        full_argv = [*self._ssh_base(), self._remote_cd_cmd(argv, cwd=cwd)]
        proc = self._run(
            full_argv,
            op="exec_capture",
            timeout=eff_timeout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and proc.returncode != 0:
            detail = proc.stderr.strip() or proc.stdout.strip()
            msg = (
                f"remote command {shlex.join([str(a) for a in argv])!r} on {self.host!r} "
                f"failed with exit code {proc.returncode}"
            )
            if detail:
                msg += f": {detail}"
            raise InfraError(msg, code="PROVIDER_FAILED")
        return proc.returncode, proc.stdout, proc.stderr

    def spawn(self, argv: Sequence[str], *, cwd: str = ".") -> Process:
        """Spawn an ``ssh`` session running ``argv`` in remote ``cwd``."""
        full_argv = [*self._ssh_base(), self._remote_cd_cmd(argv, cwd=cwd)]
        log_command(full_argv)
        try:
            return subprocess.Popen(
                full_argv,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise InfraError(f"failed to spawn ssh: {exc}", code="PROVIDER_FAILED") from exc

    def read_lines(
        self,
        path: str,
        *,
        keywords: Sequence[str] | None = None,
        lines: tuple[int, int] | None = None,
        timeout: float | _UnsetType | None = _UNSET,
    ) -> Iterator[tuple[int, str]]:
        """Stream ``(1-based lineno, line)`` from remote ``path`` using compressed ssh."""
        eff_timeout = self._resolve_timeout(timeout)
        remote_path = self._remote_path(path)
        q_path = sh_quote(remote_path, field="path")
        kw_list = list(keywords) if keywords else []
        if kw_list:
            patterns = " ".join(f"-e {sh_quote(kw, field='keyword')}" for kw in kw_list)
            remote_cmd = f"grep -n -F {patterns} -- {q_path}"
            allowed_rc = (0, 1)
        elif lines is not None:
            start, end = int(lines[0]), int(lines[1])
            if end < start or end < 1:
                return
            start = max(1, start)
            awk_prog = sh_quote(
                f'NR>={start} && NR<={end} {{print NR ":" $0}} NR>{end} {{exit}}',
                field="awk",
            )
            remote_cmd = f"awk {awk_prog} {q_path}"
            allowed_rc = (0,)
        else:
            awk_prog = sh_quote('{print NR ":" $0}', field="awk")
            remote_cmd = f"awk {awk_prog} {q_path}"
            allowed_rc = (0,)

        full_argv = [*self._ssh_base(compress=True), remote_cmd]
        log_command(full_argv)
        try:
            proc = subprocess.Popen(
                full_argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise InfraError(f"failed to run ssh: {exc}", code="PROVIDER_FAILED") from exc

        assert proc.stdout is not None
        assert proc.stderr is not None
        timed_out = threading.Event()
        timer: threading.Timer | None = None

        def _arm_watchdog() -> None:
            nonlocal timer
            if timer is not None:
                timer.cancel()
            if eff_timeout is not None:
                def _on_timeout() -> None:
                    timed_out.set()
                    with contextlib.suppress(OSError):
                        proc.kill()

                timer = threading.Timer(eff_timeout, _on_timeout)
                timer.daemon = True
                timer.start()

        try:
            _arm_watchdog()
            stream = io.TextIOWrapper(proc.stdout, encoding="utf-8", errors="replace")
            for raw in stream:
                _arm_watchdog()
                clean = raw.rstrip("\r\n")
                lineno_str, sep, text = clean.partition(":")
                if not sep or not lineno_str.isdigit():
                    continue
                lineno = int(lineno_str)
                if lines is not None and kw_list and not (lines[0] <= lineno <= lines[1]):
                    continue
                yield lineno, text
            err_bytes = proc.stderr.read()
            rc = proc.wait()
        finally:
            if timer is not None:
                timer.cancel()
            if proc.poll() is None:
                with contextlib.suppress(OSError):
                    proc.terminate()
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
            with contextlib.suppress(OSError):
                proc.stdout.close()
            with contextlib.suppress(OSError):
                proc.stderr.close()

        if timed_out.is_set():
            raise InfraError(
                f"ssh read_lines timed out after {eff_timeout:g}s for "
                f"{remote_path!r} on {self.host!r}",
                code="PROVIDER_FAILED",
                hint="check network connectivity or ssh_opts",
            )
        if rc not in allowed_rc:
            err_text = err_bytes.decode("utf-8", errors="replace").strip()
            msg = f"ssh read_lines failed for {remote_path!r} on {self.host!r} (exit code {rc})"
            if err_text:
                msg += f": {err_text}"
            raise InfraError(msg, code="PROVIDER_FAILED")

    def line_count(
        self,
        path: str,
        *,
        timeout: float | _UnsetType | None = _UNSET,
    ) -> int | None:
        """Return total line count of remote ``path``, or ``None`` if absent/unreadable."""
        eff_timeout = self._resolve_timeout(timeout)
        remote_path = self._remote_path(path)
        remote_cmd = f"wc -l < {sh_quote(remote_path, field='path')}"
        full_argv = [*self._ssh_base(), remote_cmd]
        proc = self._run(
            full_argv,
            op="line_count",
            timeout=eff_timeout,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode == 255:
            raise InfraError(
                f"ssh line_count failed for {remote_path!r} on {self.host!r}",
                code="PROVIDER_FAILED",
            )
        if proc.returncode != 0:
            return None
        try:
            return int(proc.stdout.strip())
        except ValueError as exc:
            raise InfraError(
                f"unexpected wc -l output for {remote_path!r}: {proc.stdout!r}",
                code="PROVIDER_FAILED",
            ) from exc

    def read_many(
        self,
        pattern: str,
        *,
        timeout: float | _UnsetType | None = _UNSET,
    ) -> dict[str, str]:
        """Fetch all remote files matching ``pattern`` in a single ``ssh`` roundtrip."""
        eff_timeout = self._resolve_timeout(timeout)
        remote_pattern = _glob_quote(self._remote_path(pattern))
        remote_cmd = (
            f"for f in {remote_pattern}; do "
            '[ -f "$f" ] || continue; '
            'sz=$(wc -c < "$f" | tr -d "[:space:]"); '
            'printf \'%s\\000%s\\000\' "$sz" "$f"; '
            'if [ "$sz" -gt 0 ]; then head -c "$sz" < "$f"; fi; '
            "done"
        )
        full_argv = [*self._ssh_base(), remote_cmd]
        proc = self._run(
            full_argv,
            op="read_many",
            timeout=eff_timeout,
            capture_output=True,
        )
        if proc.returncode != 0:
            err_text = proc.stderr.decode("utf-8", errors="replace").strip()
            msg = f"ssh read_many failed on {self.host!r} (exit code {proc.returncode})"
            if err_text:
                msg += f": {err_text}"
            raise InfraError(msg, code="PROVIDER_FAILED")

        data = proc.stdout
        pos = 0
        total_len = len(data)
        result: dict[str, str] = {}
        while pos < total_len:
            nul1 = data.find(b"\x00", pos)
            if nul1 == -1:
                raise InfraError(
                    f"malformed ssh read_many header {data[pos : pos + 64]!r}",
                    code="PROVIDER_FAILED",
                )
            size_bytes = data[pos:nul1]
            try:
                size = int(size_bytes.strip())
            except ValueError as exc:
                raise InfraError(
                    f"malformed ssh read_many header {size_bytes!r}",
                    code="PROVIDER_FAILED",
                ) from exc
            if size < 0:
                raise InfraError(
                    f"malformed ssh read_many header {size_bytes!r}",
                    code="PROVIDER_FAILED",
                )
            pos = nul1 + 1
            nul2 = data.find(b"\x00", pos)
            if nul2 == -1:
                raise InfraError("truncated ssh read_many stream", code="PROVIDER_FAILED")
            file_path = data[pos:nul2].decode("utf-8", errors="replace")
            pos = nul2 + 1
            if pos + size > total_len:
                raise InfraError(
                    f"truncated ssh read_many payload for {file_path!r}",
                    code="PROVIDER_FAILED",
                )
            payload = data[pos : pos + size]
            pos += size
            result[file_path] = payload.decode("utf-8", errors="replace")
        return result

    def put(
        self,
        path: str,
        data: bytes,
        *,
        timeout: float | _UnsetType | None = _UNSET,
    ) -> None:
        """Write ``data`` to remote ``path`` via ``ssh`` stdin pipe."""
        eff_timeout = self._resolve_timeout(timeout)
        remote_path = self._remote_path(path)
        remote_dir = posixpath.dirname(remote_path) or "."
        remote_cmd = (
            f"mkdir -p {sh_quote(remote_dir, field='dir')} "
            f"&& cat > {sh_quote(remote_path, field='path')}"
        )
        full_argv = [*self._ssh_base(), remote_cmd]
        proc = self._run(
            full_argv,
            op="put",
            timeout=eff_timeout,
            input=data,
            capture_output=True,
        )
        if proc.returncode != 0:
            err_text = proc.stderr.decode("utf-8", errors="replace").strip()
            msg = (
                f"ssh put failed for {remote_path!r} on {self.host!r} (exit code {proc.returncode})"
            )
            if err_text:
                msg += f": {err_text}"
            raise InfraError(msg, code="PROVIDER_FAILED")

    def exists(
        self,
        path: str,
        *,
        timeout: float | _UnsetType | None = _UNSET,
    ) -> bool:
        """Return True if remote ``path`` exists, False if absent; raise on ssh error."""
        eff_timeout = self._resolve_timeout(timeout)
        remote_path = self._remote_path(path)
        remote_cmd = f"test -e {sh_quote(remote_path, field='path')}"
        full_argv = [*self._ssh_base(), remote_cmd]
        proc = self._run(
            full_argv,
            op="exists",
            timeout=eff_timeout,
            capture_output=True,
        )
        if proc.returncode == 0:
            return True
        if proc.returncode == 1:
            return False
        err_text = proc.stderr.decode("utf-8", errors="replace").strip()
        msg = (
            f"ssh exists failed for {remote_path!r} on {self.host!r} (exit code {proc.returncode})"
        )
        if err_text:
            msg += f": {err_text}"
        raise InfraError(msg, code="PROVIDER_FAILED")

    def rm_tree(
        self,
        path: str,
        *,
        timeout: float | _UnsetType | None = _UNSET,
    ) -> None:
        """Recursively remove remote ``path``."""
        eff_timeout = self._resolve_timeout(timeout)
        remote_path = self._remote_path(path)
        remote_cmd = f"rm -rf {sh_quote(remote_path, field='path')}"
        full_argv = [*self._ssh_base(), remote_cmd]
        proc = self._run(
            full_argv,
            op="rm_tree",
            timeout=eff_timeout,
            capture_output=True,
        )
        if proc.returncode != 0:
            err_text = proc.stderr.decode("utf-8", errors="replace").strip()
            msg = f"ssh rm_tree failed for {remote_path!r} (exit code {proc.returncode})"
            if err_text:
                msg += f": {err_text}"
            raise InfraError(msg, code="PROVIDER_FAILED")

    def kill_pgid(
        self,
        pgid: int,
        sig: str = "TERM",
        *,
        timeout: float | _UnsetType | None = _UNSET,
    ) -> None:
        """Send signal ``sig`` to remote process group ``pgid`` over ``ssh``."""
        eff_timeout = self._resolve_timeout(timeout)
        sig_name = sig.removeprefix("SIG")
        remote_cmd = f"kill -{sh_quote(sig_name, field='signal')} -{int(pgid)} 2>/dev/null || true"
        full_argv = [*self._ssh_base(), remote_cmd]
        proc = self._run(
            full_argv,
            op="kill_pgid",
            timeout=eff_timeout,
            capture_output=True,
        )
        if proc.returncode == 255:
            err_text = proc.stderr.decode("utf-8", errors="replace").strip()
            msg = f"ssh kill_pgid failed on {self.host!r} (exit code 255)"
            if err_text:
                msg += f": {err_text}"
            raise InfraError(msg, code="PROVIDER_FAILED")

    def stop_pgid(self, pgid: int) -> None:
        """Terminate remote process group ``pgid`` with SIGTERM."""
        self.kill_pgid(pgid, sig="TERM")
