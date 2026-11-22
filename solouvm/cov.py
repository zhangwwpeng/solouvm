"""Coverage database merging and summary reporting on execution host B (V-01..V-04)."""

from __future__ import annotations

import json
import posixpath
import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from solouvm.backends import resolve_sim_backend
from solouvm.errors import ConfigError, InfraError
from solouvm.gen.shell import check_no_newlines, sh_quote

if TYPE_CHECKING:
    from solouvm.backends.base import Backend
    from solouvm.config.schema import Config
    from solouvm.model import Plan
    from solouvm.providers.base import Provider

__all__ = ["cov_merge", "cov_report"]

_SUPPORTED_RESULTS_SCHEMA = 1
_PASSED_STATUSES = frozenset({"pass", "xfail"})

_ALL_COV_DBS: tuple[str, ...] = (
    "cov.vdb",
    "cov_work",
    "cov.ucdb",
    "coverage.dat",
    "xezim_cov.json",
)
_BACKEND_COV_DB: dict[str, str] = {
    "vcs": "cov.vdb",
    "xrun": "cov_work",
    "questa": "cov.ucdb",
    "verilator": "coverage.dat",
    "xezim": "xezim_cov.json",
}
_MERGED_DB_NAME: dict[str, str] = {
    "vcs": "merged.vdb",
    "xrun": "merged",
    "questa": "merged.ucdb",
    "verilator": "merged.dat",
    "xezim": "merged_cov.json",
}
_BUILTIN_COV_TOOLS: dict[str, str] = {
    "vcs": "urg",
    "xrun": "imc",
    "questa": "vcover",
    "verilator": "verilator_coverage",
    "xezim": "xezim-json",
}

_HEADER_ALIAS: dict[str, str] = {
    "score": "total",
    "total": "total",
    "overall": "total",
    "line": "line",
    "stmt": "line",
    "statement": "line",
    "statements": "line",
    "block": "line",
    "cond": "cond",
    "condition": "cond",
    "conditions": "cond",
    "expr": "cond",
    "expression": "cond",
    "expressions": "cond",
    "fsm": "fsm",
    "toggle": "toggle",
    "toggles": "toggle",
    "tgl": "toggle",
    "branch": "branch",
    "branches": "branch",
}

_METRIC_LINE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "total",
        re.compile(
            r"(?i)\b(?:total\s+coverage(?:\s+by\s+instance)?|overall(?:\s+average)?|total\s+score|"
            r"score|total(?!\s+covergroup))\b[^%\n]*?(\d+(?:\.\d+)?)\s*%"
        ),
    ),
    (
        "line",
        re.compile(
            r"(?i)\b(?:line|stmt|statements?|block)(?:\s+coverage)?\b[^%\n]*?(\d+(?:\.\d+)?)\s*%"
        ),
    ),
    (
        "cond",
        re.compile(
            r"(?i)\b(?:cond(?:itions?)?|expr(?:essions?)?)(?:\s+coverage)?\b"
            r"[^%\n]*?(\d+(?:\.\d+)?)\s*%"
        ),
    ),
    (
        "fsm",
        re.compile(
            r"(?i)\bfsm(?:\s+(?:states?|transitions?|coverage))?\b[^%\n]*?(\d+(?:\.\d+)?)\s*%"
        ),
    ),
    (
        "toggle",
        re.compile(r"(?i)\b(?:toggles?|tgl)(?:\s+coverage)?\b[^%\n]*?(\d+(?:\.\d+)?)\s*%"),
    ),
    (
        "branch",
        re.compile(r"(?i)\bbranch(?:es)?(?:\s+coverage)?\b[^%\n]*?(\d+(?:\.\d+)?)\s*%"),
    ),
)

_COLON_NUM_RE = re.compile(
    r"(?i)^\s*(total(?:\s+coverage)?|score|overall|line|stmt|statements?|cond(?:itions?)?|"
    r"fsm|toggles?|tgl|branch(?:es)?)\s*[:=]\s*(\d+(?:\.\d+)?)\s*%?\s*$"
)
_TABLE_VAL_RE = re.compile(r"^(?:(\d+(?:\.\d+)?)%?|--|n/?a)$", re.IGNORECASE)


def _resolve_backend(config: Config) -> Backend:
    return resolve_sim_backend(config.simulator, config.sim)


def _tcl_quote(value: str) -> str:
    check_no_newlines(value, field="tcl path")
    if any(ch in value for ch in (" ", "\t", "$", "[", "]", ";", "\x22")):
        escaped = value.replace("{", r"\{").replace("}", r"\}")
        return "{" + escaped + "}"
    return value


def _load_results(
    config: Config, run_id: str, results_data: dict[str, object] | None
) -> dict[str, object]:
    if results_data is not None:
        if not isinstance(results_data, dict):
            raise ConfigError("results_data must be a dict", code="CFG_INVALID")
        schema = results_data.get("schema")
        if schema is not None and (type(schema) is not int or schema != _SUPPORTED_RESULTS_SCHEMA):
            raise ConfigError(
                f"unsupported results schema {schema!r} (expected {_SUPPORTED_RESULTS_SCHEMA})",
                code="CFG_INVALID",
            )
        return results_data

    results_path = Path(config.root) / ".solouvm" / "runs" / run_id / "results.json"
    if not results_path.is_file():
        raise ConfigError(
            f"results not found for run {run_id!r}: {results_path}",
            code="RUN_NOT_FOUND",
            hint=f"run 'solouvm judge --run {run_id}' first",
        )
    try:
        raw_text = results_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(
            f"cannot read results file {results_path}: {exc}",
            code="CFG_LOAD_FAILED",
            file=results_path,
        ) from exc
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"invalid JSON in results file {results_path}: {exc}",
            code="CFG_LOAD_FAILED",
            file=results_path,
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigError(
            "results file must contain a JSON object",
            code="CFG_INVALID",
            file=results_path,
        )
    schema = parsed.get("schema")
    if schema is not None and (type(schema) is not int or schema != _SUPPORTED_RESULTS_SCHEMA):
        raise ConfigError(
            f"unsupported results schema {schema!r} (expected {_SUPPORTED_RESULTS_SCHEMA})",
            code="CFG_INVALID",
            file=results_path,
        )
    return parsed


def _parse_numeric_try(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _last_numeric_try_from_entry(entry: Mapping[str, object]) -> int | None:
    attempts = entry.get("attempts")
    if not isinstance(attempts, list):
        return None
    non_wave: list[int] = []
    all_tries: list[int] = []
    for item in attempts:
        if not isinstance(item, Mapping):
            continue
        num = _parse_numeric_try(item.get("try"))
        if num is None:
            continue
        all_tries.append(num)
        if not item.get("wave", False):
            non_wave.append(num)
    if non_wave:
        return max(non_wave)
    if all_tries:
        return max(all_tries)
    return None


def _candidate_db_names(backend_name: str) -> tuple[str, ...]:
    primary = _BACKEND_COV_DB.get(backend_name)
    if primary is None:
        return _ALL_COV_DBS
    return (primary, *(name for name in _ALL_COV_DBS if name != primary))


def _find_db_in_try(
    provider: Provider,
    try_dir: str,
    candidates: Sequence[str],
) -> str | None:
    for db_name in candidates:
        candidate = f"{try_dir}/{db_name}"
        if provider.transport.exists(candidate):
            return candidate
    return None


def _probe_latest_try_db(
    provider: Provider,
    job_dir: str,
    candidates: Sequence[str],
) -> str | None:
    found: str | None = None
    n = 1
    while True:
        try_dir = f"{job_dir}/try{n}"
        db = _find_db_in_try(provider, try_dir, candidates)
        if db is not None:
            found = db
            n += 1
            continue
        if provider.transport.exists(try_dir):
            n += 1
            continue
        break
    return found


def _collect_passed_dbs(
    *,
    config: Config,
    plan: Plan,
    provider: Provider,
    backend_name: str,
    results: Mapping[str, object],
) -> list[str]:
    raw_jobs = results.get("jobs", [])
    if not isinstance(raw_jobs, list):
        raise ConfigError("results field 'jobs' must be a list", code="CFG_INVALID")

    plan_jobs = {job.job_id: job for job in plan.jobs}
    candidates = _candidate_db_names(backend_name)
    db_paths: list[str] = []

    for item in raw_jobs:
        if not isinstance(item, Mapping):
            continue
        status = item.get("status")
        if status not in _PASSED_STATUSES:
            continue
        job_id = item.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            continue
        job = plan_jobs.get(job_id)
        job_dir = job.dir if job is not None else f".solouvm/runs/{plan.run_id}/{job_id}"
        last_try = _parse_numeric_try(item.get("chosen_try"))
        if last_try is None:
            last_try = _last_numeric_try_from_entry(item)
        if last_try is not None:
            db = _find_db_in_try(provider, f"{job_dir}/try{last_try}", candidates)
        else:
            db = _probe_latest_try_db(provider, job_dir, candidates)
        if db is not None:
            db_paths.append(db)

    _ = config
    return db_paths


def _expand_custom_cmd(
    template: object,
    *,
    key: str,
    values: Mapping[str, str],
    db_paths: Sequence[str] = (),
) -> list[str]:
    if isinstance(template, str):
        try:
            raw_tokens = shlex.split(template)
        except ValueError as exc:
            raise ConfigError(
                f"sim.{key}: invalid command syntax {template!r}: {exc}",
                code="CFG_INVALID",
            ) from None
    elif isinstance(template, (list, tuple)) and all(isinstance(t, str) for t in template):
        raw_tokens = list(template)
    else:
        raise ConfigError(
            f"sim.{key} must be a string or list of strings, got {template!r}",
            code="CFG_INVALID",
        )
    if not raw_tokens:
        raise ConfigError(f"sim.{key} cannot be empty", code="CFG_INVALID")

    argv: list[str] = []
    used_dbs = False
    for tok in raw_tokens:
        if tok == "{dbs}":
            argv.extend(db_paths)
            used_dbs = True
            continue
        expanded = tok
        if "{dbs}" in expanded:
            expanded = expanded.replace("{dbs}", " ".join(db_paths))
            used_dbs = True
        for name, val in values.items():
            expanded = expanded.replace(f"{{{name}}}", val)
        argv.append(check_no_newlines(expanded, field=f"sim.{key}"))

    if db_paths and not used_dbs:
        argv.extend(db_paths)
    return argv


def _exec_tool(provider: Provider, argv: Sequence[str], *, tool: str) -> str:
    cmd = [check_no_newlines(str(part), field=tool) for part in argv]
    if provider.setup:
        shell_cmd = f"{provider.setup} && {' '.join(sh_quote(part, field=tool) for part in cmd)}"
        exec_argv = ["bash", "-c", shell_cmd]
    else:
        exec_argv = cmd

    exec_output = getattr(provider.transport, "exec_output", None)
    if callable(exec_output):
        try:
            rc, output = exec_output(exec_argv, cwd=".", check=False)
        except InfraError as exc:
            hint = exc.hint or (
                f"ensure {tool!r} is available on execution host or configure provider.setup"
            )
            raise InfraError(exc.message, code="PROVIDER_FAILED", hint=hint) from exc
        if rc != 0:
            detail = output.strip()
            msg = f"coverage tool {tool!r} failed with exit code {rc}"
            if detail:
                first_line = detail.splitlines()[0].strip()
                msg += f": {first_line}"
            hint = (
                f"ensure {tool!r} is installed and on PATH (or set provider.setup)"
                if rc == 127 or "not found" in detail.lower()
                else "check coverage databases and tool output on execution host"
            )
            raise InfraError(msg, code="PROVIDER_FAILED", hint=hint)
        return str(output)

    try:
        rc = provider.transport.exec(exec_argv, cwd=".", check=False)
    except InfraError as exc:
        hint = exc.hint or (
            f"ensure {tool!r} is available on execution host or configure provider.setup"
        )
        raise InfraError(exc.message, code="PROVIDER_FAILED", hint=hint) from exc
    if rc != 0:
        raise InfraError(
            f"coverage tool {tool!r} failed with exit code {rc}",
            code="PROVIDER_FAILED",
            hint=f"ensure {tool!r} is installed and on PATH (or set provider.setup)",
        )
    return ""


def _ensure_remote_dir(provider: Provider, dir_path: str) -> None:
    if not dir_path or dir_path == "." or provider.transport.exists(dir_path):
        return
    resolve = getattr(provider.transport, "_resolve", None)
    if callable(resolve) and type(provider.transport).__name__ == "LocalTransport":
        Path(resolve(dir_path)).mkdir(parents=True, exist_ok=True)
        return
    exec_output = getattr(provider.transport, "exec_output", None)
    if callable(exec_output):
        exec_output(["mkdir", "-p", dir_path], cwd=".", check=True)
    else:
        provider.transport.exec(["mkdir", "-p", dir_path], cwd=".", check=True)


def _read_optional_file(provider: Provider, path: str) -> str:
    if not provider.transport.exists(path):
        return ""
    return "\n".join(text for _, text in provider.transport.read_lines(path))


def _resolve_merged_target(
    provider: Provider,
    backend_name: str,
    merged_dir: str,
) -> tuple[str, str]:
    """Return ``(base_cov_dir, merged_db_target)`` from ``merged_dir``."""
    db_name = _MERGED_DB_NAME.get(backend_name)
    if db_name is None:
        return merged_dir, merged_dir
    if merged_dir.endswith((".vdb", ".ucdb", ".dat", ".json")):
        parent = posixpath.dirname(merged_dir) or "."
        return parent, merged_dir
    candidate = posixpath.join(merged_dir, db_name)
    if provider.transport.exists(candidate) or not provider.transport.exists(merged_dir):
        return merged_dir, candidate
    return merged_dir, candidate


def _merge_hit_dict(dst: dict[str, int], src: Mapping[object, object], prefix: str = "") -> None:
    for raw_k, raw_v in src.items():
        key = f"{prefix}{raw_k}"
        if isinstance(raw_v, int) and not isinstance(raw_v, bool):
            dst[key] = dst.get(key, 0) + max(0, raw_v)
        elif isinstance(raw_v, bool):
            dst[key] = dst.get(key, 0) + (1 if raw_v else 0)
        elif isinstance(raw_v, Mapping):
            _merge_hit_dict(dst, raw_v, prefix=f"{key}:")


def _merge_xezim_json(
    provider: Provider,
    db_paths: Sequence[str],
    merged_target: str,
) -> str:
    buckets: dict[str, dict[str, int]] = {}
    fallback_pct: dict[str, float] = {}
    bucket_keys = {
        "lines": "line",
        "line": "line",
        "branches": "branch",
        "branch": "branch",
        "toggles": "toggle",
        "toggle": "toggle",
        "conditions": "cond",
        "cond": "cond",
        "fsm": "fsm",
    }
    for db in db_paths:
        raw = _read_optional_file(provider, db)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise InfraError(
                f"invalid xezim coverage JSON in {db}: {exc}",
                code="PROVIDER_FAILED",
            ) from exc
        if not isinstance(data, Mapping):
            raise InfraError(
                f"invalid xezim coverage JSON in {db}: expected object",
                code="PROVIDER_FAILED",
            )
        for raw_k, canonical in bucket_keys.items():
            val = data.get(raw_k)
            if isinstance(val, Mapping):
                _merge_hit_dict(buckets.setdefault(canonical, {}), val)
        files_map = data.get("files")
        if isinstance(files_map, Mapping):
            for fname, fentry in files_map.items():
                if isinstance(fentry, Mapping):
                    for raw_k, canonical in bucket_keys.items():
                        sub = fentry.get(raw_k)
                        if isinstance(sub, Mapping):
                            _merge_hit_dict(
                                buckets.setdefault(canonical, {}), sub, prefix=f"{fname}:"
                            )
        for summary_key in ("summary", "metrics"):
            smap = data.get(summary_key)
            if isinstance(smap, Mapping):
                for sk, sv in smap.items():
                    canon = _HEADER_ALIAS.get(str(sk).lower())
                    if (
                        canon
                        and isinstance(sv, (int, float))
                        and not isinstance(sv, bool)
                        and 0.0 <= float(sv) <= 100.0
                    ):
                        fallback_pct[canon] = max(fallback_pct.get(canon, 0.0), float(sv))

    metrics: dict[str, float] = {}
    for canonical, hits in sorted(buckets.items()):
        if hits:
            covered = sum(1 for cnt in hits.values() if cnt > 0)
            metrics[canonical] = round(100.0 * covered / len(hits), 2)
    for canonical, pct in fallback_pct.items():
        metrics.setdefault(canonical, round(pct, 2))
    non_total = [v for k, v in metrics.items() if k != "total"]
    if non_total and "total" not in metrics:
        metrics["total"] = round(sum(non_total) / len(non_total), 2)

    merged_payload = {
        "schema": 1,
        "jobs_merged": len(db_paths),
        "hits": buckets,
        "metrics": metrics,
    }
    encoded = (json.dumps(merged_payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    provider.transport.put(merged_target, encoded)
    return f"merged {len(db_paths)} xezim coverage file(s) -> {merged_target}"


def _report_xezim_json(
    provider: Provider,
    db_target: str,
    report_dir: str,
) -> tuple[dict[str, float], str]:
    if not provider.transport.exists(db_target):
        raise InfraError(
            f"merged xezim coverage file not found: {db_target}",
            code="PROVIDER_FAILED",
            hint="run 'solouvm cov --merge' first",
        )
    raw = _read_optional_file(provider, db_target)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InfraError(
            f"invalid merged xezim coverage JSON in {db_target}: {exc}",
            code="PROVIDER_FAILED",
        ) from exc
    if not isinstance(data, Mapping):
        raise InfraError(
            f"invalid merged xezim coverage JSON in {db_target}: expected object",
            code="PROVIDER_FAILED",
        )
    metrics: dict[str, float] = {}
    raw_metrics = data.get("metrics") or data.get("summary")
    if isinstance(raw_metrics, Mapping):
        for k, v in raw_metrics.items():
            canon = _HEADER_ALIAS.get(str(k).lower(), str(k).lower())
            if isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= float(v) <= 100.0:
                metrics[canon] = round(float(v), 2)
    lines = ["Xezim Coverage Summary"]
    for key in ("total", "line", "branch", "cond", "toggle", "fsm"):
        if key in metrics:
            lines.append(f"{key.capitalize()}: {metrics[key]:.2f}%")
    summary_text = "\n".join(lines) + "\n"
    _ensure_remote_dir(provider, report_dir)
    provider.transport.put(
        posixpath.join(report_dir, "summary.txt"), summary_text.encode("utf-8")
    )
    return metrics, summary_text


def _parse_table_metrics(lines: Sequence[str]) -> dict[str, float]:
    metrics: dict[str, float] = {}
    for idx, line in enumerate(lines):
        tokens = line.strip().split()
        if len(tokens) < 2:
            continue
        col_map: list[str | None] = [_HEADER_ALIAS.get(tok.lower()) for tok in tokens]
        if sum(1 for col in col_map if col is not None) < 2:
            continue
        for next_line in lines[idx + 1 :]:
            stripped = next_line.strip()
            if not stripped:
                continue
            if set(stripped) <= {"-", "=", "|", "+", " "}:
                continue
            vals = stripped.split()
            if len(vals) != len(tokens):
                break
            if not all(_TABLE_VAL_RE.match(v) for v in vals):
                break
            for col_name, raw_val in zip(col_map, vals, strict=True):
                if col_name is None:
                    continue
                match = _TABLE_VAL_RE.match(raw_val)
                if match and match.group(1) is not None:
                    val = float(match.group(1))
                    if 0.0 <= val <= 100.0:
                        metrics.setdefault(col_name, val)
            if metrics:
                return metrics
            break
    return metrics


def _parse_coverage_metrics(text: str) -> dict[str, float]:
    """Extract normalized coverage percentages from tool text output."""
    lines = text.splitlines()
    metrics = _parse_table_metrics(lines)

    for line in lines:
        colon_m = _COLON_NUM_RE.match(line)
        if colon_m:
            raw_key = colon_m.group(1).lower().split()[0]
            canonical = _HEADER_ALIAS.get(raw_key)
            val = float(colon_m.group(2))
            if canonical is not None and 0.0 <= val <= 100.0:
                metrics.setdefault(canonical, val)
            continue
        for canonical, pattern in _METRIC_LINE_PATTERNS:
            if canonical in metrics:
                continue
            match = pattern.search(line)
            if match:
                val = float(match.group(1))
                if 0.0 <= val <= 100.0:
                    metrics[canonical] = val

    return metrics


def cov_merge(
    *,
    config: Config,
    plan: Plan,
    provider: Provider,
    results_data: dict[str, object] | None = None,
    out_dir: str | None = None,
) -> dict[str, object]:
    """Merge per-job coverage databases from passed jobs on execution host B (V-03)."""
    backend = _resolve_backend(config)
    results = _load_results(config, plan.run_id, results_data)
    db_paths = _collect_passed_dbs(
        config=config,
        plan=plan,
        provider=provider,
        backend_name=backend.name,
        results=results,
    )
    if not db_paths:
        raise ConfigError(
            "no coverage databases found in passed jobs",
            code="CFG_INVALID",
            hint="enable coverage=True in build config and run with that build",
        )

    merged_dir = check_no_newlines(
        out_dir if out_dir is not None else f".solouvm/runs/{plan.run_id}/cov",
        field="out_dir",
    )
    _ensure_remote_dir(provider, merged_dir)

    custom_merge = config.sim.get("cov_merge")
    if custom_merge is not None:
        argv = _expand_custom_cmd(
            custom_merge,
            key="cov_merge",
            values={
                "out_dir": merged_dir,
                "merged_dir": merged_dir,
                "run_id": plan.run_id,
                "top": config.top,
            },
            db_paths=db_paths,
        )
        tool = Path(argv[0]).name
    elif backend.name == "vcs":
        tool = "urg"
        argv = ["urg", "-full64"]
        for db in db_paths:
            argv.extend(["-dir", db])
        argv.extend(["-dbname", posixpath.join(merged_dir, "merged.vdb")])
    elif backend.name == "xrun":
        tool = "imc"
        merged_target = posixpath.join(merged_dir, "merged")
        tcl_path = posixpath.join(merged_dir, "merge.tcl")
        quoted_dbs = " ".join(_tcl_quote(db) for db in db_paths)
        tcl_body = f"merge -out {_tcl_quote(merged_target)} {quoted_dbs} -overwrite\nexit\n"
        provider.transport.put(tcl_path, tcl_body.encode("utf-8"))
        argv = ["imc", "-exec", tcl_path]
    elif backend.name == "questa":
        tool = "vcover"
        argv = ["vcover", "merge", posixpath.join(merged_dir, "merged.ucdb"), *db_paths]
    elif backend.name == "verilator":
        tool = "verilator_coverage"
        argv = [
            "verilator_coverage",
            "--write",
            posixpath.join(merged_dir, "merged.dat"),
            *db_paths,
        ]
    elif backend.name == "xezim":
        merged_target = posixpath.join(merged_dir, "merged_cov.json")
        out_msg = _merge_xezim_json(provider, db_paths, merged_target)
        return {
            "run_id": plan.run_id,
            "merged_dir": merged_dir,
            "job_count": len(db_paths),
            "tool": "xezim-json",
            "output": out_msg,
        }
    else:
        raise ConfigError(
            f"simulator {backend.name!r} has no built-in coverage merge command",
            code="CFG_INVALID",
            hint="use vcs, xrun, questa, verilator, xezim, or configure sim['cov_merge']",
        )

    output = _exec_tool(provider, argv, tool=tool)
    return {
        "run_id": plan.run_id,
        "merged_dir": merged_dir,
        "job_count": len(db_paths),
        "tool": tool,
        "output": output,
    }


def cov_report(
    *,
    config: Config,
    plan: Plan,
    provider: Provider,
    merged_dir: str | None = None,
) -> dict[str, object]:
    """Generate a coverage report on B and return parsed summary percentages to A (V-04)."""
    backend = _resolve_backend(config)
    raw_dir = check_no_newlines(
        merged_dir if merged_dir is not None else f".solouvm/runs/{plan.run_id}/cov",
        field="merged_dir",
    )
    base_dir, db_target = _resolve_merged_target(provider, backend.name, raw_dir)
    report_dir = posixpath.join(base_dir, "report")

    extra_files: list[str] = []
    custom_report = config.sim.get("cov_report")
    if custom_report is not None:
        argv = _expand_custom_cmd(
            custom_report,
            key="cov_report",
            values={
                "merged_dir": base_dir,
                "merged_db": db_target,
                "report_dir": report_dir,
                "out_dir": base_dir,
                "run_id": plan.run_id,
                "top": config.top,
            },
        )
        tool = Path(argv[0]).name
    elif backend.name == "vcs":
        tool = "urg"
        _ensure_remote_dir(provider, report_dir)
        argv = ["urg", "-full64", "-dir", db_target, "-report", report_dir, "-format", "both"]
        extra_files = [
            posixpath.join(report_dir, "dashboard.txt"),
            posixpath.join(report_dir, "summary.txt"),
        ]
    elif backend.name == "xrun":
        tool = "imc"
        _ensure_remote_dir(provider, report_dir)
        summary_file = posixpath.join(report_dir, "summary.txt")
        tcl_path = posixpath.join(base_dir, "report.tcl")
        tcl_body = (
            f"load -run {_tcl_quote(db_target)}\n"
            f"report -summary -out {_tcl_quote(summary_file)}\n"
            "report -summary\nexit\n"
        )
        provider.transport.put(tcl_path, tcl_body.encode("utf-8"))
        argv = ["imc", "-exec", tcl_path]
        extra_files = [summary_file]
    elif backend.name == "questa":
        tool = "vcover"
        argv = ["vcover", "report", "-summary", db_target]
    elif backend.name == "verilator":
        tool = "verilator_coverage"
        _ensure_remote_dir(provider, report_dir)
        argv = ["verilator_coverage", "--annotate", report_dir, db_target]
    elif backend.name == "xezim":
        metrics, summary_text = _report_xezim_json(provider, db_target, report_dir)
        return {
            "run_id": plan.run_id,
            "merged_dir": raw_dir,
            "report_dir": report_dir,
            "tool": "xezim-json",
            "metrics": metrics,
            "output": summary_text,
        }
    else:
        default_tool = _BUILTIN_COV_TOOLS.get(backend.name)
        raise ConfigError(
            f"simulator {backend.name!r} has no built-in coverage report command",
            code="CFG_INVALID",
            hint=(
                f"use {default_tool} or configure sim['cov_report']"
                if default_tool
                else "use vcs, xrun, questa, verilator, xezim, or configure sim['cov_report']"
            ),
        )

    output = _exec_tool(provider, argv, tool=tool)
    text_parts = [output]
    for path in extra_files:
        extra_text = _read_optional_file(provider, path)
        if extra_text:
            text_parts.append(extra_text)
    combined_text = "\n".join(part for part in text_parts if part)
    metrics = _parse_coverage_metrics(combined_text)

    return {
        "run_id": plan.run_id,
        "merged_dir": raw_dir,
        "report_dir": report_dir,
        "tool": tool,
        "metrics": metrics,
        "output": combined_text,
    }
