"""token 数量估算。实际用量以平台返回值为准。"""

from __future__ import annotations

import re

# 估算 token：中文 1 字 ≈ 1 token，其余 3.6 字符 ≈ 1。
# 这只是估算——真实用量以平台回报为准。
# 但**估算必须保守**：猜大了会 400，猜小了只是少带几轮。
_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return round(cjk + (len(text) - cjk) / 3.6)
