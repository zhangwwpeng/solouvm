from __future__ import annotations

import os
import time


def hook(ctx) -> None:
    total = len(ctx.plan.jobs) if ctx.plan else 0
    ctx.log(f"evaluating results & expect rules for {total} job(s)")
    delay = float(os.environ.get("SMOKE_DELAY", "0.15"))
    if delay > 0:
        time.sleep(delay)
