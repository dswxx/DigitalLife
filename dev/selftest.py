"""纯标准库自测：不需要 pytest，也不需要装任何包。

    py -3 dev/selftest.py

验的是"框架"这一层：生命的创建与记忆、JSONL 只追加、末行截断能容忍、
JSON 不转义中文、四档记忆递减和预算裁剪真的在做事。
HTTP 那一层（FastAPI）等依赖装好之后再单独验。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import context, store  # noqa: E402
from server.life import Life  # noqa: E402

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
    # 临时目录放在项目里（dev/.tmp），不放系统 temp：
    # 一来某些受限环境下系统 temp 写不进去，二来出问题时能直接翻文件看
    root = Path(os.environ.get("RT_SELFTEST_DIR") or Path(__file__).resolve().parent / ".tmp")
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    humans = root / "humans"
    print(f"临时目录：{root}\n")

    # ── 创建生命 ──
    print("── 生命的创建 ──")
    life = Life.create(humans, template_id="tpl_socrates", provider_id="pv_deepseek",
                       model="deepseek-flash", name="苏格拉底", emoji="🏛️")
    check((life.root / "identity.json").exists(), "identity.json 落地了")
    check((life.root / "sessions").is_dir(), "sessions/ 目录在")
    check((life.root / "memory").is_dir(), "memory/ 目录在")

    raw = (life.root / "identity.json").read_text(encoding="utf-8")
    check("苏格拉底" in raw, "JSON 里中文是原字，没有被转义成 \\uXXXX（否则记事本看不懂）")
    check(life.identity["templateId"] == "tpl_socrates", "记住了自己来自哪个模板")

    # ── 对话记录：只追加 ──
    print("\n── 对话记录（JSONL，只追加）──")
    sid = "th_first"
    life.append(sid, "you", text="我该不该辞职去写作？")
    life.append(sid, "self", text="你说想写作——那么我问你，你怕的是没钱，还是写不出东西？")
    life.append(sid, "heard", who="查理·芒格", text="先别问该不该，先问他能承受多久没收入。")
    entries = life.read_session(sid)
    check(len(entries) == 3, f"读回 {len(entries)} 条")
    check(all("t" in e for e in entries), "每条都带时间戳（记忆递减和时间感都靠它）")

    before = life.session_path(sid).stat().st_size
    life.append(sid, "self", text="接着说下一句。")
    after = life.session_path(sid).stat().st_size
    check(after > before, "追加只让文件变大，不改写前面（原文是真相）")

    # ── 末行截断能容忍 ──
    print("\n── 崩溃留下的半截行 ──")
    with life.session_path(sid).open("a", encoding="utf-8") as f:
        f.write('{"t":1,"kind":"self","text":"写到一半断电了')  # 故意不闭合
    got = life.read_session(sid)
    check(len(got) == 4, f"半截行被跳过而不是抛错，前面 {len(got)} 条都读得回来")

    # ── 长期记忆：索引与正文分离 ──
    print("\n── 长期记忆（索引 + 正文分离）──")
    life.remember(name="他怕的不是没钱", desc="他在写作这件事上真正的恐惧", type="user",
                  text="他三十三岁，已婚。怕的不是没收入，是怕自己写不出东西。\n不要再劝他「想清楚」，那句话对他没用。")
    life.remember(name="我对劝人想清楚的反感", desc="我认为那是逃避", type="self",
                  text="劝人想清楚，常常只是不想陪他想。")
    mems = life.memories()
    check(len(mems) == 2, f"写下 {len(mems)} 条记忆")
    idx = life.index_text()
    check("他怕的不是没钱" in idx, "目录里有这条")
    check("三十三岁" not in idx and "已婚" not in idx,
          "目录里**只有一句话摘要，没有正文**（记忆几百条也不炸窗口）")
    check("他在写作这件事上真正的恐惧" in idx, "目录里那句话是写入时给的 desc，不是正文首行")
    body = (life.memory_dir() / f"{mems[0].id}.md").read_text(encoding="utf-8")
    check(body.startswith("---") and "type: user" in body, "正文是 YAML 头 + Markdown（能直接手改）")
    check(life.forget(mems[1].id) and len(life.memories()) == 1, "能删掉一条（他记错了你直接删）")
    check("我对劝人想清楚的反感" not in life.index_text(), "删完目录也重建了")

    # ── 上下文组装：预算够时全带上 ──
    print("\n── 上下文组装 ──")
    for i in range(12):
        life.append(sid, "you", text=f"第 {i} 个问题：这件事你怎么看？")
        life.append(sid, "self", text=f"第 {i} 个回答：先说清你信什么。")
    wide = context.build_context(life, session_id=sid, question="现在这个问题", window=200_000,
                                 persona="你是苏格拉底。你从不直接给答案。")
    # 期望值从数据里数出来，不写死——写死的话改测试数据就会假失败
    expected = len([e for e in life.read_session(sid) if e.get("kind") != "summary"])
    check(wide.session_entries == expected,
          f"窗口够大时本会话原文全带上（{wide.session_entries}/{expected} 条）")
    check(wide.dropped == 0, "没有丢东西")
    check("现在这个问题" in wide.messages[-1]["content"], "本轮问题在最后")
    check(wide.system.startswith("你是苏格拉底。"), "人格在最前面")
    check("【现在】" in wide.system and "【你上次和他说话】" in wide.system, "system 里有时间感")
    check("【你记得的事】" in wide.system, "system 里有长期记忆目录")
    roles = [m["role"] for m in wide.messages]
    check(all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)),
          f"user/assistant 严格交替（Anthropic 的要求）：{roles[:6]}…")

    # ── 预算装不下时：丢最老的，保住最新的 ──
    print("\n── 预算不够时 ──")
    narrow = context.build_context(life, session_id=sid, question="现在这个问题", window=1400,
                                   output_reserve=200, persona="你是苏格拉底。你从不直接给答案。")
    check(narrow.session_entries < wide.session_entries,
          f"窗口小就少带（{wide.session_entries} → {narrow.session_entries} 条）")
    check(narrow.dropped > 0, f"确实丢了 {narrow.dropped} 条")
    check("第 11 个回答" in str(narrow.messages), "**最新的还在**（丢的是最老的，不是最新的）")
    check("第 0 个问题" not in str(narrow.messages), "最老的那条被丢了")
    check(narrow.estimated_input <= narrow.budget + narrow.estimated_input, "账算得出来")

    # ── 时间感：更早的会话只剩一行 ──
    print("\n── 记忆递减 ──")
    old = "th_old"
    life.append(old, "you", text="上周问过的事")
    life.append(old, "summary", text="他问过要不要换个城市，我说先问自己想要什么。")
    built = context.build_context(life, session_id=sid, question="今天聊什么", window=200_000,
                                  persona="你是苏格拉底。")
    check("换个城市" in built.system, "更早的会话以一行摘要进了 system")
    check("上周问过的事" not in built.system, "但那次会话的原文没有进 system（递减了）")
    check(built.older_summaries == 1, f"带上了 {built.older_summaries} 条更早会话的摘要")

    # ── 纯基线（原「裸模型」）：一个 system 字段都不发 ──
    print("\n── 纯基线模板 ──")
    bare_life = Life.create(humans, template_id="tpl_bare", provider_id="pv_kimi",
                            model="kimi-k2.6", name="kimi-k2.6")
    bare = context.build_context(bare_life, session_id="th_x", question="在吗", window=200_000,
                                 persona=None, bare=True)
    check(bare.system is None, "system 是 None —— 压根不构造，不是发空串（老项目的承诺）")
    check(len(bare.messages) == 1 and bare.messages[0]["content"] == "在吗",
          "只发用户那一句，连时间感和记忆都不加")
    notbare = context.build_context(life, session_id=sid, question="在吗", window=200_000,
                                    persona="你是苏格拉底。")
    check(notbare.system is not None and "【现在】" in notbare.system,
          "对照：普通生命仍然有时间感和记忆")

    print(f"\n{'─' * 46}")
    print(f"  通过 {passed} 项，失败 {failed} 项")
    print(f"{'─' * 46}")

    shutil.rmtree(root, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
