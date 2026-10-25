"""``pre_hook_<stage>.py`` / ``post_hook_<stage>.py`` support.

Contract (deliberately small, and identical for every stage):

* the module lives at the project root and must define ``hook(ctx)``;
* ``ctx`` is a plain dict with ``stage``, ``when``, ``command``, ``root``,
  ``workdir`` and (when available) ``plan``;
* the return value is ignored;
* any exception aborts the command with exit code 2 — hooks are part of the
  build, so a broken hook must never silently degrade a regression.
"""

from __future__ import annotations

import runpy
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .errors import ConfigError

STAGES = ("main", "com", "run", "end")
WHENS = ("pre", "post")


def path_for(root: Path, when: str, stage: str) -> Path:
    return root / f"{when}_hook_{stage}.py"


def run(stage: str, when: str, ctx: Mapping[str, Any]) -> bool:
    """Execute one hook if it exists. Returns ``True`` when a hook ran."""
    if stage not in STAGES:
        raise ConfigError(f"unknown hook stage {stage!r}")
    if when not in WHENS:
        raise ConfigError(f"unknown hook timing {when!r}")

    root = Path(ctx["root"])
    path = path_for(root, when, stage)
    if not path.is_file():
        return False

    previous = list(sys.path)
    sys.path.insert(0, str(root))
    try:
        namespace = runpy.run_path(str(path), run_name=f"solouvm_hook_{when}_{stage}")
    except ConfigError:
        raise
    except Exception as exc:
        raise ConfigError(
            f"{path.name} failed: {type(exc).__name__}: {exc}"
        ) from exc
    finally:
        sys.path[:] = previous

    hook = namespace.get("hook")
    if not callable(hook):
        raise ConfigError(
            f"{path.name} must define a callable named hook(ctx)",
            hint="def hook(ctx): ...",
        )
    try:
        hook(dict(ctx))
    except Exception as exc:
        raise ConfigError(
            f"{path.name} raised {type(exc).__name__}: {exc}",
            hint="hooks are part of the pipeline; fix or remove the hook",
        ) from exc
    return True
