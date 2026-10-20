"""token 数量估算。实际用量以平台返回值为准。"""

from __future__ import annotations

import re

# 估算 token：中文 1 字 ≈ 1 token，其余 3.6 字符 ≈ 1。
# 这只是估算——真实用量以平台回报为准。
# 仅在平台缺少输出用量时用于展示，不参与上下文选择或压缩触发。
_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    """按中英文字符比例粗估 token，不依赖某一家模型的 tokenizer。"""
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return round(cjk + (len(text) - cjk) / 3.6)
