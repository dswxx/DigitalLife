"""适配器的公共部分：错误文案、URL 组装、重试策略、传输层接口。

这些行为几乎全部是**老项目踩过坑之后写下来的**，换栈时必须一起带过来
（"技术栈会换，产品特性不能消失"）：

  · 平台报错要翻成人话，而且 **400 必须看着报错内容分派**——
    以前不管什么 400 都说"上下文撑爆了"，结果 max_tokens 填超范围时
    把人往"调小记忆轮数"上引，方向完全错了
  · **模型不接受自定义温度** → 摘掉 temperature 重试，并告诉用户
  · **max_tokens 超出该模型范围** → 按平台自己说的上限改小重试，
    并把学到的值交回上层存下来（下次不用再白挨一个 400）
  · 连接开着却长时间一个字节都不来 → 当作卡住，明确失败
    （否则界面上就是"永远在思考"，用户只能猜）

传输层是**注入**的（`Transport`），所以上面这些逻辑全部可以离线测：
假传输按剧本返回 400 → 重发 → 200，断言重试后的请求体长什么样。
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

# 连接开着却一个字节都不来，等多久算卡住。
# 90 秒是老的默认值；测试里用环境变量调小，不然一个测试要等一分半。
IDLE_MS = int(os.environ.get("RT_IDLE_MS") or 90_000)

DEFAULT_MAX_TOKENS = 4096


class AdapterError(RuntimeError):
    """一次上游调用失败。`status` 是我们自己给的语义码（不是平台的原文）。"""

    def __init__(self, status: int, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


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
                   "去「高级设置」把「单次回答最长 token」改小")
    elif re.search(r"context|too long|length|token", detail, re.I):
        hint400 = "这个席位把上下文撑爆了——去「高级设置」调小记忆轮数，或减少席位"

    hints = {
        401: "API Key 无效或已过期",
        402: "账户余额不足",
        403: "权限不足，或该 Key 无权访问此模型",
        404: "模型名或 Base URL 不对",
        413: "请求体太大，多半是上下文超了——调小记忆轮数或减少席位",
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


def temperature_field(t, cap=None) -> dict:
    """组装 temperature 字段。

    **不传 = 不发这个字段**，交给平台用默认值。有些模型只接受固定温度
    （Kimi k2、OpenAI o 系列、部分推理模型），传任何别的值都会被 400 拒绝。
    """
    if t is None:
        return {}
    try:
        n = float(t)
    except (TypeError, ValueError):
        return {}
    return {"temperature": n if cap is None else min(n, cap)}


# ─────────────────────────── 传输层 ───────────────────────────


@dataclass
class Opened:
    """一次上游响应的开口。"""

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
    """发流式请求，并兜住两类"能自愈"的报错。

    返回 `(响应, 说明)`，说明里有 `droppedTemperature` / `limitedMaxTokens`，
    交给上层**明确告诉用户**——不静默换掉他的设置。
    """
    notes = {"droppedTemperature": False, "limitedMaxTokens": 0}
    opened = _send(transport, plan, timeout)
    if opened.ok:
        return opened, notes

    error_text = opened.error_text
    retryable = opened.status in (400, 422)

    # 1) 温度不被接受 → 摘掉重试
    if retryable and "temperature" in plan.body and re.search(r"temperature", error_text, re.I):
        retry = StreamPlan(plan.url, plan.headers, {k: v for k, v in plan.body.items() if k != "temperature"}, plan.kind)
        second = _send(transport, retry, timeout)
        if second.ok:
            notes["droppedTemperature"] = True
            return second, notes
        raise AdapterError(second.status, friendly_http_error(second.status, second.error_text, provider))

    # 2) 输出上限不被接受 → 按平台说的改小重试
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
