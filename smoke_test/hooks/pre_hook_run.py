from __future__ import annotations

import os
import time


def hook(ctx) -> None:
    jobs = ctx.extra.get("jobs") or []
    ctx.log(f"launching {len(jobs)} simulation job(s): {', '.join(str(j) for j in jobs)}")
    delay = float(os.environ.get("SMOKE_DELAY", "0.15"))
    if delay > 0:
        time.sleep(delay)
