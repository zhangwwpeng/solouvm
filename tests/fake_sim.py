#!/usr/bin/env python3
"""Fake simulator used by solouvm's test-suite.

Behaviour is selected with ``+MODE=<mode>`` so tests can drive every verdict
path without a real EDA tool installed.
"""

from __future__ import annotations

import os
import signal
import sys
import time


def plus(name: str, default: str | None = None) -> str | None:
    prefix = f"+{name}="
    for arg in sys.argv[1:]:
        if arg.startswith(prefix):
            return arg[len(prefix) :]
    return default


def summary(errors: int = 0, fatals: int = 0, warnings: int = 0) -> None:
    print("--- UVM Report Summary ---")
    print("UVM_INFO : 120")
    print(f"UVM_WARNING : {warnings}")
    print(f"UVM_ERROR : {errors}")
    print(f"UVM_FATAL : {fatals}")


def main() -> int:
    mode = plus("MODE", "pass") or "pass"
    test = plus("UVM_TESTNAME", "fake_test")

    # Non-UVM modes must not emit UVM markers: they exercise the SOLOUVM_DONE path.
    if mode == "plain_pass":
        print("SOLOUVM_DONE checks=4 status=pass")
        return 0
    if mode == "plain_fail":
        print("SOLOUVM_DONE checks=4 status=fail")
        return 0
    if mode == "plain_none":
        print("hello, no marker here")
        return 0

    print(f"UVM_INFO @ 0: reporter [RNTST] Running test {test}...")
    print(
        "UVM_INFO @ 0: env [FAKE] case="
        f"{plus('SOLOUVM_CASE_ID')} seed={plus('SOLOUVM_SEED')} outdir={plus('SOLOUVM_OUTDIR')}"
    )

    if mode == "pass":
        print("TEST PASSED")
        summary()
        return 0
    if mode == "uvm_error":
        print("UVM_ERROR @ 0: scoreboard [MISMATCH] data mismatch")
        summary(errors=1)
        return 0
    if mode == "fatal":
        print("UVM_FATAL @ 0: env [BROKEN] fatal condition")
        summary(fatals=1)
        return 0
    if mode == "nosummary":
        print("UVM_INFO @ 0: tb [EARLY] simulation died before the summary")
        return 0
    if mode == "rc1":
        print("TEST FAILED")
        summary()
        return 1
    if mode == "hang":
        time.sleep(60)
        return 0
    if mode == "crash":
        os.kill(os.getpid(), signal.SIGSEGV)
        time.sleep(1)
        return 0
    print(f"unknown mode {mode!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
