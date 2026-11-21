"""Exception hierarchy and stable error codes (formats.md §9)."""

from __future__ import annotations

from os import PathLike

# 错误码 → 退出码；只增不改（N-18）
ERROR_CODES: dict[str, int] = {
    # 退出码 2：命令没能完成
    **dict.fromkeys(
        (
            "CFG_NOT_FOUND",
            "CFG_UNKNOWN_KEY",
            "CFG_INVALID",
            "CFG_LOAD_FAILED",
            "FILES_MISSING",
            "CASE_INVALID",
            "CASE_PLANNED",
            "RECORD_INVALID",
            "RECORD_CONFLICT",
            "RECORD_WRITE_FAILED",
            "SELECT_EMPTY",
            "LIMIT_EXCEEDED",
            "ROLE_DENIED",
            "BUILD_FAILED",
            "PROVIDER_FAILED",
            "GIT_DIRTY",
            "RUN_ACTIVE",
            "RUN_NOT_FOUND",
            "JOB_NOT_FOUND",
            "TASK_NOT_FOUND",
            "TRIAGE_INVALID",
            "UVMGEN_EXISTS",
            "CHECK_UNAVAILABLE",
            "HOOK_FAILED",
        ),
        2,
    ),
    # 退出码 1：命令完成了，但结果不好
    **dict.fromkeys(("TESTS_FAILED", "CHECK_FAILED", "STYLE_VIOLATION", "TRIAGE_PENDING"), 1),
    # 退出码 130：被中断
    "INTERRUPTED": 130,
}


class SolouvmError(Exception):
    """Base class of all expected solouvm errors."""

    # 基类不直接使用；子类覆盖默认错误码
    code: str = ""

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        hint: str | None = None,
        file: str | PathLike[str] | None = None,
        line: int | None = None,
    ) -> None:
        super().__init__(message)
        if code is not None:
            if code not in ERROR_CODES:
                raise ValueError(f"unknown error code {code!r}")
            # 写在实例上，不影响类上的默认值
            self.code = code
        if line is not None and file is None:
            raise ValueError("'line' requires 'file'")
        self.message = message
        self.hint = hint
        self.file = None if file is None else str(file)
        self.line = line

    @property
    def exit_code(self) -> int:
        return ERROR_CODES[self.code]

    def __str__(self) -> str:
        if self.file is None:
            return self.message
        if self.line is None:
            return f"{self.file}: {self.message}"
        return f"{self.file}:{self.line}: {self.message}"

    def to_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "hint": self.hint,
        }


class ConfigError(SolouvmError):
    code = "CFG_INVALID"


class InfraError(SolouvmError):
    code = "PROVIDER_FAILED"


class BuildError(SolouvmError):
    code = "BUILD_FAILED"


class RoleError(SolouvmError):
    code = "ROLE_DENIED"


class RunActiveError(SolouvmError):
    code = "RUN_ACTIVE"


class RecordError(SolouvmError):
    code = "RECORD_INVALID"


def format_error(err: SolouvmError) -> str:
    """Human-readable error text: ``solouvm: error: ...`` plus an optional hint line."""
    text = f"solouvm: error: {err}"
    if err.hint:
        text += f"\nhint: {err.hint}"
    return text
