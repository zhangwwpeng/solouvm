"""Exceptions and the exit-code contract.

Every failure surfaced to a shell has exactly one of these codes:

=====  ==============================================================
0      everything that was supposed to pass, passed
1      at least one case failed / timed out / errored (test failure)
2      infrastructure or user-configuration problem (nothing to judge)
130    interrupted by SIGINT
=====  ==============================================================
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_TEST_FAIL = 1
EXIT_INFRA = 2
EXIT_INTERRUPT = 130


class SolouvmError(Exception):
    """Base class for all errors that abort a stage."""

    exit_code = EXIT_INFRA

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def render(self) -> str:
        if self.hint:
            return f"{self.message}\n  hint: {self.hint}"
        return self.message


class ConfigError(SolouvmError):
    """Invalid or missing user configuration (``solouvm.py``, ``case.py``...)."""


class InfraError(SolouvmError):
    """Environment problems: missing simulator, unreadable plan, write failures."""
