"""失败签名（verdict.md §8）：把首条错误归一化，让同一个问题在不同 job 中得到同一个签名。"""

from __future__ import annotations

import hashlib
import re

__all__ = ["MAX_SIGNATURE_LEN", "normalize_signature", "signature_id"]

MAX_SIGNATURE_LEN = 200

# 1. UVM 前缀里的文件名、行号、仿真时间；文件与行号可能被关掉
_UVM_PREFIX = re.compile(r"^(UVM_(?:INFO|WARNING|ERROR|FATAL))\s+(?:\S+\(\d+\)\s+)?@\s*[^:]*:\s*")
# 2. 路径只保留文件名（含绝对路径开头的 /）
_PATH = re.compile(r"/?(?:[\w.-]+/)+([\w.-]+)")
# 3. 先十六进制、后十进制：否则 0x1f 会被十进制规则拆开
_HEX = re.compile(r"\b0[xX][0-9a-fA-F]+\b")
# 前面不能是字母数字，避免把 axi4 这类标识符拆掉；允许 100ns 这样后接单位
_DEC = re.compile(r"(?<![\w<])\d+(?:\.\d+)?")


def normalize_signature(line: str) -> str:
    """Normalize a failing log line into a stable signature text."""
    text = _UVM_PREFIX.sub(r"\1 ", line.strip())
    text = _PATH.sub(r"\1", text)
    # 4. 引号内的字符串原样保留，但其中的数字同样替换——与引号外用同一套规则
    text = _HEX.sub("<HEX>", text)
    text = _DEC.sub("<N>", text)
    # 5. 压空白、截断
    return " ".join(text.split())[:MAX_SIGNATURE_LEN]


def signature_id(text: str) -> str:
    """Return the short id of a signature text: first 8 hex digits of its SHA-1."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
