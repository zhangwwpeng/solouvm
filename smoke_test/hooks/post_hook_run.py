from __future__ import annotations

import os
import time


def hook(ctx) -> None:
    ctx.log(f"simulation stage finished (stopped_early={ctx.extra.get('stopped_early', False)})")
    delay = float(os.environ.get("SMOKE_DELAY", "0.15"))
    if delay > 0:
        time.sleep(delay)
