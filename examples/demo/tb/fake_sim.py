#!/usr/bin/env python3
"""Stand-in simulator for the demo: no EDA tool required.

Modes are selected with ``+MODE=<pass|uvm_error|fatal|hang>``.
"""

from __future__ import annotations

import sys
import time


def plus(name: str, default: str | None = None) -> str | None:
    prefix = f"+{name}="
    for arg in sys.argv[1:]:
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return default


def main() -> int:
    if "--build" in sys.argv:
        print("[demo] compiling rtl/ and tb/ ...")
        print("[demo] build ok")
        return 0

    mode = plus("MODE", "pass") or "pass"
    test = plus("UVM_TESTNAME", "demo_test")
    seed = plus("SOLOUVM_SEED", "0")
    number = plus("NUM", "1")

    print(f"UVM_INFO @ 0: reporter [RNTST] Running test {test} (seed={seed}, NUM={number})")
    print("UVM_INFO @ 0: scoreboard [DEMO] collected 32 transactions")

    if mode == "pass":
        print("UVM_INFO @ 0: reporter [TEST_DONE] test passed")
    elif mode == "uvm_error":
        print("UVM_ERROR @ 100: scoreboard [MISMATCH] expected 8, got 9")
    elif mode == "fatal":
        print("UVM_FATAL @ 100: env [DEADLOCK] no progress in 1ms")
    elif mode == "hang":
        time.sleep(600)
    else:
        print(f"unknown mode {mode!r}", file=sys.stderr)
        return 2

    print("--- UVM Report Summary ---")
    print("UVM_INFO : 32")
    print("UVM_WARNING : 0")
    print(f"UVM_ERROR : {1 if mode == 'uvm_error' else 0}")
    print(f"UVM_FATAL : {1 if mode == 'fatal' else 0}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
