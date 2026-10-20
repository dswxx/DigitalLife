"""平台配置、密钥掩码与输出上限。运行时更新在锁内读取、合并、写入。"""

from __future__ import annotations

from . import presets, store


MAX_OUTPUT_CEILING = 32768


def mask_key(key: str) -> str:
    """向界面返回掩码；长密钥保留首尾片段用于辨认，短密钥全部遮盖。"""
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
    """补齐默认配置；真实密钥只留在服务端对象中，响应使用 public_view。"""
    raw = store.read_json(data_dir / "settings.json", {})
    return {
        "version": int(raw.get("version") or presets.DATA_VERSION),
        "providers": list(raw.get("providers") or []),
        "advanced": {**presets.DEFAULT_SETTINGS["advanced"], **(raw.get("advanced") or {})},
        # 从平台报错里学到的输出上限；旧 contextWindow 字段不再参与请求。
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
    """在锁内读取最新配置再合并，避免较早请求覆盖用户刚保存的密钥。

    providers=None 表示不更新平台；空列表表示删除全部平台。
    也接受基于最新平台列表的函数，供拉取模型列表后局部更新使用。
    """
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
    """incoming 决定最终平台列表；省略、空值或掩码形式的密钥沿用原值。

    只更新显示名称或模型列表时，浏览器无需也不应持有真实密钥。
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


# ─────────────────────────── 输出上限 ───────────────────────────


def resolve_output_cap(settings: dict, model: str) -> int:
    """仅控制生成长度，不借助猜测的上下文窗口限制输入。"""
    wanted = int((settings.get("advanced") or {}).get("maxTokens") or 4096)
    learned = ((settings.get("modelInfo") or {}).get(model) or {}).get("maxOutput")
    cap = min(wanted, MAX_OUTPUT_CEILING)
    if isinstance(learned, (int, float)) and learned > 0:
        cap = min(cap, int(learned))
    return max(1, int(cap))
