"""Fast compile checks (``solouvm check``): slang, fake_top, and RTL incremental (E-01..E-11)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from solouvm.check.faketop import HIER_REF_HINT, extract_tb_packages, run_faketop
from solouvm.check.incremental import (
    FAST_STATE_REL,
    FAST_STATE_SCHEMA,
    IncrementalPlan,
    IncrementalResult,
    plan_rtl_incremental,
    read_fast_state,
    run_incremental_rtl,
    write_fast_state,
)
from solouvm.check.slang import (
    VALID_FORMATS,
    CheckDiagnostic,
    CheckResult,
    format_check_result,
    generate_check_dotf,
    is_remote_provider,
    resolve_check_where,
    run_slang,
)
from solouvm.errors import ConfigError

if TYPE_CHECKING:
    from solouvm.backends.base import Backend
    from solouvm.collect.files import FileList
    from solouvm.config.schema import Config
    from solouvm.providers.base import Provider

__all__ = [
    "FAST_STATE_REL",
    "FAST_STATE_SCHEMA",
    "HIER_REF_HINT",
    "CheckDiagnostic",
    "CheckResult",
    "IncrementalPlan",
    "IncrementalResult",
    "extract_tb_packages",
    "format_check_result",
    "generate_check_dotf",
    "plan_rtl_incremental",
    "read_fast_state",
    "resolve_check_where",
    "run_check",
    "run_faketop",
    "run_incremental_rtl",
    "run_slang",
    "write_fast_state",
]


def _count_severities(diagnostics: list[CheckDiagnostic]) -> tuple[int, int]:
    errors = sum(1 for d in diagnostics if d.severity == "error")
    warnings = sum(1 for d in diagnostics if d.severity == "warning")
    return errors, warnings


def run_check(
    *,
    config: Config,
    files: FileList,
    backend: Backend,
    provider: Provider,
    slang_only: bool = False,
    no_faketop: bool = False,
    where: str | None = None,
    fmt: str = "text",
    incremental_rtl: bool = False,
) -> tuple[CheckResult, int]:
    """Run layered fast-compile checks and return ``(CheckResult, exit_code)``.

    Layers execute sequentially (slang -> fake_top -> incremental RTL) and stop
    at the first layer with errors. Raises ``InfraError(CHECK_UNAVAILABLE)``
    (exit code 2) when ``slang`` or the simulator cannot execute.
    """
    if fmt not in VALID_FORMATS:
        raise ConfigError(
            f"unsupported check format {fmt!r}; expected one of: text, gcc, json",
            code="CFG_INVALID",
        )

    requested_where = where if where is not None else config.check.where
    slang_res = run_slang(
        config=config,
        files=files,
        provider=provider,
        where=where,
    )

    diagnostics = list(slang_res.diagnostics)
    warnings = list(slang_res.warnings)
    layers = list(slang_res.layers)
    duration_s = slang_res.duration_s
    error_count, warning_count = _count_severities(diagnostics)

    result = CheckResult(
        ok=slang_res.ok,
        unavailable=False,
        where=slang_res.where,
        files_checked=slang_res.files_checked,
        duration_s=duration_s,
        diagnostics=diagnostics,
        error_count=error_count,
        warning_count=warning_count,
        layers=layers,
        warnings=warnings,
        incremental_files=[],
    )

    if not slang_res.ok or slang_only:
        return result, (0 if result.ok else 1)

    # Simulator-backed layers follow the active provider unless --where local forced A.
    sim_where = (
        "local"
        if requested_where == "local"
        else ("remote" if is_remote_provider(provider) else slang_res.where)
    )

    if not no_faketop and config.check.faketop:
        ft_res = run_faketop(
            config=config,
            files=files,
            backend=backend,
            provider=provider,
            where=sim_where,
        )
        warnings.extend(ft_res.warnings)
        if not ft_res.skipped:
            layers.append("faketop")
            duration_s += ft_res.duration_s
            diagnostics.extend(ft_res.diagnostics)
        error_count, warning_count = _count_severities(diagnostics)
        result.duration_s = duration_s
        result.error_count = error_count
        result.warning_count = warning_count
        result.ok = ft_res.ok and error_count == 0
        if not result.ok:
            return result, 1

    if incremental_rtl:
        inc_res = run_incremental_rtl(
            config=config,
            files=files,
            backend=backend,
            provider=provider,
            where=sim_where,
        )
        warnings.extend(inc_res.warnings)
        if not inc_res.skipped:
            layers.append("incremental")
            duration_s += inc_res.duration_s
            diagnostics.extend(inc_res.diagnostics)
            result.incremental_files = list(inc_res.files_compiled)
        error_count, warning_count = _count_severities(diagnostics)
        result.duration_s = duration_s
        result.error_count = error_count
        result.warning_count = warning_count
        result.ok = inc_res.ok and error_count == 0
        if not result.ok:
            return result, 1

    return result, 0
