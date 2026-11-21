from __future__ import annotations

import os
import time


def hook(ctx) -> None:
    summary = (ctx.payload or {}).get("summary") or {}
    passed = summary.get("passed", 0)
    total = summary.get("total", 0)
    ctx.log(f"judge complete: passed={passed}/{total} (exit_code={ctx.exit_code})")
    ctx.echo(f"smoke_test verdict: {passed}/{total} passed (run={ctx.run_id})")
    delay = float(os.environ.get("SMOKE_DELAY", "0.15"))
    if delay > 0:
        time.sleep(delay)
