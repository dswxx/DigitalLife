"""读取平台、人格与默认设置预设；提示词内容保存在 presets.json。"""

from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_DATA = json.loads((_DIR / "presets.json").read_text(encoding="utf-8"))

# 平台卡片和人格正文均由数据文件提供，新增预设无需改界面代码。
PROVIDER_PRESETS: list[dict] = _DATA["providerPresets"]

# 人格预设不绑定平台；创建实体时选择平台与模型。
SEED_TEMPLATES: list[dict] = _DATA["seedTemplates"]


BARE_TEMPLATE: dict = _DATA["bareTemplate"]

# 数据格式版本与产品版本、HTTP 接口版本分别维护。
DATA_VERSION = 12

DEFAULT_SETTINGS: dict = {
    "version": DATA_VERSION,
    "providers": [],
    "advanced": dict(_DATA["defaultSettings"]["advanced"]),
}
