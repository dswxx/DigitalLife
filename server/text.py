"""文本与时间的小工具：token 估算、时间格式化、"多久以前"。

单独一个文件是为了**打断一个循环依赖**：`context` 要调 `memory` 做召回，
而 `memory` 要估算 token、要格式化时间戳。把这两样抽出来，
依赖方向就是干净的：

    text  ←  life?  (不，life 不需要)
    text  ←  memory, context
    life  ←  memory, context
    model ←  memory
    context → memory → life → store

口径和参数都**必须和老项目一致**（换栈时这条口径要一起带走，
否则同样的预算在新旧两端行为不一样）。
"""

from __future__ import annotations

import re
import time

# 估算 token：中文 1 字 ≈ 1 token，其余 3.6 字符 ≈ 1。
# 这只是估算——真实用量以平台回报为准（`model.ModelReply.usage`）。
# 但**估算必须保守**：猜大了会 400，猜小了只是少带几轮。
_CJK = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = len(_CJK.findall(text))
    return round(cjk + (len(text) - cjk) / 3.6)


def fmt_clock(ms: int) -> str:
    return time.strftime("%H:%M", time.localtime(ms / 1000))


def fmt_day(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000))


def human_ago(ms: int) -> str:
    """"3 小时前""昨天""上周三"——记忆递减的那个"感觉"。

    给模型看"上次说话是什么时候"，它才会像一个真的过了几天的人那样说话，
    而不是把三天前的事当成刚刚发生的。
    """
    if not ms:
        return "不记得了"
    diff = time.time() - ms / 1000
    if diff < 90:
        return "刚刚"
    if diff < 3600:
        return f"{int(diff // 60)} 分钟前"
    if diff < 86400:
        return f"{int(diff // 3600)} 小时前"
    if diff < 172800:
        return "昨天"
    if diff < 604800:
        return f"{int(diff // 86400)} 天前"
    return time.strftime("%m月%d日", time.localtime(ms / 1000))
