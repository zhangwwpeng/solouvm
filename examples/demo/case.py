from solouvm import Case

CASES = [
    Case(name="dut_smoke", uvm_test="dut_smoke_test", params={"MODE": "pass"}),
    Case(
        name="dut_full",
        uvm_test="dut_full_test",
        params={"MODE": "pass", "NUM": 32},
        tags=("nightly",),
    ),
    # Negative test: it is expected to fail, so a failure counts as XFAIL.
    Case(
        name="known_bug",
        uvm_test="dut_full_test",
        params={"MODE": "uvm_error"},
        expect_fail=True,
        tags=("known-issue",),
    ),
]
