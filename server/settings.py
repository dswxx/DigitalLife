"""设置与预算：**机器管的那一半**。

设计文档里那句话是这一层的职责：

> 内容归模型，预算归机器。
> 如果由模型决定"这次要看多少历史"，成本就不可预测了——而这是你要花钱的地方。

所以这里管三件事，全部是确定性的、可单独测的：

  1. **密钥不出本机**：读出去的永远是掩码（`sk-abc…1234`），写进来的才落盘
  2. **上下文窗口从哪来**：平台报的 > 你手填的 > 保守猜的 128k
     （宁可保守：猜大了直接 400，猜小了只是少带几轮）
  3. **输出上限**：平台学的上限、档位上限、窗口本身，三者取最小

学到的东西要**记住**：平台在 400 里说了自己的上限，下次就不用再白挨一次。
"""

from __future__ import annotations

from dataclasses import dataclass

from . import presets, store

# 单次回答最长 token 的档位上限。老项目定这个值是因为被平台 400 打过：
# DeepSeek 说 "the valid range of max_tokens is [1, 393216]"，而有的模型只到 8192。
MAX_OUTPUT_CEILING = 32768

# 平台没报、你也没填时按这个估（老项目的默认值）
DEFAULT_WINDOW = presets.DEFAULT_CONTEXT_WINDOW


def mask_key(key: str) -> str:
    """密钥掩码。**接口只回掩码**——截图、录屏、贴给别人看都不会漏。"""
    k = str(key or "")
    if not k:
        return ""
    if len(k) <= 8:
        return "•" * len(k)
    return f"{k[:4]}…{k[-4:]}"


def is_masked(value: str) -> bool:
    """看一个值是不是掩码（前端把掩码原样回传时不要覆盖真密钥）。"""
    v = str(value or "")
    return "…" in v or (v and set(v) == {"•"})


def load(data_dir) -> dict:
    """读设置。缺项用默认值补齐（**不是留空**——留空会让预算算不出来）。"""
    raw = store.read_json(data_dir / "settings.json", {})
    return {
        "version": int(raw.get("version") or presets.DATA_VERSION),
        "providers": list(raw.get("providers") or []),
        "advanced": {**presets.DEFAULT_SETTINGS["advanced"], **(raw.get("advanced") or {})},
        # 从平台报错里学到的每个模型的上限/窗口
        "modelInfo": dict(raw.get("modelInfo") or {}),
    }


def save(data_dir, settings: dict) -> dict:
    """写设置。版本号始终写最新的。"""
    out = {
        "version": presets.DATA_VERSION,
        "providers": list(settings.get("providers") or []),
        "advanced": dict(settings.get("advanced") or {}),
        "modelInfo": dict(settings.get("modelInfo") or {}),
    }
    store.write_json(data_dir / "settings.json", out)
    return out


def public_view(settings: dict) -> dict:
    """给前端的视图：密钥打码，但**告诉它原来有没有填**（否则界面上分不清
    "没填"和"填了但被打了码"）。"""
    out = dict(settings)
    out["providers"] = [
        {**p, "apiKey": mask_key(p.get("apiKey")), "hasKey": bool(p.get("apiKey"))}
        for p in settings.get("providers", [])
    ]
    return out


def provider(settings: dict, provider_id: str) -> dict | None:
    return next((p for p in settings.get("providers", []) if p.get("id") == provider_id), None)


def merge_providers(old: list[dict], incoming: list[dict]) -> list[dict]:
    """合并前端提交的平台列表。

    **掩码值不许覆盖真密钥**：前端拿到的是 `sk-a…1234`，它原样回传时
    我们得认出那是掩码，保留原来的密钥。这条不做的话，用户随手点一下保存，
    密钥就被自己的掩码覆盖了。
    """
    by_id = {p.get("id"): p for p in old}
    out = []
    for p in incoming:
        prev = by_id.get(p.get("id"), {})
        key = p.get("apiKey")
        if key is None or is_masked(key) or key == "":
            key = prev.get("apiKey", "")
        out.append({**prev, **p, "apiKey": key})
    return out


# ─────────────────────────── 预算 ───────────────────────────


@dataclass
class Window:
    tokens: int
    source: str  # api=平台报的 | manual=你填的 | guess=保守猜的


def resolve_window(settings: dict, model: str, provider_id: str = "") -> Window:
    """这个模型的上下文窗口有多大。

    优先级：平台报过的 > 你在高级设置里手填的 > 保守猜的 128k。
    """
    info = (settings.get("modelInfo") or {}).get(model) or {}
    from_api = info.get("contextWindow")
    if isinstance(from_api, (int, float)) and from_api > 0:
        return Window(int(from_api), "api")
    manual = (settings.get("advanced") or {}).get("contextWindow")
    if isinstance(manual, (int, float)) and manual > 0:
        return Window(int(manual), "manual")
    return Window(int(DEFAULT_WINDOW), "guess")


def resolve_output_cap(settings: dict, model: str, window: Window) -> tuple[int, int]:
    """这次实际能用多少输出额度：`(你的设置, 实际会用到的)`。

    三者取最小：你设的值、平台学到的上限、**上下文窗口本身**。
    最后一条最容易被忽略——窗口 8k 却要 32k 的输出，平台只会回你一个 400。
    """
    wanted = int((settings.get("advanced") or {}).get("maxTokens") or 4096)
    learned = ((settings.get("modelInfo") or {}).get(model) or {}).get("maxOutput")
    cap = min(wanted, MAX_OUTPUT_CEILING, window.tokens)
    if isinstance(learned, (int, float)) and learned > 0:
        cap = min(cap, int(learned))
    return wanted, max(1, int(cap))


def learn_model_limit(data_dir, settings: dict, model: str, limit: int) -> dict:
    """平台在 400 里说了它的输出上限 → 记下来，下次不再白挨一次。"""
    if limit <= 0:
        return settings
    info = settings.setdefault("modelInfo", {})
    entry = dict(info.get(model) or {})
    entry["maxOutput"] = int(limit)
    info[model] = entry
    save(data_dir, settings)
    return settings
