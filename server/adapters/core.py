"""协议共用的 URL 校验、错误翻译、参数重试与传输契约。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

# 上游连续无输出的超时，单位为毫秒。
IDLE_MS = int(os.environ.get("DL_IDLE_MS") or 90_000)

DEFAULT_MAX_TOKENS = 4096


class AdapterError(RuntimeError):
    """一次上游调用失败。`status` 是我们自己给的语义码（不是平台的原文）。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ─────────────────────────── URL ───────────────────────────


def normalize_base(base_url: str) -> str:
    """规范化 Base URL：缺路径补 `/v1`，去掉尾斜杠。

    用户填 `https://api.deepseek.com` 和 `https://api.deepseek.com/v1` 都该能用——
    要求用户记住这件事是没道理的。
    """
    raw = str(base_url or "").strip().rstrip("/")
    if not raw:
        raise AdapterError(400, "这个平台还没填 Base URL")
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise AdapterError(400, f"Base URL 格式不对：{raw}")
    path = parts.path
    if path in ("", "/"):
        path = "/v1"
    return urlunsplit((parts.scheme, parts.netloc, path.rstrip("/"), "", ""))


def endpoint(provider: dict) -> str:
    base = normalize_base(provider.get("baseUrl"))
    if provider.get("kind") == "anthropic":
        return f"{base}/messages" if base.endswith("/v1") else f"{base}/v1/messages"
    return f"{base}/chat/completions"


def models_endpoint(provider: dict) -> str:
    base = normalize_base(provider.get("baseUrl"))
    return f"{base}/models"


# ─────────────────────────── 错误文案 ───────────────────────────


def _label(provider: dict) -> str:
    return provider.get("label") or provider.get("name") or provider.get("id") or "该平台"


def friendly_network_error(err: BaseException, provider: dict) -> str:
    text = f"{err} {getattr(err, '__cause__', '') or ''}"
    if re.search(r"ENOTFOUND|EAI_AGAIN|getaddrinfo|Name or service not known|nodename", text, re.I):
        return f"{_label(provider)}：域名解析失败，检查网络或 Base URL"
    if re.search(r"ECONNREFUSED|Connection refused|拒绝", text, re.I):
        return f"{_label(provider)}：连接被拒绝，检查 Base URL 是否正确"
    if re.search(r"CERT|TLS|SSL|certificate", text, re.I):
        return f"{_label(provider)}：TLS 握手中断，可能需要代理"
    if re.search(r"timeout|timed out|aborted|Timeout", text, re.I):
        return f"{_label(provider)}：请求超时"
    if re.search(r"proxy|Proxy", text, re.I):
        return f"{_label(provider)}：代理有问题"
    return f"{_label(provider)}：网络请求失败（{text.strip()[:200]}）"


def friendly_http_error(status: int, body: str, provider: dict) -> str:
    detail = ""
    try:
        j = json.loads(body)
        if isinstance(j, dict):
            err = j.get("error")
            if isinstance(err, dict):
                detail = err.get("message") or err.get("type") or ""
            else:
                detail = j.get("message") or (err if isinstance(err, str) else "") or ""
    except (json.JSONDecodeError, TypeError):
        detail = str(body or "")[:300]

    # 400 的提示**必须看着报错内容分派**（见模块开头的说明）
    hint400 = "请求被拒"
    if re.search(r"max_?tokens|output tokens", detail, re.I):
        hint400 = ("这个模型的输出上限不接受这个值——看下面平台给的范围，"
                   "请在 settings.json 中调小 maxTokens")
    elif re.search(r"context|too long|length|token", detail, re.I):
        hint400 = "请求超过模型上下文窗口，请缩短问题或选择更大窗口的模型"

    hints = {
        401: "API Key 无效或已过期",
        402: "账户余额不足",
        403: "权限不足，或该 Key 无权访问此模型",
        404: "模型名或 Base URL 不对",
        413: "请求体过大，请缩短问题或选择更大窗口的模型",
        429: "触发限流或配额用尽，稍后再试",
        500: "平台内部错误",
        503: "平台暂时不可用（常见于高峰期）",
    }
    hint = hint400 if status == 400 else hints.get(status, "")
    return f"{_label(provider)} {status}{f'（{hint}）' if hint else ''}{f'：{detail}' if detail else ''}"


def idle_message(provider: dict, seconds: float) -> str:
    return (f"{_label(provider)}：已经 {round(seconds)} 秒没有收到任何输出了"
            f"——连接还开着，但平台不再发数据")


def parse_max_tokens_limit(text: str) -> int:
    """平台说「max_tokens 超范围」时，把它说的上限读出来。

    各家的输出上限差得很远（有的 4k、有的 384k），没有目录可查，
    **唯一的权威就是平台自己的报错**。所以听见了就照它说的改，
    并把学到的值交回上层存下来——下次就不用再白挨一个 400。
    """
    s = str(text or "")
    if not re.search(r"max_?tokens|output tokens", s, re.I):
        return 0
    m = re.search(r"\[\s*\d+\s*,\s*(\d+)\s*\]", s)
    if m:
        return int(m.group(1))
    m = re.search(r"max_?tokens:?\s*\d+\s*>\s*(\d+)", s, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"maximum[^0-9]{0,40}?(?:output tokens|max_?tokens)[^0-9]{0,20}?(\d{3,})", s, re.I)
    if m:
        return int(m.group(1))
    return 0


@dataclass
class Opened:
    status: int
    chunks: Iterator[bytes] = field(default_factory=lambda: iter(()))
    error_text: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Transport(Protocol):
    """真实实现用 httpx；测试里换成一个按剧本返回的假传输。"""

    def open_stream(self, *, url: str, headers: dict, body: dict, timeout: float) -> Opened:
        ...

    def get_json(self, *, url: str, headers: dict, timeout: float) -> tuple[int, str]:
        """GET 一个 JSON 接口，返回 `(状态码, 正文)`。"""
        ...


@dataclass
class StreamPlan:
    """组装好的请求 + 该怎么解析回话。适配器给出来的就是这个。"""

    url: str
    headers: dict
    body: dict
    kind: str


def post_stream(transport: Transport, plan: StreamPlan, provider: dict, *, timeout: float) -> tuple[Opened, dict]:
    """发送流式请求；平台拒绝输出额度时按其限制重试。"""
    notes = {"limitedMaxTokens": 0}
    opened = _send(transport, plan, timeout)
    if opened.ok:
        return opened, notes

    error_text = opened.error_text
    retryable = opened.status in (400, 422)

    if retryable and "max_tokens" in plan.body and re.search(r"max_?tokens|output tokens", error_text, re.I):
        current = plan.body["max_tokens"]
        said = parse_max_tokens_limit(error_text)
        candidates: list[int | None] = []
        if 0 < said < current:
            candidates.append(max(1, said))
        halved = current // 2
        if halved >= 1 and halved != said:
            candidates.append(halved)
        candidates.append(None)  # 最后退到"干脆不发这个字段"，让平台用自己的默认值

        for candidate in candidates:
            body = dict(plan.body)
            if candidate is None:
                body.pop("max_tokens", None)
            else:
                body["max_tokens"] = candidate
            second = _send(transport, StreamPlan(plan.url, plan.headers, body, plan.kind), timeout)
            if second.ok:
                notes["limitedMaxTokens"] = said if said > 0 else (-1 if candidate is None else candidate)
                return second, notes
        raise AdapterError(second.status, friendly_http_error(second.status, second.error_text, provider))

    raise AdapterError(opened.status, friendly_http_error(opened.status, error_text, provider))


def _send(transport: Transport, plan: StreamPlan, timeout: float) -> Opened:
    try:
        return transport.open_stream(url=plan.url, headers=plan.headers, body=plan.body, timeout=timeout)
    except AdapterError:
        raise
    except Exception as e:  # noqa: BLE001 —— 网络层的异常五花八门，统一翻成人话
        raise AdapterError(502, friendly_network_error(e, {"label": "平台"})) from e
