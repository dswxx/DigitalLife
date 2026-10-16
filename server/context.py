"""组装一次请求的 system / messages。

这是**框架版**：把四档记忆递减和预算裁剪的骨架搭起来，
记忆内容的精细编排（摘要怎么写、召回怎么挑、压缩提示词怎么设计）留到后面迭代。

一次请求里到底放什么（参考材料里的核心一问）：

    system  = 人格 + 时间感 + 长期记忆目录 + 更早会话的一行摘要
    messages= 本次会话的近期原文 + 本轮问题

**硬约束**：这里只做**确定性裁剪**，绝不调用模型做压缩。
压缩发生在两轮之间的空隙里（见设计文档）。请求路径一旦要调模型，
响应就会多几秒、还可能失败——"用户无感"就没了。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from .life import KIND_HEARD, KIND_SELF, KIND_SUMMARY, KIND_YOU, Life

# 估算 token：中文 1 字 ≈ 1 token，其余 3.6 字符 ≈ 1。
# 和老项目同一个口径（换栈时这条口径要一起带走，否则预算行为和以前不一样了）。
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
    """"3 小时前""昨天""上周三"——记忆递减的那个"感觉"。"""
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


@dataclass
class Built:
    """组装结果 + 账。账要能看见，否则越老的生命越贵而用户不知道。"""

    system: str | None
    messages: list[dict]
    estimated_input: int
    session_entries: int  # 本次会话原文带了几条
    older_summaries: int  # 带了几条更早会话的摘要
    memory_lines: int  # 目录带了几行
    dropped: int  # 因为预算丢掉了几条原文
    budget: int


def build_context(
    life: Life,
    *,
    session_id: str,
    question: str,
    window: int,
    output_reserve: int = 8192,
    heard: list[dict] | None = None,
    persona: str | None = None,
    bare: bool = False,
    safety_ratio: float = 0.1,
) -> Built:
    """把一个生命"此刻能想起的东西"组装成一次请求。

    window        —— 这个模型的上下文窗口（优先用平台报的，见 resolve_window）
    output_reserve—— 给输出预留的额度（推理模型的思考也算在这里）
    heard         —— 会议模式下：本轮此前已发言的人说了什么
    persona       —— 人格模板的 system；None 表示这个模板没有写人格
    bare          —— **纯基线**：整个 system 字段都不发，时间感和记忆也不注入

    bare 这条是从老项目继承的产品承诺：「裸模型」要能看出"模型本身"的原样回答。
    所以它不是"发个空的 system"，而是**压根不构造**——连时间感和记忆目录都不加。
    代价是这类生命不积累记忆（它是用来对打的探针，不是用来陪伴的）。
    """
    if bare:
        return Built(
            system=None,
            messages=[{"role": "user", "content": question}],
            estimated_input=estimate_tokens(question),
            session_entries=0,
            older_summaries=0,
            memory_lines=0,
            dropped=0,
            budget=window,
        )

    safety = max(1024, round(window * safety_ratio))
    persona_tokens = estimate_tokens(persona or "")

    # ── system：时间感 + 长期记忆目录 + 更早会话的一行 ──
    now = int(time.time() * 1000)
    sys_parts: list[str] = []
    if persona:
        sys_parts.append(persona)
    sys_parts.append(
        f"【现在】{fmt_day(now)} {fmt_clock(now)}\n"
        f"【你上次和他说话】{human_ago(_last_talked(life, session_id))}"
    )

    older = life.summaries(exclude_session=session_id)
    if older:
        lines = [f"· {fmt_day(s['t'])} {fmt_clock(s['t'])}：{s['text']}" for s in older[:20]]
        sys_parts.append("【更早的对话，你只记得这些】\n" + "\n".join(lines))

    index = life.index_text().strip()
    memory_lines = len([x for x in index.splitlines() if x.strip()])
    if index:
        sys_parts.append("【你记得的事】\n" + index)

    system = "\n\n".join(sys_parts)

    # ── messages：本次会话的近期原文，从最新往前装到装不下 ──
    budget = max(0, window - output_reserve - safety - estimate_tokens(system))
    fixed: list[dict] = []
    if heard:
        fixed.append(
            {
                "role": "user",
                "content": "【本轮此前已发言的人】\n"
                + "\n\n".join(f"《{h['who']}》：{h['text']}" for h in heard),
            }
        )
    fixed.append({"role": "user", "content": question})
    spent = sum(estimate_tokens(m["content"]) for m in fixed)

    entries = [e for e in life.read_session(session_id) if e.get("kind") != KIND_SUMMARY]
    tail: list[dict] = []
    kept = 0
    for e in reversed(entries):
        role = "user" if e.get("kind") in (KIND_YOU, KIND_HEARD) else "assistant"
        text = e.get("text", "")
        cost = estimate_tokens(text)
        if spent + cost > budget:
            break
        spent += cost
        prefix = f"（{e['who']}）" if e.get("kind") == KIND_HEARD else ""
        tail.insert(0, {"role": role, "content": prefix + text})
        kept += 1

    messages = _merge_consecutive(tail + fixed)

    return Built(
        system=system or None,
        messages=messages,
        estimated_input=spent + estimate_tokens(system),
        session_entries=kept,
        older_summaries=len(older[:20]),
        memory_lines=memory_lines,
        dropped=len(entries) - kept,
        budget=budget,
    )


def _last_talked(life: Life, exclude_session: str) -> int:
    """上一次说话是什么时候（不含当前这次会话）。"""
    last = 0
    for sid in life.sessions():
        if sid == exclude_session:
            continue
        for e in life.read_session(sid):
            last = max(last, e.get("t", 0))
    return last


def _merge_consecutive(messages: list[dict]) -> list[dict]:
    """合并相邻同角色消息。**Anthropic 要求 user/assistant 交替**，不合并会被拒。"""
    out: list[dict] = []
    for m in messages:
        if not m["content"].strip():
            continue
        if out and out[-1]["role"] == m["role"]:
            out[-1]["content"] += "\n\n" + m["content"]
        else:
            out.append(dict(m))
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out
