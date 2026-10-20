"""读取平台、人格与默认设置预设；提示词内容保存在 presets.json。"""

from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_DATA = json.loads((_DIR / "presets.json").read_text(encoding="utf-8"))

# 9 个平台模板：deepseek / openai / anthropic / moonshot / zhipu / dashscope /
# siliconflow / openrouter / custom
PROVIDER_PRESETS: list[dict] = _DATA["providerPresets"]

# 人格预设不绑定平台；创建实体时选择平台与模型。
SEED_TEMPLATES: list[dict] = _DATA["seedTemplates"]


BARE_TEMPLATE: dict = _DATA["bareTemplate"]

# 平台没报上下文窗口时按这个估。宁可保守：猜大了直接 400，猜小了只是少带几轮。
DEFAULT_CONTEXT_WINDOW: int = _DATA["defaultContextWindow"]

DATA_VERSION = 12

DEFAULT_SETTINGS: dict = {
    "version": DATA_VERSION,
    "providers": [],
    "advanced": dict(_DATA["defaultSettings"]["advanced"]),
}
