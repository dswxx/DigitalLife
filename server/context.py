"""上下文组装：人格与可选历史摘要 + 最近原文 + 当前问题。

这里只选择和映射消息；摘要由 compression 生成并传入，不在这里调用模型。
普通 AI 按会话筛选历史；数字人沿用自己的跨会话历史。
"""

from dataclasses import dataclass


@dataclass
class Built:
    """协议无关的请求内容；OpenAI 与 Anthropic 适配器各自转换 system 的位置。"""
    system: str | None
    messages: list[dict]


def system_text(persona: str, bare: bool, summary: str) -> str:
    """人格与历史摘要各司其职；普通 AI 省略人格，仍可使用本会话摘要。"""
    parts = [persona] if persona and not bare else []
    if summary:
        parts.append("【此前对话摘要，供继续交流参考】\n" + summary)
    return "\n\n".join(parts)


def build_context(life, *, session_id: str, question: str,
                  persona: str = "", bare: bool = False,
                  history: list[dict] | None = None, summary: str = "") -> Built:
    """【核心】完整组装尚未压缩的问答正文，不估算窗口或静默裁剪历史。

    history 可传入加锁读取的快照；传空列表表示确实没有历史，不会再次读盘。
    bare=True 时省略人格并隔离会话，避免普通 AI 带入其他会话内容。
    """
    system = system_text(persona, bare, summary) or None
    source = life.read_history() if history is None else history
    if bare:
        source = [e for e in source if e.get("sessionId") == session_id]
    entries = ordered_history(source, pending=life.active_rounds.copy())
    # reasoning 只供原文归档和页面展示；正式回答与摘要都只携带 text。
    tail = [{"role": "user" if entry["kind"] == "you" else "assistant",
             "content": entry["text"]} for entry in entries]
    messages = []
    # 相邻同角色消息合并，统一保持 user/assistant 交替，兼顾协议约束。
    # 只处理请求副本，不改变 history.jsonl 中原来的消息边界。
    for message in tail + [{"role": "user", "content": question}]:
        if messages and messages[-1]["role"] == message["role"]:
            messages[-1]["content"] += "\n\n" + message["content"]
        else:
            messages.append(dict(message))
    return Built(system, messages)


def ordered_history(entries: list[dict], *, pending: set | None = None) -> list[dict]:
    """按 roundId 聚合，保持提问出现顺序，并让每轮问题排在回答前。

    文件追加顺序可能是“甲问、乙问、乙答、甲答”，请求应还原为成对问答。
    仅使用有效原文，跳过仍在生成且尚无回答的其他轮次。
    """
    groups = {}
    for entry in entries:
        if entry.get("kind") not in ("you", "self") or not entry.get("text") or entry.get("error"):
            continue
        groups.setdefault(entry["roundId"], []).append(entry)
    result = []
    # 字典保留首次遇见轮次的顺序；pending 由调用方传入活动轮次的副本。
    for rid, group in groups.items():
        if rid in (pending or set()) and not any(e["kind"] == "self" for e in group):
            continue
        result.extend(sorted(group, key=lambda e: e["kind"] != "you"))
    return result
