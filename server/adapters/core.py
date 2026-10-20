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
    """模型请求异常；status 可来自上游 HTTP，也可由本地校验或网络翻译生成。"""

    def __init__(self, status: int, message: str, *, kind: str = ""):
        super().__init__(message)
        self.status = status
        # 业务层依赖结构化分类，不通过翻译后的中文提示猜测是否该压缩。
        self.kind = kind


def is_context_limit(status: int, body: str) -> bool:
    """只接受明确的上下文/输入超限信号；普通 413、输出范围和配额错误均不算。

    HTTP 错误和流内 error 共用此分类。优先检查服务商的 code/type，
    再识别具体文案，不凭 400 状态或单独的 token/length 关键词触发。
    """
    if status not in (400, 413, 422):
        return False
    detail = str(body or "")
    try:
        payload = json.loads(detail)
        error = payload.get("error", payload) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            codes = (error.get("code"), error.get("type"))
            if any(code in {"context_length_exceeded", "context_window_exceeded",
                            "prompt_too_long", "input_too_long", "too_many_input_tokens"}
                   for code in codes if isinstance(code, str)):
                return True
            detail = str(error.get("message") or "")
    except (ValueError, TypeError):
        pass
    if re.search(r"rate.?limit|quota|tokens? per (?:minute|second|day)|配额|速率限制", detail, re.I):
        return False
    return bool(re.search(
        r"context[_ ](?:length|window)[_ ]exceeded|"
        r"(?:maximum|max|exceed\w*|longer than)[^\n]{0,60}context (?:length|window)|"
        r"context (?:length|window)[^\n]{0,80}(?:exceed\w*|too (?:long|large)|limit)|"
        r"(?:prompt|input|input tokens|input length)[^\n]{0,80}(?:too (?:long|large)|exceed\w*|longer than)|"
        r"(?:上下文|输入|提示词)[^\n]{0,30}(?:超出|超过|过长|超限)", detail, re.I))


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
    """依据异常文本补充可操作的提示，同时保留有限长度的原始原因。"""
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
    """提取平台错误正文，并按状态和具体字段区分认证、额度与上下文问题。"""
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

    # 同为 400 可能是输出额度或输入超窗，不能统一归因于历史太长。
    hint400 = "请求被拒"
    if is_context_limit(status, body):
        hint400 = "请求超过模型上下文窗口"
    elif re.search(r"max_?tokens|output tokens", detail, re.I):
        hint400 = ("这个模型的输出上限不接受这个值——看下面平台给的范围，"
                   "请在 settings.json 中调小 maxTokens")

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
    """仅在错误明确涉及输出 token 时提取已识别格式的上限，无法识别返回 0。

    不从任意数字推测模型规格；解析结果由额度兼容逻辑使用。
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
    """已收到 HTTP 状态的响应；成功时惰性读取 chunks，失败时保留 error_text。"""
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
    """协议层生成的请求数据，保持传输实现与消息解析相互独立。"""

    url: str
    headers: dict
    body: dict
    kind: str


def post_stream(transport: Transport, plan: StreamPlan, provider: dict, *, timeout: float) -> tuple[Opened, dict]:
    """仅对明确拒绝 max_tokens 的 400/422 做有限次数的参数兼容重试。

    候选依次为可解析的较小上限、原值一半、去掉该参数；其他错误直接上抛。
    此处只处理尚未开始消费的失败响应，不会因生成很慢而重新发起已在作答的调用。
    """
    notes = {"limitedMaxTokens": 0}
    opened = _send(transport, plan, timeout)
    if opened.ok:
        return opened, notes

    # 总上下文错误经常也提到 max_tokens，必须先交给压缩层，不能当输出参数重试。
    if is_context_limit(opened.status, opened.error_text):
        raise AdapterError(opened.status, friendly_http_error(opened.status, opened.error_text, provider),
                           kind="context_limit")

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
            if is_context_limit(second.status, second.error_text):
                raise AdapterError(second.status, friendly_http_error(second.status, second.error_text, provider),
                                   kind="context_limit")
        raise AdapterError(second.status, friendly_http_error(second.status, second.error_text, provider))

    raise AdapterError(opened.status, friendly_http_error(opened.status, error_text, provider))


def _send(transport: Transport, plan: StreamPlan, timeout: float) -> Opened:
    """保持 AdapterError 的状态与提示；其他传输异常统一翻译成网络错误。"""
    try:
        return transport.open_stream(url=plan.url, headers=plan.headers, body=plan.body, timeout=timeout)
    except AdapterError:
        raise
    except Exception as e:  # noqa: BLE001 —— 网络层的异常五花八门，统一翻成人话
        raise AdapterError(502, friendly_network_error(e, {"label": "平台"})) from e
