"""Generate ``.solouvm/Makefile``, root ``Makefile`` and ``run.mk`` (G-01..G-05, N-11, N-12)."""

from __future__ import annotations

import re
import shlex
from typing import TYPE_CHECKING

from solouvm.errors import ConfigError
from solouvm.gen.shell import check_no_newlines, make_escape, relpath, sh_quote
from solouvm.units import parse_duration

if TYPE_CHECKING:
    from solouvm.backends.base import Backend
    from solouvm.collect.files import FileList
    from solouvm.config.schema import Config
    from solouvm.model import Job, Plan

__all__ = [
    "PLACEHOLDERS",
    "expand_template",
    "render_entry_makefile",
    "render_root_makefile",
    "render_run_mk",
]

PLACEHOLDERS = frozenset(
    {
        "root",
        "filelist",
        "filelist_rtl",
        "filelist_tb",
        "filelist_top",
        "top",
        "build_dir",
        "simv",
        "seed",
        "job_dir",
        "job_id",
        "case",
        "uvm_test",
        "fake_top",
        "check_dir",
    }
)

# Avoid matching shell ${VAR} / ${VAR:-default}; only match {identifier} placeholders.
_PLACEHOLDER_RE = re.compile(r"(?<!\$)\{(\w+)\}")
_BAD_TARGET_RE = re.compile(r"[\s:#=$]")
_SHELL_OPS = frozenset({"&&", "||", ";", "|"})


def _wrap_shell_tokens(
    tokens: list[str],
    *,
    field: str,
    append_args: bool = False,
) -> list[str]:
    if not any(tok in _SHELL_OPS for tok in tokens):
        return tokens
    shell_parts = [tok if tok in _SHELL_OPS else sh_quote(tok, field=field) for tok in tokens]
    script = " ".join(shell_parts)
    if append_args:
        return ["bash", "-c", f'{script} "$@"', "_"]
    return ["bash", "-c", script]


def _check_target_token(value: str, *, field: str) -> str:
    check_no_newlines(value, field=field)
    if not value or _BAD_TARGET_RE.search(value):
        raise ConfigError(
            f"{field} contains invalid Makefile target characters: {value!r}",
            code="CFG_INVALID",
        )
    return value


def _fmt_seconds(seconds: float) -> str:
    if float(seconds).is_integer():
        return str(int(seconds))
    return f"{seconds:g}"


def expand_template(template: str, values: dict[str, str], *, where: str) -> str:
    """Expand ``{name}`` placeholders in ``template`` against ``values``."""

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            raise ConfigError(
                f"unknown placeholder '{{{name}}}' in {where}",
                code="CFG_INVALID",
            )
        return values[name]

    return _PLACEHOLDER_RE.sub(repl, template)


def _expand_tokens(template: str, values: dict[str, str], *, where: str) -> list[str]:
    try:
        raw_tokens = shlex.split(template)
    except ValueError as exc:
        raise ConfigError(
            f"{where}: invalid command syntax {template!r}: {exc}",
            code="CFG_INVALID",
        ) from None
    return [expand_template(tok, values, where=where) for tok in raw_tokens]


def _is_subslice(needle: list[str], haystack: list[str]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    span = len(needle)
    return any(haystack[i : i + span] == needle for i in range(len(haystack) - span + 1))


def _merge_run_opts(
    *,
    build_opts: list[str],
    global_opts: list[str],
    job_opts: list[str],
) -> list[str]:
    """Combine build, global and job run options without duplicating already-merged slices."""
    merged: list[str] = []
    for source in (build_opts, global_opts):
        if source and not _is_subslice(source, job_opts) and not _is_subslice(source, merged):
            merged.extend(source)
    merged.extend(job_opts)
    return merged


def _build_placeholders(
    *,
    rel_build_dir: str,
    run_dir: str,
    config: Config,
    backend: Backend,
) -> dict[str, str]:
    return {
        "root": relpath(".", run_dir),
        "filelist": relpath(f"{rel_build_dir}/filelist.f", run_dir),
        "filelist_rtl": relpath(f"{rel_build_dir}/filelist.rtl.f", run_dir),
        "filelist_tb": relpath(f"{rel_build_dir}/filelist.tb.f", run_dir),
        "filelist_top": relpath(f"{rel_build_dir}/filelist.top.f", run_dir),
        "top": config.top,
        "build_dir": relpath(rel_build_dir, run_dir),
        "simv": relpath(f"{rel_build_dir}/{backend.simv}", run_dir),
    }


def _job_placeholders(
    *,
    job: Job,
    rel_build_dir: str,
    try_dir: str,
    config: Config,
    backend: Backend,
) -> dict[str, str]:
    values = _build_placeholders(
        rel_build_dir=rel_build_dir,
        run_dir=try_dir,
        config=config,
        backend=backend,
    )
    values.update(
        {
            "seed": str(job.seed),
            "job_dir": relpath(try_dir, try_dir),
            "job_id": job.job_id,
            "case": job.case,
            "uvm_test": job.uvm_test or "",
        }
    )
    return values


def _render_build_recipe(
    *,
    mode: str,
    key: str,
    rel_build_dir: str,
    config: Config,
    backend: Backend,
    provider_setup: str,
    run_id: str,
) -> list[str]:
    build_cfg = config.builds[mode]
    ctx = _build_placeholders(
        rel_build_dir=rel_build_dir,
        run_dir=rel_build_dir,
        config=config,
        backend=backend,
    )
    cmd_tokens = _expand_tokens(backend.build, ctx, where=f"backend {backend.name!r} build")
    if build_cfg.coverage and backend.cov_build:
        cmd_tokens.extend(
            _expand_tokens(
                backend.cov_build,
                ctx,
                where=f"backend {backend.name!r} cov_build",
            )
        )
    cmd_tokens.extend(build_cfg.compile_opts)
    if not cmd_tokens:
        raise ConfigError(f"build command for mode {mode!r} is empty", code="CFG_INVALID")
    cmd_tokens = _wrap_shell_tokens(cmd_tokens, field=f"build[{mode!r}] cmd", append_args=False)

    timeout_s = _fmt_seconds(parse_duration(build_cfg.timeout))
    build_job_id = f"build__{mode}__{key}"

    runner_args = [
        "$(SUBMIT)",
        "$(RUN_JOB)",
        "--kind",
        "build",
        "--dir",
        f"$(B_{mode})",
        "--log",
        "build.log",
        "--lock",
        "--run-id",
        "$(RUN_ID)",
        "--job-id",
        make_escape(build_job_id, field="build job_id"),
        "--timeout",
        timeout_s,
    ]
    if provider_setup:
        runner_args.extend(["--setup", make_escape(provider_setup, field="provider.setup")])
    escaped_cmd = [make_escape(tok, field=f"build[{mode!r}] cmd") for tok in cmd_tokens]
    _ = run_id  # already passed via $(RUN_ID)
    return [
        f"$(B_{mode})/.ok:",
        f"\t{' '.join(runner_args)} \\",
        f"\t    -- {' '.join(escaped_cmd)}",
    ]


def _render_job_recipe(
    *,
    job: Job,
    rel_build_dir: str,
    config: Config,
    backend: Backend,
    provider_setup: str,
) -> list[str]:
    build_cfg = config.builds[job.build]
    try_dir = f"{job.dir}/try1"
    ctx = _job_placeholders(
        job=job,
        rel_build_dir=rel_build_dir,
        try_dir=try_dir,
        config=config,
        backend=backend,
    )

    cmd_tokens = _expand_tokens(backend.run, ctx, where=f"backend {backend.name!r} run")
    if "{seed}" not in backend.run and backend.seed:
        cmd_tokens.extend(_expand_tokens(backend.seed, ctx, where=f"backend {backend.name!r} seed"))
    if job.uvm_test and "{uvm_test}" not in backend.run:
        cmd_tokens.append(f"+UVM_TESTNAME={job.uvm_test}")
    cmd_tokens.append(f"+SOLOUVM_SEED={job.seed}")
    cmd_tokens.append(f"+SOLOUVM_JOB_ID={job.job_id}")

    for param_key, param_val in sorted(job.params.items()):
        if isinstance(param_val, bool):
            if param_val:
                cmd_tokens.append(f"+{param_key}")
            continue
        cmd_tokens.append(f"+{param_key}={param_val}")

    cmd_tokens.extend(
        _merge_run_opts(
            build_opts=list(build_cfg.run_opts),
            global_opts=list(config.run.opts),
            job_opts=list(job.opts),
        )
    )
    if build_cfg.coverage and backend.cov_run:
        cmd_tokens.extend(
            _expand_tokens(
                backend.cov_run,
                ctx,
                where=f"backend {backend.name!r} cov_run",
            )
        )

    wave_tokens = (
        _expand_tokens(backend.wave, ctx, where=f"backend {backend.name!r} wave")
        if backend.wave
        else []
    )
    cmd_tokens = _wrap_shell_tokens(
        cmd_tokens,
        field=f"job {job.job_id!r} cmd",
        append_args=bool(wave_tokens),
    )
    escaped_cmd = [make_escape(tok, field=f"job {job.job_id!r} cmd") for tok in cmd_tokens]
    if wave_tokens:
        escaped_wave = " ".join(
            make_escape(tok, field=f"backend {backend.name!r} wave") for tok in wave_tokens
        )
        escaped_cmd.append(f"$(if $(filter 1,$(WAVE)),{escaped_wave},)")

    dir_arg = f"{make_escape(job.dir, field=f'job {job.job_id!r} dir')}/try$(TRY)"
    runner_args = [
        "$(SUBMIT)",
        "$(RUN_JOB)",
        "--kind",
        "job",
        "--dir",
        dir_arg,
        "--log",
        "sim.log",
        "--run-id",
        "$(RUN_ID)",
        "--job-id",
        make_escape(job.job_id, field="job_id"),
        "--try",
        "$(TRY)",
        "--timeout",
        _fmt_seconds(job.timeout_s),
    ]
    if job.hang_s is not None:
        runner_args.extend(["--hang", _fmt_seconds(job.hang_s)])
    if provider_setup:
        runner_args.extend(["--setup", make_escape(provider_setup, field="provider.setup")])
    if config.job.pre:
        runner_args.extend(["--pre", make_escape(config.job.pre, field="job.pre")])
    if config.job.post:
        runner_args.extend(["--post", make_escape(config.job.post, field="job.post")])
    if config.run.mem_limit:
        runner_args.extend(
            ["--mem-limit", make_escape(config.run.mem_limit, field="run.mem_limit")]
        )

    merged_env = {**config.run.env, **job.env}
    for env_key, env_val in sorted(merged_env.items()):
        check_no_newlines(env_key, field=f"env key {env_key!r}")
        if not env_key or "=" in env_key:
            raise ConfigError(f"invalid env key {env_key!r}", code="CFG_INVALID")
        runner_args.extend(
            [
                "--env",
                make_escape(f"{env_key}={env_val}", field=f"env[{env_key!r}]"),
            ]
        )

    return [
        f"run/{job.job_id}: $(B_{job.build})/.ok",
        f"\t{' '.join(runner_args)} \\",
        f"\t    -- {' '.join(escaped_cmd)}",
    ]


def render_entry_makefile(version: str = "0.1.0") -> str:
    """Render ``.solouvm/Makefile`` pointing to the latest run's ``run.mk``."""
    check_no_newlines(version, field="version")
    return (
        f"# generated by solouvm {version} -- do not edit\n-include .solouvm/runs/latest/run.mk\n"
    )


def render_root_makefile(version: str = "0.1.0") -> str:
    """Render the project-root ``Makefile`` created only when absent."""
    check_no_newlines(version, field="version")
    return f"# generated by solouvm {version} -- do not edit\ninclude .solouvm/Makefile\n"


def render_run_mk(
    *,
    plan: Plan,
    config: Config,
    files: FileList,
    build_keys: dict[str, str],
    backend: Backend,
    version: str = "0.1.0",
) -> str:
    """Render the self-contained per-run ``run.mk`` deterministically (N-11)."""
    _ = files  # kept in signature for caller symmetry
    check_no_newlines(version, field="version")
    check_no_newlines(plan.run_id, field="plan.run_id")
    check_no_newlines(config.top, field="top")

    modes = sorted(build_keys)
    for mode in modes:
        _check_target_token(mode, field="build mode")
        _check_target_token(build_keys[mode], field=f"build key for {mode!r}")
        if mode not in config.builds:
            raise ConfigError(f"unknown build mode {mode!r}", code="CFG_INVALID")

    for job in plan.jobs:
        _check_target_token(job.job_id, field="job_id")
        if job.build not in build_keys:
            raise ConfigError(
                f"job {job.job_id!r} uses build mode {job.build!r} missing from build_keys",
                code="CFG_INVALID",
            )

    provider_cfg = config.providers.get(plan.provider) or config.providers.get(config.provider)
    provider_setup = provider_cfg.setup if provider_cfg is not None else ""

    run_id_make = plan.run_id.replace("$", "$$")
    lines: list[str] = [
        f"# generated by solouvm {version} -- do not edit",
        "ROOT    := $(CURDIR)",
        f"RUN_ID  := {run_id_make}",
        'RUN_JOB := bash "$(ROOT)/.solouvm/gen/bin/run_job.sh"',
        "SUBMIT  ?=",
        "TRY     ?= 1",
        "WAVE    ?= 0",
        "",
    ]

    build_dirs: dict[str, str] = {}
    for mode in modes:
        rel_dir = f".solouvm/build/{mode}/{build_keys[mode]}"
        build_dirs[mode] = rel_dir
        lines.append(f"B_{mode} := {rel_dir}")
    if modes:
        lines.append("")

    phony_targets = ["build", *(f"build/{m}" for m in modes), "regress", "clean"]
    phony_targets.extend(f"run/{job.job_id}" for job in plan.jobs)
    lines.append(f".PHONY: {' '.join(phony_targets)}")
    lines.append("")

    build_prereqs = " ".join(f"$(B_{m})/.ok" for m in modes)
    lines.append(f"build: {build_prereqs}" if build_prereqs else "build:")
    lines.append("")

    for mode in modes:
        lines.append(f"build/{mode}: $(B_{mode})/.ok")
        lines.append("")
        lines.extend(
            _render_build_recipe(
                mode=mode,
                key=build_keys[mode],
                rel_build_dir=build_dirs[mode],
                config=config,
                backend=backend,
                provider_setup=provider_setup,
                run_id=plan.run_id,
            )
        )
        lines.append("")

    regress_prereqs = " ".join(f"run/{job.job_id}" for job in plan.jobs)
    lines.append(f"regress: {regress_prereqs}" if regress_prereqs else "regress:")
    lines.append("")

    for job in plan.jobs:
        lines.extend(
            _render_job_recipe(
                job=job,
                rel_build_dir=build_dirs[job.build],
                config=config,
                backend=backend,
                provider_setup=provider_setup,
            )
        )
        lines.append("")

    if modes:
        clean_targets = " ".join(f"$(B_{m}) $(B_{m}).lock" for m in modes)
        lines.append("clean:")
        lines.append(f"\trm -rf {clean_targets}")
    else:
        lines.append("clean:")
    lines.append("")

    return "\n".join(lines)
