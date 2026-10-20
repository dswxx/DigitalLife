"""OpenAI 兼容协议的请求组装与流式事件解析。"""

from __future__ import annotations

import json

from .core import DEFAULT_MAX_TOKENS, StreamPlan, endpoint


def build_plan(provider: dict, *, model: str, system: str | None, messages: list[dict],
               max_tokens: int = DEFAULT_MAX_TOKENS) -> StreamPlan:
    """OpenAI 兼容协议把人格作为 messages 首条 system；无人格时不添加该消息。"""
    body = {
        "model": model,
        "stream": True,
        "max_tokens": max_tokens,
        "messages": ([{"role": "system", "content": system}] if system else []) + list(messages),
    }
    return StreamPlan(
        url=endpoint(provider),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {provider.get('apiKey') or ''}",
        },
        body=body,
        kind="openai",
    )


def parse_event(evt: dict) -> list[dict]:
    """将 SSE 事件映射为 text、reasoning、usage、finish 或 error。兼容两种 reasoning 字段，保留结束原因以识别输出截断。"""
    out: list[dict] = []
    if not isinstance(evt, dict):
        return out
    err = evt.get("error")
    if err:
        msg = err.get("message") if isinstance(err, dict) else str(err)
        return [{"type": "error", "text": msg or "流式返回错误"}]

    choices = evt.get("choices") or []
    if choices:
        choice = choices[0] or {}
        if choice.get("finish_reason"):
            out.append({"type": "finish", "reason": choice["finish_reason"]})
        delta = choice.get("delta") or choice.get("message") or {}
        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if reasoning:
            out.append({"type": "reasoning", "text": reasoning})
        if delta.get("content"):
            out.append({"type": "text", "text": delta["content"]})
    # 用量可能出现在独立的末尾事件中，此时 choices 可以是空数组。
    usage = evt.get("usage")
    if usage:
        out.append({"type": "usage", "usage": {
            "input": int(usage.get("prompt_tokens") or 0),
            "output": int(usage.get("completion_tokens") or 0),
        }})
    return out


def is_done(data: str) -> bool:
    """`data: [DONE]` 是这一套协议的结束标记（Anthropic 没有这个）。"""
    return data.strip() == "[DONE]"


def decode(data: str) -> dict | None:
    """忽略非对象或无法解析的载荷；网络块的字符和行边界已经由 SSE 层处理。"""
    try:
        got = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None
    return got if isinstance(got, dict) else None
