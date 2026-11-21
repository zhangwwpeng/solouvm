from __future__ import annotations

import os
import time


def hook(ctx) -> None:
    cached = bool(ctx.extra.get("all_cached", False))
    rc = ctx.extra.get("build_rc", 0)
    ok_modes = ctx.extra.get("ok_modes")
    detail = f"ok_modes={ok_modes}" if ok_modes is not None else f"modes={ctx.extra.get('modes')}"
    ctx.log(f"build stage finished (rc={rc}, all_cached={cached}, {detail})")
    delay = float(os.environ.get("SMOKE_DELAY", "0.15"))
    if delay > 0:
        time.sleep(delay)
