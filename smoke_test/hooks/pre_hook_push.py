from __future__ import annotations

import os
import time


def hook(ctx) -> None:
    pname = ctx.provider.name if ctx.provider else "-"
    manifest = ctx.extra.get("manifest") or []
    ctx.log(f"about to sync {len(manifest)} artifact(s) via provider={pname}")
    delay = float(os.environ.get("SMOKE_DELAY", "0.15"))
    if delay > 0:
        time.sleep(delay)
