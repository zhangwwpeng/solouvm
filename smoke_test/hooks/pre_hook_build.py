from __future__ import annotations

import os
import time


def hook(ctx) -> None:
    modes = ctx.extra.get("modes") or []
    ctx.log(f"starting compile check for mode(s): {', '.join(str(m) for m in modes)}")
    delay = float(os.environ.get("SMOKE_DELAY", "0.15"))
    if delay > 0:
        time.sleep(delay)
