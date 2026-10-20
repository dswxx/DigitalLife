"""将人格、历史原文和当前问题组装成一次模型请求。"""

from dataclasses import dataclass

from .settings import history_budget
from .text import estimate_tokens


@dataclass
class Built:
    system: str | None
    messages: list[dict]


def build_context(life, *, session_id: str, question: str, window: int,
                  output_reserve: int = 8192, persona: str = "", bare: bool = False,
                  history: list[dict] | None = None) -> Built:
    system = None if bare else persona or None
    source = life.read_history() if history is None else history
    if bare:
        source = [e for e in source if e.get("sessionId") == session_id]
    entries = ordered_history(source, pending=life.active_rounds.copy())
    budget = history_budget(window, output_reserve) - estimate_tokens(system or "")
    spent = estimate_tokens(question) + 8
    if spent > budget:
        raise ValueError("人格和当前问题超过模型窗口，请缩短内容或选择更大窗口的模型")
    tail = []
    for entry in reversed(entries):
        cost = estimate_tokens(entry["text"]) + 8
        if spent + cost > budget:
            break
        spent += cost
        tail.append({"role": "user" if entry["kind"] == "you" else "assistant",
                     "content": entry["text"]})
    tail.reverse()
    while tail and tail[0]["role"] != "user":
        tail.pop(0)
    messages = []
    for message in tail + [{"role": "user", "content": question}]:
        if messages and messages[-1]["role"] == message["role"]:
            messages[-1]["content"] += "\n\n" + message["content"]
        else:
            messages.append(dict(message))
    return Built(system, messages)


def ordered_history(entries: list[dict], *, pending: set | None = None) -> list[dict]:
    """按轮次配对，防止同一实体在不同会话并发作答时交错。"""
    groups = {}
    for entry in entries:
        if entry.get("kind") not in ("you", "self") or not entry.get("text") or entry.get("error"):
            continue
        groups.setdefault(entry["roundId"], []).append(entry)
    result = []
    for rid, group in groups.items():
        if rid in (pending or set()) and not any(e["kind"] == "self" for e in group):
            continue
        result.extend(sorted(group, key=lambda e: e["kind"] != "you"))
    return result
