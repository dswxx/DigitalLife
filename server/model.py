"""模型调用的公共契约。

适配器（OpenAI 兼容 / Anthropic）实现它，压缩和对话使用它。
单独一个文件是为了让依赖方向干净：

    adapters  →  model（只有类型）
    memory    →  model
    main      →  adapters

`memory` 只认这个契约，不认 httpx——所以压缩的全部逻辑（什么时候压、
压哪一段、压完怎么推水位线）都能用假模型离线验完，
不必等到网络那一层写好。

`prompt_tokens` / `completion_tokens` 不是装饰：压缩花的 token 要进用量账，
"否则越老的生命越贵而你看不见"（设计文档里的话）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ModelError(RuntimeError):
    """一次调用失败了。带上是哪一家、哪个模型，以及原始响应片段。"""

    def __init__(self, message: str, *, provider: str = "", model: str = "", status: int = 0, raw: str = ""):
        super().__init__(message)
        self.provider = provider
        self.model = model
        self.status = status
        self.raw = raw


@dataclass
class ModelReply:
    """一次调用的结果。**只有 text 是必需的**，用量能拿到就拿。"""

    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    model: str = ""
    finish_reason: str = ""
    # 推理模型的思考过程（有些平台单独给）。不发给用户，但可以给他自己看。
    reasoning: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def usage(self) -> dict:
        return {"promptTokens": self.prompt_tokens, "completionTokens": self.completion_tokens}

    def __str__(self) -> str:  # 方便和"直接返回字符串"的假模型互换
        return self.text


def as_reply(value) -> ModelReply:
    """把调用方的返回值归一成 ModelReply。

    允许调用者直接返回字符串——写测试、写小工具时不该被迫造一个 dataclass。
    """
    if isinstance(value, ModelReply):
        return value
    if isinstance(value, str):
        return ModelReply(text=value)
    text = getattr(value, "text", None)
    if text is None:
        raise TypeError(f"模型调用返回了看不懂的东西：{type(value).__name__}")
    return ModelReply(
        text=text,
        prompt_tokens=int(getattr(value, "prompt_tokens", 0) or 0),
        completion_tokens=int(getattr(value, "completion_tokens", 0) or 0),
        model=str(getattr(value, "model", "") or ""),
        finish_reason=str(getattr(value, "finish_reason", "") or ""),
        reasoning=str(getattr(value, "reasoning", "") or ""),
    )
