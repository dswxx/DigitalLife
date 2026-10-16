"""OpenAI 兼容协议（DeepSeek / Kimi / OpenAI / 通义 / Ollama …）。

绝大多数平台都是这一套，所以这个适配器是主力。
请求体的组装和事件解析都是**纯函数**——网络那一层在 `core.post_stream`。
"""

from __future__ import annotations

import json

from .core import DEFAULT_MAX_TOKENS, StreamPlan, endpoint, temperature_field


def build_plan(provider: dict, *, model: str, system: str | None, messages: list[dict],
               temperature=None, max_tokens: int = DEFAULT_MAX_TOKENS) -> StreamPlan:
    """组装请求。**system 单独放在 messages 最前面**（这一套协议就是这么表达人格的）。"""
    body = {
        "model": model,
        "stream": True,
        **temperature_field(temperature),
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
    """一条 SSE 事件 → 若干个块（text / reasoning / usage / finish / error）。

    这里有两处以前出过事的细节：

    1. **`finish_reason` 必须看。** 推理模型经常"思考把额度吃光、正文一个字都没轮到"，
       这时 `finish_reason` 是 `length`。不看它，用户只会看到一个空回复，
       完全猜不出为什么。（老项目那个"卡在思考中"就是这么来的。）

    2. **思考过程有两种字段名**：`reasoning_content`（DeepSeek/Kimi 系）
       和 `reasoning`（OpenRouter 等），两个都要认。
    """
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
    """解析一条载荷。**解析失败就返回 None 并跳过**——上游偶尔会插心跳或半截数据，
    为这个中断整段回答不值得。"""
    try:
        got = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        return None
    return got if isinstance(got, dict) else None
