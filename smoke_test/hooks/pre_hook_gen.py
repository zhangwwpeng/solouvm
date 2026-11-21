from __future__ import annotations

import os
import time


def hook(ctx) -> None:
    jobs = len(ctx.plan.jobs) if ctx.plan else 0
    ctx.log(f"preparing gen for run_id={ctx.run_id} ({jobs} job(s))")
    delay = float(os.environ.get("SMOKE_DELAY", "0.15"))
    if delay > 0:
        time.sleep(delay)
