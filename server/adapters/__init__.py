"""适配器门面：`stream_chat` / `chat_once` / `list_models`。

对外只有一个概念：**给一个平台配置和一段对话，流式吐出块**。
上层（HTTP 路由）不需要知道对面是 OpenAI 那一套还是 Anthropic 那一套。

想支持新协议？写一个模块，实现 `build_plan` / `parse_event` / `decode`，
登记进 `ADAPTERS` 就行，上层一行都不用改。
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from ..model import ModelReply
from . import anthropic, openai
from .core import (DEFAULT_MAX_TOKENS, AdapterError, IDLE_MS, StreamPlan, Transport,
                   friendly_http_error, models_endpoint, normalize_base, post_stream)
from .sse import iter_sse_data

ADAPTERS = {"openai": openai, "anthropic": anthropic}


def _adapter(provider: dict):
    return ADAPTERS.get(provider.get("kind") or "openai", openai)


def stream_chat(provider: dict, *, model: str, system: str | None, messages: list[dict],
                temperature=None, max_tokens: int = DEFAULT_MAX_TOKENS,
                timeout: float | None = None, transport: Transport) -> Iterator[dict]:
    """统一的流式对话。

    产出这些块（`dict`，直接能序列化成 NDJSON 推给前端）：
        {"type": "text"|"reasoning"|"usage"|"finish"|"notice"|"limit"|"error"|"done", ...}

    最后一块一定是 `done`，带 `reply`（完整的 ModelReply，含用量）——
    这样上层不用自己把碎块拼回去。
    """
    label = provider.get("label") or provider.get("name") or provider.get("id") or "该平台"
    if not provider.get("apiKey"):
        raise AdapterError(400, f"{label} 还没填 API Key")
    if not model:
        raise AdapterError(400, "这个席位还没选模型")
    normalize_base(provider.get("baseUrl"))  # 早点报错，别等发出去才发现

    adapter = _adapter(provider)
    plan = adapter.build_plan(provider, model=model, system=system, messages=messages,
                              temperature=temperature, max_tokens=max_tokens)
    seconds = (timeout or IDLE_MS) / 1000
    opened, notes = post_stream(transport, plan, provider, timeout=seconds)

    if notes["droppedTemperature"]:
        yield {"type": "notice", "text": "该模型不接受自定义温度，已改用平台默认值"}
    if notes["limitedMaxTokens"]:
        said = notes["limitedMaxTokens"]
        yield {
            "type": "limit",
            "field": "max_tokens",
            "max": said if said > 0 else 0,
            "text": (f"「单次回答最长 token」设成了 {max_tokens}，超出这个模型允许的范围，"
                     f"已按平台说的 {said} 重发一次") if said > 0 else
                    "这个模型不接受指定的「单次回答最长 token」，已改用平台的默认值重发一次",
        }

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict = {}
    finish = ""
    for data in iter_sse_data(opened.chunks):
        if not data:
            continue
        if adapter.is_done(data):
            break
        evt = adapter.decode(data)
        if evt is None:
            continue
        for chunk in adapter.parse_event(evt):
            kind = chunk["type"]
            if kind == "text":
                text_parts.append(chunk["text"])
            elif kind == "reasoning":
                reasoning_parts.append(chunk["text"])
            elif kind == "usage":
                usage.update(chunk["usage"])
            elif kind == "finish":
                finish = chunk["reason"]
            elif kind == "error":
                raise AdapterError(502, f"{label}：{chunk['text']}")
            yield chunk

    reply = ModelReply(
        text="".join(text_parts),
        reasoning="".join(reasoning_parts),
        prompt_tokens=int(usage.get("input") or 0),
        completion_tokens=int(usage.get("output") or 0),
        model=model,
        finish_reason=finish,
    )
    yield {"type": "done", "reply": reply}


def chat_once(provider: dict, *, model: str, system: str | None, messages: list[dict],
              temperature=None, max_tokens: int = DEFAULT_MAX_TOKENS,
              timeout: float | None = None, transport: Transport) -> ModelReply:
    """非流式：把流跑完只要最后那个结果（提炼人格、压缩、提取记忆都用这个）。"""
    reply: ModelReply | None = None
    for chunk in stream_chat(provider, model=model, system=system, messages=messages,
                             temperature=temperature, max_tokens=max_tokens,
                             timeout=timeout, transport=transport):
        if chunk["type"] == "done":
            reply = chunk["reply"]
    if reply is None:
        raise AdapterError(502, "上游没有返回任何结果")
    return reply


def make_caller(provider: dict, *, model: str, temperature=None,
                max_tokens: int = DEFAULT_MAX_TOKENS, transport: Transport):
    """把一个平台变成 `memory` 层要的那种"模型调用"。

    压缩和提取记忆都通过它调模型——所以那一层完全不需要知道 httpx。
    """
    def caller(*, system: str, messages: list[dict]) -> ModelReply:
        return chat_once(provider, model=model, system=system, messages=messages,
                         temperature=temperature, max_tokens=max_tokens, transport=transport)

    return caller


def list_models(provider: dict, *, timeout: float = 20.0, transport: Transport) -> list[dict]:
    """拉这个平台有哪些模型。有些平台不提供 /models，那就抛错让用户手填。"""
    base = normalize_base(provider.get("baseUrl"))
    headers = ({"x-api-key": provider.get("apiKey") or "", "anthropic-version": anthropic.ANTHROPIC_VERSION}
               if provider.get("kind") == "anthropic"
               else {"authorization": f"Bearer {provider.get('apiKey') or ''}"})
    status, body = transport.get_json(url=models_endpoint(provider), headers=headers, timeout=timeout)
    if status != 200:
        raise AdapterError(status, friendly_http_error(status, body, provider)
                           + f"（{base} 可能不提供模型列表，可以在设置里手动填模型名）")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise AdapterError(502, f"{provider.get('name') or base}：模型列表不是合法 JSON（{e}）") from e
    items = data.get("data") if isinstance(data, dict) else data
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if isinstance(it, str):
            out.append({"id": it, "created": 0})
        elif isinstance(it, dict):
            mid = it.get("id") or it.get("name")
            if mid:
                out.append({"id": mid, "created": normalize_time(it.get("created")),
                            "ownedBy": it.get("owned_by") or ""})
    return out


def normalize_time(v) -> int:
    """时间字段归一化：有的平台给秒级时间戳，有的给毫秒，有的给 ISO 字符串。

    归一成毫秒；认不出来就返回 0——**宁可没有，也不瞎猜**。
    """
    if v is None or v == "":
        return 0
    if isinstance(v, (int, float)):
        n = float(v)
        if n > 1e12:
            return int(n)
        if n > 1e9:
            return int(n * 1000)
        return 0
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return 0
