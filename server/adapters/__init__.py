"""协议适配入口：将两种上游协议统一成业务层可消费的事件。

build_plan 负责请求形状，SSE 层负责网络分块，parse_event 负责字段差异。
这里不认识会话或实体，也不保存聊天记录。
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from . import anthropic, openai
from .core import (DEFAULT_MAX_TOKENS, AdapterError, IDLE_MS, Transport,
                   friendly_http_error, is_context_limit, models_endpoint, normalize_base, post_stream)
from .sse import iter_sse_data

ADAPTERS = {"openai": openai, "anthropic": anthropic}


def _adapter(provider: dict):
    return ADAPTERS.get(provider.get("kind") or "openai", openai)


def stream_chat(provider: dict, *, model: str, system: str | None, messages: list[dict],
                max_tokens: int = DEFAULT_MAX_TOKENS,
                timeout: float | None = None, transport: Transport) -> Iterator[dict]:
    """【核心】一次模型调用：校验配置 → 组装协议 → 请求 → SSE → 统一事件。

    done 仅表示适配器读流结束；业务保存完成由 chat 发出的 seat_done 表示。
    timeout 以秒传给传输层，默认环境变量 IDLE_MS 在此仅转换一次单位。
    """
    label = provider.get("label") or provider.get("name") or provider.get("id") or "该平台"
    if not provider.get("apiKey"):
        raise AdapterError(400, f"{label} 还没填 API Key")
    if not model:
        raise AdapterError(400, "这位还没选模型")
    normalize_base(provider.get("baseUrl"))  # 早点报错，别等发出去才发现

    adapter = _adapter(provider)
    plan = adapter.build_plan(provider, model=model, system=system, messages=messages,
                              max_tokens=max_tokens)
    seconds = timeout if timeout is not None else IDLE_MS / 1000
    opened, notes = post_stream(transport, plan, provider, timeout=seconds)

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

    # 模型报错可能发生在 HTTP 200 的流内部，仍应转为业务可处理的异常。
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
            if kind == "error":
                # SSE 的 HTTP 状态可能仍是 200；保留原始 code/type 参与分类。
                kind = "context_limit" if is_context_limit(400, json.dumps(evt, ensure_ascii=False)) else ""
                raise AdapterError(502, f"{label}：{chunk['text']}", kind=kind)
            yield chunk

    yield {"type": "done"}


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
    # 不同兼容平台可能给 data 数组、直接数组或字符串模型名，在入口统一为对象列表。
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
