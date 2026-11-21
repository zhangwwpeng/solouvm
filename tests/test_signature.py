import hashlib

import pytest

from solouvm.judge.signature import normalize_signature, signature_id


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        # verdict.md §8 的示例，逐字符对比
        (
            "UVM_ERROR verif/envs/axi/axi_scb.sv(120) @ 1234ns: uvm_test_top.env.scb [AXI_SCB] "
            "mismatch: expect 0x00 got 0x1f (addr 0x1f00)",
            "UVM_ERROR uvm_test_top.env.scb [AXI_SCB] mismatch: "
            "expect <HEX> got <HEX> (addr <HEX>)",
        ),
        # 文件行号被 +UVM_REPORT_DISABLE_FILE_LINE 关掉
        (
            "UVM_FATAL @ 52340: uvm_test_top.env [CFG] missing vif",
            "UVM_FATAL uvm_test_top.env [CFG] missing vif",
        ),
        # 路径只保留文件名，十进制数替换
        (
            "Error-[NOA] Null object access /home/u/proj/verif/tb/top.sv, 42",
            "Error-[NOA] Null object access top.sv, <N>",
        ),
        # 引号内的字符串保留，但其中的数字同样替换
        (
            "UVM_ERROR a.sv(1) @ 10ns: rep [SEQ] item 'pkt_7 len=64' dropped after 3 tries",
            "UVM_ERROR rep [SEQ] item 'pkt_7 len=<N>' dropped after <N> tries",
        ),
        # 连续空白压成一个，标识符里的数字不动
        (
            "  xmsim: *E,ASRTST   (./tb/axi4_if.sv,88):   assertion   failed at 500 NS  ",
            "xmsim: *E,ASRTST (axi4_if.sv,<N>): assertion failed at <N> NS",
        ),
        ("Offending 'axi_wrap_chk'", "Offending 'axi_wrap_chk'"),
    ],
)
def test_normalize_signature(line, expected):
    assert normalize_signature(line) == expected


def test_same_problem_same_signature():
    a = "UVM_ERROR x/a.sv(1) @ 10ns: scb [AXI_SCB] mismatch got 0x1f at 12"
    b = "UVM_ERROR y/a.sv(99) @ 77ns: scb [AXI_SCB] mismatch got 0xab at 400"
    assert normalize_signature(a) == normalize_signature(b)


def test_truncated_to_200():
    result = normalize_signature("word " * 100)
    assert len(result) == 200


def test_signature_id():
    first = signature_id("abc")
    assert len(first) == 8
    assert first == signature_id("abc")
    assert first == hashlib.sha1(b"abc").hexdigest()[:8]
    assert signature_id("abd") != first
