"""纯标准库的保真检查：移植过来的内容必须和老的 presets.mjs **一字不差**。

    py -3 dev/check_presets.py

为什么值得单独写一个脚本：那 6 段人格提示词是这个产品最不可替代的东西，
而"手抄一遍"是这类迁移里最容易悄悄出错的地方——少一个星号、多一个空行，
肉眼看不出来，效果却会变。

做法：把旧文件当纯文本读进来，逐条检查新 JSON 里的字符串**是不是原文的子串**。
如果移植时被改动过一个字符，这里就会红。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OLD = Path("D:/AAsanboxForDsh/server/presets.mjs")

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {msg}")
    else:
        failed += 1
        print(f"  ❌ {msg}")


def main() -> int:
    if not OLD.exists():
        print(f"找不到旧文件：{OLD}（换机器时用 DL_OLD_SERVER 指过去）")
        return 1
    old_text = OLD.read_text(encoding="utf-8")

    from server import presets

    print("── 平台模板 ──")
    check(len(presets.PROVIDER_PRESETS) == 9, f"9 个平台模板（实际 {len(presets.PROVIDER_PRESETS)}）")
    for p in presets.PROVIDER_PRESETS:
        ok = f"'{p['key']}'" in old_text and f"'{p['label']}'" in old_text
        if p["baseUrl"]:
            ok = ok and p["baseUrl"] in old_text
        check(ok, f"{p['key']}：key / label / baseUrl 都在原文里")
    check(all(p["kind"] in ("openai", "anthropic") for p in presets.PROVIDER_PRESETS),
          "kind 只有 openai / anthropic 两种")

    print("\n── 内置人格（逐字比对）──")
    check(len(presets.SEED_TEMPLATES) == 6, f"6 段内置人格（实际 {len(presets.SEED_TEMPLATES)}）")
    total = 0
    for t in presets.SEED_TEMPLATES:
        check(t["system"] in old_text, f"「{t['name']}」的提示词 {len(t['system'])} 字，逐字相同")
        check(t["tagline"] in old_text, f"「{t['name']}」的一句话简介相同")
        check(t["emoji"] in old_text, f"「{t['name']}」的图标相同")
        check(all(tag in old_text for tag in t["tags"]), f"「{t['name']}」的标签相同")
        total += len(t["system"])
    check(total == 2377, f"6 段提示词合计 {total} 字（旧项目导出时也是 2377 字）")

    print("\n── 纯基线模板 ──")
    b = presets.BARE_TEMPLATE
    check(b["name"] == "裸模型" and b["tagline"] in old_text, "名称与简介相同")
    check(b.get("bare") is True, "bare 标记带过来了（服务端据此**不构造** system 字段）")
    check(b["system"] == "", "system 是空串（配合 bare，永远发不出去）")

    print("\n── 提炼元提示词 ──")
    check(presets.DISTILL_SYSTEM in old_text,
          f"提炼元提示词 {len(presets.DISTILL_SYSTEM)} 字，逐字相同")
    check("人格建模师" in presets.DISTILL_SYSTEM, "内容对（不是别的提示词）")

    print("\n── 提炼的用户消息 ──")
    msg = presets.distill_user_prompt("《测试》", "正文", "重点保留语气")
    check("【材料名称】《测试》" in msg and "【材料正文】" in msg, "材料名称与正文都在")
    check("【额外要求】重点保留语气" in msg, "额外要求带上了")
    check("严格按 JSON 输出" in msg, "结尾要求还在")
    check("【额外要求】" not in presets.distill_user_prompt("x", "y"), "没传额外要求时不留空段")

    print("\n── 默认设置 ──")
    adv = presets.DEFAULT_SETTINGS["advanced"]
    old_adv = json.loads((ROOT / "server" / "presets.json").read_text(encoding="utf-8"))["defaultSettings"]["advanced"]
    check(adv == old_adv, f"高级设置的默认值和老项目一致：{adv}")
    check(presets.DEFAULT_SETTINGS["version"] == 9, "数据版本是 9（取消圆桌那一版）")
    check(presets.DEFAULT_CONTEXT_WINDOW == 128000, "兜底窗口 128k")

    print(f"\n{'─' * 46}")
    print(f"  通过 {passed} 项，失败 {failed} 项")
    print(f"{'─' * 46}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
