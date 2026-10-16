"""平台模板 + 内置人格 + 提炼元提示词。

**内容放在 `presets.json` 里（数据），代码放这里。** 理由：

  · 那 6 段人格提示词是产品的灵魂，是**内容**不是逻辑。放 JSON 里能直接改、
    能 diff、能手写；放在 .py 里就得小心引号和缩进。
  · 这份 JSON 是用 `dev/_port_presets.mjs` 从老项目**原样导出**的，
    不是手抄的——保真度由构造保证，`dev/check_presets.py` 还会逐字比对一遍。

想加一个新平台？在 presets.json 的 providerPresets 里加一条，前端会自动多一个方块。
"""

from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent
_DATA = json.loads((_DIR / "presets.json").read_text(encoding="utf-8"))

# 9 个平台模板：deepseek / openai / anthropic / moonshot / zhipu / dashscope /
# siliconflow / openrouter / custom
PROVIDER_PRESETS: list[dict] = _DATA["providerPresets"]

# 6 段内置人格。**它们是「模板」，不绑厂商、没有记忆**——
# 用某个模板 + 某家 API 创建出来的才是一个有记忆的生命。
SEED_TEMPLATES: list[dict] = _DATA["seedTemplates"]

# 纯基线模板（原来的「裸模型」）：一个 system 字段都不发。
# 用途是当探针——同一个问题，一个席位放鲁迅、另一个放纯基线，
# 就能看出差异有多少来自提示词。它**不积累记忆**（探针不是陪伴）。
BARE_TEMPLATE: dict = _DATA["bareTemplate"]

# 平台没报上下文窗口时按这个估。宁可保守：猜大了直接 400，猜小了只是少带几轮。
DEFAULT_CONTEXT_WINDOW: int = _DATA["defaultContextWindow"]

DISTILL_SYSTEM: str = _DATA["distillSystem"]

# 上面这两条（`DISTILL_SYSTEM` 和下面那个 `distill_user_prompt`）是"提炼工作台"用的：
# 把一本书/一段材料炼成一份人格。**目前没有接口和界面用它**——你说这个功能先不做。
# 留着是因为它是**从老项目逐字搬过来的数据**，`dev/check_presets.py` 会盯着它别走样；
# 以后想做时不用重新搬一遍、也不用重新核。

# ── 数据版本 ──
#
# 老项目最后停在 7（v7 把超出范围的高级设置纠正回范围内）。
# 新项目从头开始记自己的版本号，8 是"生命/记忆"这一代的第一次结构变化：
# humans.json（一个数组）拆成 templates/（模板）+ humans/<id>/（生命）。
DATA_VERSION = 8

DEFAULT_SETTINGS: dict = {
    "version": DATA_VERSION,
    "providers": [],
    "advanced": dict(_DATA["defaultSettings"]["advanced"]),
}


def distill_user_prompt(source_name: str, text: str, extra: str = "") -> str:
    """提炼工作台发给模型的用户消息。

    和老项目一字不差（换了引号风格但内容相同），因为**提示词改了效果就会变**，
    而提炼质量还没有被验证过——这一版先不动它。
    """
    head = f"【材料名称】{source_name or '未命名材料'}\n\n【材料正文】\n\"\"\"\n{text}\n\"\"\"\n\n"
    tail = f"【额外要求】{extra}\n" if extra else ""
    return f"{head}{tail}请提炼出这个人的数字人人格，严格按 JSON 输出。"
