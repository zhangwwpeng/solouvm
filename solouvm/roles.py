"""Role definitions and command allowlists for ``--role`` (AI-10, cli.md §1.5)."""

from __future__ import annotations

from solouvm.errors import RoleError

VALID_ROLES: frozenset[str] = frozenset({"main", "triage"})

TRIAGE_READONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "uvm diff",
        "lint-style",
        "list",
        "files",
        "config",
        "validate",
        "status",
        "log",
        "explain",
        "wave",
        "notes",
        "history",
        "progress",
        "schema",
        "help",
        "triage packet",
        "triage status",
        "version",
    }
)

TRIAGE_WRITE_COMMANDS: frozenset[str] = frozenset(
    {
        "bug add",
        "bug update",
        "note add",
        "todo add",
        "triage write",
    }
)

MAIN_ONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "init",
        "doctor",
        "new",
        "uvm sync",
        "gen",
        "build",
        "run",
        "regress",
        "rerun",
        "check",
        "stop",
        "judge",
        "report",
        "todo done",
        "triage plan",
        "triage review",
        "doc",
        "cov merge",
        "cov report",
        "bisect",
        "clean",
        "update",
    }
)

_TRIAGE_ALLOWED: frozenset[str] = TRIAGE_READONLY_COMMANDS | TRIAGE_WRITE_COMMANDS

ROLE_ALLOWED: dict[str, frozenset[str]] = {
    "triage": _TRIAGE_ALLOWED,
    "main": _TRIAGE_ALLOWED | MAIN_ONLY_COMMANDS,
}

__all__ = [
    "MAIN_ONLY_COMMANDS",
    "ROLE_ALLOWED",
    "TRIAGE_READONLY_COMMANDS",
    "TRIAGE_WRITE_COMMANDS",
    "VALID_ROLES",
    "check_role",
]


def check_role(role: str, command: str) -> None:
    """Validate ``role`` and ensure ``command`` is permitted for that role.

    Raises ``RoleError`` (``ROLE_DENIED``, exit code 2) if ``role`` is unknown
    or ``command`` is outside the role's allowlist.
    """
    if role not in VALID_ROLES:
        raise RoleError(
            f"unknown role {role!r}",
            code="ROLE_DENIED",
            hint=f"valid roles: {', '.join(sorted(VALID_ROLES))}",
        )
    if role == "main":
        return
    normalized = " ".join(command.split())
    allowed = ROLE_ALLOWED[role]
    if normalized not in allowed:
        allowed_list = ", ".join(sorted(allowed))
        raise RoleError(
            f"command {normalized!r} is not allowed under role {role!r}",
            code="ROLE_DENIED",
            hint=f"allowed commands for {role}: {allowed_list}",
        )
