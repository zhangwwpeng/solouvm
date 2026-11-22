"""Waveform discovery, streaming VCD parsing, and B-side export queries (AI-13)."""

from __future__ import annotations

import posixpath
import re
import shlex
from collections.abc import Iterable, Mapping, Sequence
from fnmatch import fnmatch
from typing import TYPE_CHECKING

from solouvm import ui
from solouvm.errors import ConfigError, InfraError
from solouvm.gen.shell import check_no_newlines, sh_quote
from solouvm.providers.transport_local import LocalTransport
from solouvm.providers.transport_ssh import SshTransport
from solouvm.units import parse_sim_time_ns

if TYPE_CHECKING:
    from solouvm.config.schema import Config
    from solouvm.providers.base import Provider

__all__ = ["find_waveform", "format_wave_table", "query_wave"]

_WAVE_EXTENSIONS: tuple[str, ...] = (".fsdb", ".vcd", ".wlf", ".shm")
_PREFERRED_WAVE_NAMES: tuple[str, ...] = (
    "wave.fsdb",
    "novas.fsdb",
    "wave.vcd",
    "dump.vcd",
    "wave.wlf",
    "waves.shm",
    "wave.shm",
)
_TRY_NUMERIC_RE = re.compile(r"^try(\d+)$")
_STRIP_RANGE_RE = re.compile(r"\[\d+:\d+\]$")
_TIME_HEADER_UNIT_RE = re.compile(r"^#?\s*time(?:\(([a-zA-Z]+)\))?$", re.IGNORECASE)
_NUM_ONLY_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")
_SEP_LINE_RE = re.compile(r"^[-=|+:\s]+$")


def _normalize_try_dir(try_num: str | int) -> str:
    raw = check_no_newlines(str(try_num).strip(), field="try_num")
    if not raw:
        raise ConfigError("try_num cannot be empty", code="CFG_INVALID")
    return raw if raw.startswith("try") else f"try{raw}"


def _try_sort_key(try_dir_name: str) -> tuple[int, int, str]:
    match = _TRY_NUMERIC_RE.match(try_dir_name)
    if match:
        return (0, int(match.group(1)), try_dir_name)
    return (1, 0, try_dir_name)


def _wave_file_sort_key(filename: str) -> tuple[int, int, str]:
    if filename in _PREFERRED_WAVE_NAMES:
        return (0, _PREFERRED_WAVE_NAMES.index(filename), filename)
    suffix = posixpath.splitext(filename)[1].lower()
    if suffix in _WAVE_EXTENSIONS:
        ext_rank = _WAVE_EXTENSIONS.index(suffix)
    else:
        ext_rank = len(_WAVE_EXTENSIONS)
    return (1, ext_rank, filename)


def _list_local_wave_candidates(
    transport: LocalTransport,
    job_root: str,
    target_try: str | None,
) -> list[str]:
    base = transport._resolve(job_root)
    if not base.is_dir():
        return []
    try_dirs = (
        [base / target_try]
        if target_try is not None
        else [p for p in base.iterdir() if p.is_dir() and p.name.startswith("try")]
    )
    found: list[str] = []
    for try_dir in try_dirs:
        if not try_dir.is_dir():
            continue
        for entry in try_dir.iterdir():
            if entry.suffix.lower() in _WAVE_EXTENSIONS:
                found.append(f"{job_root}/{try_dir.name}/{entry.name}")
    return found


def _list_ssh_wave_candidates(
    transport: SshTransport,
    job_root: str,
    target_try: str | None,
) -> list[str]:
    q_root = sh_quote(job_root, field="job_dir")
    if target_try is not None:
        q_try = sh_quote(f"{job_root}/{target_try}", field="try_dir")
        script = (
            f"d={q_try}; "
            '[ -d "$d" ] || exit 0; '
            'for f in "$d"/*.fsdb "$d"/*.vcd "$d"/*.wlf "$d"/*.shm; do '
            'if [ -e "$f" ]; then printf "%s\\n" "$f"; fi; '
            "done; exit 0"
        )
    else:
        script = (
            f"for d in {q_root}/try*; do "
            '[ -d "$d" ] || continue; '
            'for f in "$d"/*.fsdb "$d"/*.vcd "$d"/*.wlf "$d"/*.shm; do '
            'if [ -e "$f" ]; then printf "%s\\n" "$f"; fi; '
            "done; "
            "done; exit 0"
        )
    rc, output = transport.exec_output(["sh", "-c", script], cwd=".", check=False)
    if rc != 0:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def _probe_generic_wave_candidates(
    provider: Provider,
    job_root: str,
    target_try: str | None,
) -> list[str]:
    try_names = [target_try] if target_try is not None else [f"try{n}" for n in range(1, 6)]
    found: list[str] = []
    for try_name in try_names:
        for fname in _PREFERRED_WAVE_NAMES:
            candidate = f"{job_root}/{try_name}/{fname}"
            if provider.transport.exists(candidate):
                found.append(candidate)
    return found


def find_waveform(
    provider: Provider,
    run_id: str,
    job_id: str,
    try_num: str | None = None,
) -> str:
    """Locate the latest (or specified ``try_num``) waveform artifact for ``job_id`` on B."""
    clean_run = check_no_newlines(run_id, field="run_id")
    clean_job = check_no_newlines(job_id, field="job_id")
    target_try = _normalize_try_dir(try_num) if try_num is not None else None
    job_root = f".solouvm/runs/{clean_run}/{clean_job}"

    if isinstance(provider.transport, LocalTransport):
        candidates = _list_local_wave_candidates(provider.transport, job_root, target_try)
    elif isinstance(provider.transport, SshTransport):
        candidates = _list_ssh_wave_candidates(provider.transport, job_root, target_try)
    else:
        candidates = _probe_generic_wave_candidates(provider, job_root, target_try)

    by_try: dict[str, list[str]] = {}
    for path in candidates:
        rel_try = posixpath.basename(posixpath.dirname(path))
        if not rel_try.startswith("try"):
            continue
        if target_try is not None and rel_try != target_try:
            continue
        by_try.setdefault(rel_try, []).append(path)

    if not by_try:
        raise ConfigError(
            f"no waveform found for job {job_id!r}",
            code="CFG_INVALID",
            hint=f"rerun with waveforms: solouvm rerun --run {run_id} --wave",
        )

    latest_try = max(by_try, key=_try_sort_key)
    files = sorted(by_try[latest_try], key=lambda p: _wave_file_sort_key(posixpath.basename(p)))
    return files[0]


def _format_time_ns(time_ns: float) -> str:
    rounded = round(time_ns, 9)
    if rounded == 0.0:
        return "0ns"
    if float(rounded).is_integer():
        return f"{int(rounded)}ns"
    return f"{rounded:.9f}".rstrip("0").rstrip(".") + "ns"


def _format_vcd_vector(bits: str, width: int) -> str:
    lowered = bits.lower()
    if width == 1 and len(lowered) == 1:
        return lowered
    if set(lowered) <= {"0", "1"}:
        return f"0x{int(lowered, 2):x}"
    if width > len(lowered) and lowered:
        pad = lowered[0] if lowered[0] in ("x", "z") else "0"
        lowered = (pad * (width - len(lowered))) + lowered
    return f"b{lowered}"


def _signal_matches(full_name: str, patterns: Sequence[str]) -> bool:
    if any(fnmatch(full_name, pat) for pat in patterns):
        return True
    stripped = _STRIP_RANGE_RE.sub("", full_name)
    if stripped != full_name and any(fnmatch(stripped, pat) for pat in patterns):
        return True
    if full_name.startswith("/"):
        dotted = full_name.lstrip("/").replace("/", ".")
        return any(fnmatch(dotted, pat) for pat in patterns)
    return False


def _parse_vcd_lines(
    lines: Iterable[tuple[int, str]] | Iterable[str],
    *,
    patterns: Sequence[str],
    from_ns: float | None,
    to_ns: float | None,
    changes_only: bool,
    limit: int,
) -> tuple[list[str], list[dict[str, object]], bool]:
    timescale_ns = 1.0
    in_definitions = True
    active_cmd: str | None = None
    cmd_tokens: list[str] = []
    scope_stack: list[str] = []

    matched_signals: list[str] = []
    seen_signals: set[str] = set()
    id_to_names: dict[str, list[str]] = {}
    id_width: dict[str, int] = {}
    var_decls: list[tuple[str, int, str, str]] = []

    current_ticks: int | None = None
    current_values: dict[str, str] = {}
    prior_snap: dict[str, str] | None = None
    last_emitted: dict[str, str] | None = None
    rows: list[dict[str, object]] = []
    truncated = False
    pending_vec: tuple[str, str] | None = None
    in_comment = False

    def register_var(tokens: list[str]) -> None:
        if len(tokens) < 4:
            return
        try:
            width = int(tokens[1])
        except ValueError:
            width = 1
        var_id = tokens[2]
        raw_ref = "".join(tokens[3:])
        base_ref = _STRIP_RANGE_RE.sub("", raw_ref)
        raw_full = ".".join([*scope_stack, raw_ref]) if scope_stack else raw_ref
        base_full = ".".join([*scope_stack, base_ref]) if scope_stack else base_ref
        var_decls.append((var_id, width, raw_full, base_full))

    def finalize_vars() -> None:
        base_ids: dict[str, set[str]] = {}
        for var_id, _, _, base_full in var_decls:
            base_ids.setdefault(base_full, set()).add(var_id)
        name_owner: dict[str, str] = {}
        for var_id, width, raw_full, base_full in var_decls:
            full_name = raw_full if len(base_ids[base_full]) > 1 else base_full
            if full_name in name_owner and name_owner[full_name] != var_id:
                full_name = f"{full_name} ({var_id})"
            if not (_signal_matches(full_name, patterns) or _signal_matches(raw_full, patterns)):
                continue
            if full_name not in seen_signals:
                seen_signals.add(full_name)
                matched_signals.append(full_name)
            names = id_to_names.setdefault(var_id, [])
            if full_name not in names:
                names.append(full_name)
            id_width[var_id] = width
            name_owner[full_name] = var_id
        var_decls.clear()

    def commit_tick() -> bool:
        nonlocal prior_snap, last_emitted, truncated
        if current_ticks is None:
            return True
        time_ns = round(current_ticks * timescale_ns, 9)
        snap = {sig: current_values.get(sig, "x") for sig in matched_signals}
        if to_ns is not None and time_ns > to_ns:
            return False
        if from_ns is not None and time_ns < from_ns:
            prior_snap = snap
            return True
        changed = last_emitted is None or snap != last_emitted
        last_emitted = snap
        if changes_only and not changed:
            return True
        if limit > 0 and len(rows) >= limit:
            truncated = True
            return False
        rows.append(
            {
                "time_ns": time_ns,
                "time": _format_time_ns(time_ns),
                "values": snap,
            }
        )
        return True

    def apply_change(var_id: str, formatted_val: str) -> None:
        nonlocal current_ticks
        if current_ticks is None:
            current_ticks = 0
        names = id_to_names.get(var_id)
        if not names:
            return
        for name in names:
            current_values[name] = formatted_val

    stop = False
    for entry in lines:
        raw_line = entry[1] if isinstance(entry, tuple) else entry
        tokens = raw_line.split()
        if not tokens:
            continue
        idx = 0
        while idx < len(tokens) and not stop:
            tok = tokens[idx]
            idx += 1

            if in_definitions:
                if active_cmd is None:
                    if tok.startswith("$") and tok != "$end":
                        active_cmd = tok
                        cmd_tokens = []
                    continue

                if tok == "$end":
                    if active_cmd == "$timescale" and cmd_tokens:
                        ts_text = "".join(cmd_tokens)
                        try:
                            timescale_ns = parse_sim_time_ns(ts_text)
                        except (TypeError, ValueError) as exc:
                            raise ConfigError(
                                f"invalid VCD timescale {ts_text!r}: {exc}",
                                code="CFG_INVALID",
                            ) from None
                    elif active_cmd == "$scope" and len(cmd_tokens) >= 2:
                        scope_stack.append(cmd_tokens[1])
                    elif active_cmd == "$upscope":
                        if scope_stack:
                            scope_stack.pop()
                    elif active_cmd == "$var":
                        register_var(cmd_tokens)
                    elif active_cmd == "$enddefinitions":
                        finalize_vars()
                        in_definitions = False
                        if not matched_signals:
                            stop = True
                    active_cmd = None
                    cmd_tokens = []
                else:
                    cmd_tokens.append(tok)
                continue

            if in_comment:
                if tok == "$end":
                    in_comment = False
                continue

            if pending_vec is not None:
                kind, payload = pending_vec
                pending_vec = None
                if kind == "b":
                    width = id_width.get(tok, len(payload))
                    apply_change(tok, _format_vcd_vector(payload, width))
                else:
                    apply_change(tok, payload)
                continue

            if tok == "$comment":
                in_comment = True
                continue
            if tok in ("$dumpvars", "$dumpall", "$dumpon", "$dumpoff", "$end"):
                continue

            if tok.startswith("#") and tok[1:].isdigit():
                new_ticks = int(tok[1:])
                if current_ticks is not None and new_ticks != current_ticks and not commit_tick():
                    stop = True
                    break
                current_ticks = new_ticks
                next_ns = round(new_ticks * timescale_ns, 9)
                if to_ns is not None and next_ns > to_ns:
                    stop = True
                    break
                continue

            head = tok[0].lower()
            if head in ("b", "r", "s") and len(tok) > 1:
                pending_vec = (head, tok[1:])
                continue

            if head in ("0", "1", "x", "z") and len(tok) > 1:
                apply_change(tok[1:], head)

        if stop:
            break

    if not stop and current_ticks is not None:
        commit_tick()

    if not rows and matched_signals and from_ns is not None and prior_snap is not None:
        rows.append(
            {
                "time_ns": from_ns,
                "time": _format_time_ns(from_ns),
                "values": dict(prior_snap),
            }
        )

    return matched_signals, rows, truncated


def _exec_wave_tool(provider: Provider, argv: Sequence[str], *, tool: str) -> str:
    cmd = [check_no_newlines(str(part), field=tool) for part in argv]
    if provider.setup:
        shell_cmd = f"{provider.setup} && {' '.join(sh_quote(part, field=tool) for part in cmd)}"
        exec_argv = ["bash", "-c", shell_cmd]
    else:
        exec_argv = cmd

    exec_output = getattr(provider.transport, "exec_output", None)
    if not callable(exec_output):
        raise InfraError(
            f"transport {type(provider.transport).__name__} does not support running {tool!r}",
            code="PROVIDER_FAILED",
            hint=f"use LocalTransport or SshTransport with {tool!r} installed",
        )
    try:
        rc, output = exec_output(exec_argv, cwd=".", check=False)
    except InfraError as exc:
        hint = exc.hint or (
            f"ensure {tool!r} is available on the execution host or configure provider.setup"
        )
        raise InfraError(exc.message, code="PROVIDER_FAILED", hint=hint) from exc
    if rc != 0:
        detail = output.strip()
        msg = f"waveform tool {tool!r} failed with exit code {rc}"
        if detail:
            first_line = detail.splitlines()[0].strip()
            msg += f": {first_line}"
        hint = (
            f"ensure {tool!r} is installed on the execution host or configure provider.setup"
            if rc == 127 or "not found" in detail.lower()
            else "check waveform file and tool output on execution host"
        )
        raise InfraError(msg, code="PROVIDER_FAILED", hint=hint)
    return str(output)


def _normalize_fsdb_sig(raw_sig: str, patterns: Sequence[str]) -> str:
    if any(fnmatch(raw_sig, pat) for pat in patterns):
        return raw_sig
    if raw_sig.startswith("/"):
        dotted = raw_sig.lstrip("/").replace("/", ".")
        if any(fnmatch(dotted, pat) for pat in patterns):
            return dotted
    return raw_sig


def _parse_time_token(token: str, default_unit: str) -> float | None:
    clean = token.strip().rstrip(":")
    if not clean:
        return None
    if _NUM_ONLY_RE.match(clean):
        clean = f"{clean}{default_unit}"
    try:
        return round(parse_sim_time_ns(clean), 9)
    except (TypeError, ValueError):
        return None


def _parse_fsdbreport_output(
    output: str,
    *,
    patterns: Sequence[str],
    from_ns: float | None,
    to_ns: float | None,
    changes_only: bool,
    limit: int,
) -> tuple[list[str], list[dict[str, object]], bool]:
    headers: list[str] | None = None
    default_unit = "ns"
    matched_signals: list[str] = []
    col_map: list[str | None] = []
    rows: list[dict[str, object]] = []
    last_vals: dict[str, str] | None = None
    truncated = False

    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if not stripped or _SEP_LINE_RE.match(stripped):
            continue

        if headers is None:
            parts = (
                [p.strip() for p in stripped.split(",")]
                if "," in stripped
                else [p for p in stripped.replace("|", " ").split() if p]
            )
            if not parts:
                continue
            hdr_match = _TIME_HEADER_UNIT_RE.match(parts[0])
            if hdr_match and len(parts) >= 2:
                if hdr_match.group(1):
                    default_unit = hdr_match.group(1).lower()
                headers = parts[1:]
                for sig_name in headers:
                    norm_name = _normalize_fsdb_sig(sig_name, patterns)
                    if _signal_matches(sig_name, patterns) or _signal_matches(norm_name, patterns):
                        col_map.append(norm_name)
                        if norm_name not in matched_signals:
                            matched_signals.append(norm_name)
                    else:
                        col_map.append(None)
                continue

            # Fallback: key=value lines like "1200ns tb.clk=1 tb.data=0xa"
            kv_parts = stripped.split()
            time_ns = _parse_time_token(kv_parts[0], default_unit)
            if time_ns is not None and any("=" in p for p in kv_parts[1:]):
                row_vals: dict[str, str] = {}
                for part in kv_parts[1:]:
                    if "=" not in part:
                        continue
                    sig_raw, _, val = part.partition("=")
                    norm_name = _normalize_fsdb_sig(sig_raw, patterns)
                    if _signal_matches(sig_raw, patterns) or _signal_matches(norm_name, patterns):
                        if norm_name not in matched_signals:
                            matched_signals.append(norm_name)
                        row_vals[norm_name] = val
                if row_vals:
                    if to_ns is not None and time_ns > to_ns:
                        break
                    if from_ns is not None and time_ns < from_ns:
                        last_vals = row_vals
                        continue
                    if changes_only and last_vals is not None and row_vals == last_vals:
                        continue
                    last_vals = row_vals
                    if limit > 0 and len(rows) >= limit:
                        truncated = True
                        break
                    rows.append(
                        {
                            "time_ns": time_ns,
                            "time": _format_time_ns(time_ns),
                            "values": row_vals,
                        }
                    )
            continue

        if stripped.startswith("#"):
            continue
        cells = (
            [p.strip() for p in stripped.split(",")]
            if "," in stripped
            else [p for p in stripped.replace("|", " ").split() if p]
        )
        if len(cells) < 2:
            continue
        time_ns = _parse_time_token(cells[0], default_unit)
        if time_ns is None:
            continue
        if to_ns is not None and time_ns > to_ns:
            break

        snap: dict[str, str] = {}
        for idx, sig_name in enumerate(col_map):
            if sig_name is None:
                continue
            val = cells[idx + 1] if idx + 1 < len(cells) else "x"
            snap[sig_name] = val
        if not matched_signals:
            continue

        changed = last_vals is None or snap != last_vals
        last_vals = snap
        if from_ns is not None and time_ns < from_ns:
            continue
        if changes_only and not changed:
            continue
        if limit > 0 and len(rows) >= limit:
            truncated = True
            break
        rows.append(
            {
                "time_ns": time_ns,
                "time": _format_time_ns(time_ns),
                "values": snap,
            }
        )

    return matched_signals, rows, truncated


def _remove_remote_file(provider: Provider, path: str) -> None:
    if not provider.transport.exists(path):
        return
    if isinstance(provider.transport, (LocalTransport, SshTransport)):
        provider.transport.rm_tree(path)
    else:
        exec_output = getattr(provider.transport, "exec_output", None)
        if callable(exec_output):
            exec_output(["rm", "-f", path], cwd=".", check=False)


def _query_converted_vcd(
    *,
    provider: Provider,
    wave_path: str,
    tool: str,
    build_argv: Sequence[str],
    tmp_vcd: str,
    patterns: Sequence[str],
    from_ns: float | None,
    to_ns: float | None,
    changes_only: bool,
    limit: int,
) -> tuple[list[str], list[dict[str, object]], bool]:
    try:
        output = _exec_wave_tool(provider, build_argv, tool=tool)
        if provider.transport.exists(tmp_vcd):
            return _parse_vcd_lines(
                provider.transport.read_lines(tmp_vcd),
                patterns=patterns,
                from_ns=from_ns,
                to_ns=to_ns,
                changes_only=changes_only,
                limit=limit,
            )
        if "$var" in output or "$enddefinitions" in output:
            return _parse_vcd_lines(
                output.splitlines(),
                patterns=patterns,
                from_ns=from_ns,
                to_ns=to_ns,
                changes_only=changes_only,
                limit=limit,
            )
        raise InfraError(
            f"waveform tool {tool!r} did not produce VCD output for {wave_path!r}",
            code="PROVIDER_FAILED",
            hint=f"check {tool!r} output and permissions on execution host",
        )
    finally:
        _remove_remote_file(provider, tmp_vcd)


def _expand_wave_export_cmd(
    template: object,
    *,
    wave_path: str,
    tmp_vcd: str,
    signals: Sequence[str],
    from_ns: float | None,
    to_ns: float | None,
) -> list[str]:
    if isinstance(template, str):
        try:
            raw_tokens = shlex.split(template)
        except ValueError as exc:
            raise ConfigError(
                f"sim.wave_export: invalid command syntax {template!r}: {exc}",
                code="CFG_INVALID",
            ) from None
    elif isinstance(template, (list, tuple)) and all(isinstance(t, str) for t in template):
        raw_tokens = list(template)
    else:
        raise ConfigError(
            f"sim.wave_export must be a string or list of strings, got {template!r}",
            code="CFG_INVALID",
        )
    if not raw_tokens:
        raise ConfigError("sim.wave_export cannot be empty", code="CFG_INVALID")
    values = {
        "wave": wave_path,
        "wave_file": wave_path,
        "out": tmp_vcd,
        "vcd": tmp_vcd,
        "signals": ",".join(signals),
        "from": _format_time_ns(from_ns) if from_ns is not None else "",
        "to": _format_time_ns(to_ns) if to_ns is not None else "",
    }
    argv: list[str] = []
    for tok in raw_tokens:
        expanded = tok
        for k, v in values.items():
            expanded = expanded.replace(f"{{{k}}}", v)
        argv.append(check_no_newlines(expanded, field="sim.wave_export"))
    return argv


def query_wave(
    *,
    config: Config,
    run_id: str,
    job_id: str,
    provider: Provider,
    signals: list[str],
    time_from: str | None = None,
    time_to: str | None = None,
    changes_only: bool = True,
    limit: int = 200,
    try_num: str | None = None,
) -> dict[str, object]:
    """Query signal values from a job's waveform on B within an optional time window (AI-13)."""
    clean_signals = [s.strip() for s in signals if isinstance(s, str) and s.strip()]
    if not clean_signals:
        raise ConfigError(
            "at least one signal pattern is required",
            code="CFG_INVALID",
            hint="pass --signals sig1,sig2",
        )
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ConfigError(f"limit must be an int >= 0, got {limit!r}", code="CFG_INVALID")

    from_ns: float | None = None
    if time_from is not None:
        try:
            from_ns = parse_sim_time_ns(time_from)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"invalid --from time {time_from!r}: {exc}",
                code="CFG_INVALID",
            ) from None

    to_ns: float | None = None
    if time_to is not None:
        try:
            to_ns = parse_sim_time_ns(time_to)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"invalid --to time {time_to!r}: {exc}",
                code="CFG_INVALID",
            ) from None

    if from_ns is not None and to_ns is not None and from_ns > to_ns:
        raise ConfigError(
            f"time_from ({time_from!r}) must be <= time_to ({time_to!r})",
            code="CFG_INVALID",
        )

    wave_path = find_waveform(provider, run_id, job_id, try_num=try_num)
    ext = posixpath.splitext(wave_path)[1].lower()
    custom_export = config.sim.get("wave_export") if hasattr(config, "sim") else None

    if custom_export is not None and ext != ".vcd":
        tmp_vcd = f"{wave_path}.solouvm_tmp.vcd"
        export_argv = _expand_wave_export_cmd(
            custom_export,
            wave_path=wave_path,
            tmp_vcd=tmp_vcd,
            signals=clean_signals,
            from_ns=from_ns,
            to_ns=to_ns,
        )
        matched_signals, rows, truncated = _query_converted_vcd(
            provider=provider,
            wave_path=wave_path,
            tool=posixpath.basename(export_argv[0]) or export_argv[0],
            build_argv=export_argv,
            tmp_vcd=tmp_vcd,
            patterns=clean_signals,
            from_ns=from_ns,
            to_ns=to_ns,
            changes_only=changes_only,
            limit=limit,
        )
    elif ext == ".vcd":
        matched_signals, rows, truncated = _parse_vcd_lines(
            provider.transport.read_lines(wave_path),
            patterns=clean_signals,
            from_ns=from_ns,
            to_ns=to_ns,
            changes_only=changes_only,
            limit=limit,
        )
    elif ext == ".fsdb":
        argv = ["fsdbreport", wave_path, "-s", ",".join(clean_signals)]
        if from_ns is not None:
            argv.extend(["-bt", _format_time_ns(from_ns)])
        if to_ns is not None:
            argv.extend(["-et", _format_time_ns(to_ns)])
        output = _exec_wave_tool(provider, argv, tool="fsdbreport")
        matched_signals, rows, truncated = _parse_fsdbreport_output(
            output,
            patterns=clean_signals,
            from_ns=from_ns,
            to_ns=to_ns,
            changes_only=changes_only,
            limit=limit,
        )
    elif ext == ".wlf":
        tmp_vcd = f"{wave_path}.solouvm_tmp.vcd"
        matched_signals, rows, truncated = _query_converted_vcd(
            provider=provider,
            wave_path=wave_path,
            tool="wlf2vcd",
            build_argv=["wlf2vcd", wave_path, "-o", tmp_vcd],
            tmp_vcd=tmp_vcd,
            patterns=clean_signals,
            from_ns=from_ns,
            to_ns=to_ns,
            changes_only=changes_only,
            limit=limit,
        )
    elif ext == ".shm":
        tmp_vcd = f"{wave_path}.solouvm_tmp.vcd"
        matched_signals, rows, truncated = _query_converted_vcd(
            provider=provider,
            wave_path=wave_path,
            tool="simvisdbutil",
            build_argv=["simvisdbutil", wave_path, "-vcd", "-output", tmp_vcd],
            tmp_vcd=tmp_vcd,
            patterns=clean_signals,
            from_ns=from_ns,
            to_ns=to_ns,
            changes_only=changes_only,
            limit=limit,
        )
    else:
        raise ConfigError(
            f"unsupported waveform format: {wave_path!r}",
            code="CFG_INVALID",
            hint="supported formats: .vcd, .fsdb, .wlf, .shm",
        )

    return {
        "run_id": run_id,
        "job_id": job_id,
        "wave_file": wave_path,
        "signals": matched_signals,
        "rows": rows,
        "truncated": truncated,
    }


def format_wave_table(data: Mapping[str, object]) -> str:
    """Format a ``query_wave`` result dict as an aligned plain-text ASCII table."""
    raw_signals = data.get("signals")
    signals = [str(s) for s in raw_signals] if isinstance(raw_signals, Sequence) else []
    raw_rows = data.get("rows")
    rows = [r for r in raw_rows if isinstance(r, Mapping)] if isinstance(raw_rows, Sequence) else []

    if not signals:
        wave_file = data.get("wave_file")
        return f"no matching signals in {wave_file}" if wave_file else "no matching signals"

    headers = ["TIME", *signals]
    table_rows: list[list[str]] = []
    for row in rows:
        values = row.get("values")
        val_map = values if isinstance(values, Mapping) else {}
        table_rows.append([str(row.get("time", "")), *(str(val_map.get(s, "-")) for s in signals)])

    rendered = ui.format_table(headers, table_rows)
    if not rows:
        return f"{rendered}\n(no samples in window)"

    if data.get("truncated"):
        job_id = data.get("job_id")
        more_cmd = f"solouvm wave {job_id} --limit 0" if job_id else "--limit 0"
        rendered += f"\n... truncated at {len(rows)} rows (pass {more_cmd} to show all)"
    return rendered
