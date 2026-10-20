"""平台配置、密钥掩码与模型预算。运行时更新在锁内读取、合并、写入。"""

from __future__ import annotations

from . import presets, store


MAX_OUTPUT_CEILING = 32768


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


def update(data_dir, *, providers=None, advanced=None, model_info=None) -> dict:
    """在同一把锁内读、合并、写，避免并发更新互相覆盖。"""
    with store.lock_for(str((data_dir / "settings.json").resolve())):
        current = load(data_dir)
        if providers is not None:
            incoming = providers(current["providers"]) if callable(providers) else providers
            current["providers"] = merge_providers(current["providers"], incoming)
        current["advanced"].update(advanced or {})
        for model, info in (model_info or {}).items():
            current["modelInfo"][model] = {**current["modelInfo"].get(model, {}), **info}
        return save(data_dir, current)


def public_view(settings: dict) -> dict:
    """返回密钥掩码与 hasKey 标记，供界面区分已配置和未配置的平台。"""
    out = dict(settings)
    out["providers"] = [
        {**p, "apiKey": mask_key(p.get("apiKey")), "hasKey": bool(p.get("apiKey"))}
        for p in settings.get("providers", [])
    ]
    return out


def provider(settings: dict, provider_id: str) -> dict | None:
    return next((p for p in settings.get("providers", []) if p.get("id") == provider_id), None)


def merge_providers(old: list[dict], incoming: list[dict]) -> list[dict]:
    """合并平台列表。省略、空值或掩码形式的密钥保留原值。"""
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


def resolve_window(settings: dict, model: str) -> int:
    """这个模型的上下文窗口有多大。

    优先级：平台报过的 > settings.json 中指定的 > 保守猜的 128k。
    """
    info = (settings.get("modelInfo") or {}).get(model) or {}
    from_api = info.get("contextWindow")
    if isinstance(from_api, (int, float)) and from_api > 0:
        return int(from_api)
    manual = (settings.get("advanced") or {}).get("contextWindow")
    if isinstance(manual, (int, float)) and manual > 0:
        return int(manual)
    return int(DEFAULT_WINDOW)


def resolve_output_cap(settings: dict, model: str, window: int) -> int:
    """返回设置值、平台上限与窗口三者允许的输出额度。

    三者取最小：你设的值、平台学到的上限、**上下文窗口本身**。
    最后一条最容易被忽略——窗口 8k 却要 32k 的输出，平台只会回你一个 400。
    """
    wanted = int((settings.get("advanced") or {}).get("maxTokens") or 4096)
    learned = ((settings.get("modelInfo") or {}).get(model) or {}).get("maxOutput")
    cap = min(wanted, MAX_OUTPUT_CEILING, window)
    if isinstance(learned, (int, float)) and learned > 0:
        cap = min(cap, int(learned))
    return max(1, int(cap))


def history_budget(window: int, output_reserve: int, safety_ratio: float = 0.1) -> int:
    """预留输出与安全余量，剩余窗口用于人格、问题和历史原文。"""
    return max(0, window - output_reserve - max(1024, round(window * safety_ratio)))
