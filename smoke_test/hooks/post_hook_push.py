from __future__ import annotations

import os
import time


def hook(ctx) -> None:
    pname = ctx.provider.name if ctx.provider else "-"
    ctx.log(f"push completed cleanly to provider={pname}")
    delay = float(os.environ.get("SMOKE_DELAY", "0.15"))
    if delay > 0:
        time.sleep(delay)
