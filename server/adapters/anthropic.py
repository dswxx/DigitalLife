"""Anthropic Messages 协议的请求组装与流式事件解析。"""

from __future__ import annotations

import json

from .core import DEFAULT_MAX_TOKENS, StreamPlan, endpoint

ANTHROPIC_VERSION = "2023-06-01"


def build_plan(provider: dict, *, model: str, system: str | None, messages: list[dict],
               max_tokens: int = DEFAULT_MAX_TOKENS) -> StreamPlan:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": True,
        **({"system": system} if system else {}),
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
    }
    return StreamPlan(
        url=endpoint(provider),
        headers={
            "content-type": "application/json",
            "x-api-key": provider.get("apiKey") or "",
            "anthropic-version": ANTHROPIC_VERSION,
        },
        body=body,
        kind="anthropic",
    )


def parse_event(evt: dict) -> list[dict]:
    """一条 SSE 事件 → 若干个块。

    Anthropic 的流式事件比 OpenAI 那种"每块一个 delta"复杂，逐个类型处理：
    漏掉 `message_delta` 就看不到结束原因（推理被截断时就成了"莫名其妙的空回复"），
    漏掉 `message_start` 就丢输入用量。
    """
    out: list[dict] = []
    if not isinstance(evt, dict):
        return out
    kind = evt.get("type")

    if kind == "content_block_delta":
        d = evt.get("delta") or {}
        if d.get("type") == "thinking_delta" and d.get("thinking"):
            out.append({"type": "reasoning", "text": d["thinking"]})
        elif d.get("type") == "text_delta" and d.get("text"):
            out.append({"type": "text", "text": d["text"]})
    elif kind == "message_delta":
        usage = evt.get("usage") or {}
        if usage.get("output_tokens") is not None:
            out.append({"type": "usage", "usage": {"output": int(usage.get("output_tokens") or 0)}})
        stop = (evt.get("delta") or {}).get("stop_reason")
        if stop:
            # max_tokens 就是被输出上限截断了——和 OpenAI 的 length 是同一件事
            out.append({"type": "finish", "reason": "length" if stop == "max_tokens" else stop})
    elif kind == "message_start":
        usage = (evt.get("message") or {}).get("usage") or {}
        if usage.get("input_tokens") is not None:
            out.append({"type": "usage", "usage": {"input": int(usage.get("input_tokens") or 0)}})
    elif kind == "error":
        out.append({"type": "error", "text": (evt.get("error") or {}).get("message") or "流式返回错误"})
    return out


def is_done(data: str) -> bool:
    """Anthropic 用 `message_stop` 事件表示结束，没有 `[DONE]`。"""
    return False


def decode(data: str) -> dict | None:
    try:
        got = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None
    return got if isinstance(got, dict) else None
