from __future__ import annotations

import os
import time


def hook(ctx) -> None:
    manifest = ctx.extra.get("manifest") or []
    builds = ctx.extra.get("builds") or {}
    ctx.log(f"generated {len(manifest)} file(s); builds={builds}")
    delay = float(os.environ.get("SMOKE_DELAY", "0.15"))
    if delay > 0:
        time.sleep(delay)
