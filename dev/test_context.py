"""上下文组装的真实规模测试：把迁移出来的 15 位生命真灌进去。

    py -3 dev/test_context.py
    py -3 dev/test_context.py --show      # 把组装出来的原文打印出来看

前面的 test_migrate 验的是"搬对了没有"，这个验的是"搬对之后，真拿他们对话会发出什么"。
两件事不一样：数据对了，装进请求里照样可能是错的（时间感是空的、人格被挤掉、
窗口不够时把最新那句一起砍了、user/assistant 交替断了被平台拒）。

真实数据的规模值得单独测，因为它跟合成数据不同：
  · 孙宇晨那份人格 1591 字符，一个人就吃掉可观的窗口
  · th_b7f5896a0e6b 这张桌子他答了 6 轮，原文接近 5000 token
  · 迁移过来的会话里**没有摘要、没有记忆条目**（那两步还没做），
    所以"更早的对话"和"你记得的事"这两档现在是空的——这个是事实，不是 bug
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import context, migrate, templates  # noqa: E402
from server.life import Life  # noqa: E402

passed = failed = 0
SHOW = "--show" in sys.argv
REAL_SRC = Path("D:/AAsanboxForDsh/data")


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {msg}")
    else:
        failed += 1
        print(f"  ❌ {msg}")


def alternation_ok(messages: list[dict]) -> str:
    """返回 "" 表示没问题，否则返回人话描述的毛病。"""
    if not messages:
        return "一条消息都没有"
    if messages[0]["role"] != "user":
        return f"第一条不是 user（是 {messages[0]['role']}）"
    if messages[-1]["role"] != "user":
        return f"最后一条不是 user（是 {messages[-1]['role']}）—— 平台会因为「没有新问题」报错"
    for a, b in zip(messages, messages[1:]):
        if a["role"] == b["role"]:
            return f"出现连续两条 {a['role']}"
    return ""


def expect_message(e: dict) -> str:
    """独立重算一遍：一条对话记录拼出来应该是这段文字。

    故意不复用 context 的内部函数——这样那边规则被改坏了，这个测试还能发现。
    """
    prefix = f"（{e['who']}）" if e.get("kind") == "heard" else ""
    return prefix + e.get("text", "")


def show_built(label: str, built: context.Built) -> None:
    if not SHOW:
        return
    print(f"\n{'━' * 60}\n■ {label}")
    print(f"  system：{'（不发 system 字段）' if built.system is None else f'{len(built.system)} 字符'}")
    if built.system:
        head = built.system[:400]
        print("  ┌" + head.replace("\n", "\n  │"))
        if len(built.system) > 400:
            print(f"  │ …（后面还有 {len(built.system) - 400} 字符）")
        print("  └")
    print(f"  消息 {len(built.messages)} 条：")
    for m in built.messages:
        one = m["content"].replace("\n", " ⏎ ")
        print(f"    [{m['role']:<9}] {len(m['content']):>5} 字  {one[:110]}{'…' if len(one) > 110 else ''}")


def main() -> int:
    if not REAL_SRC.exists():
        print(f"找不到老数据 {REAL_SRC}")
        return 1
    root = Path(os.environ.get("RT_CTX_DIR") or Path(__file__).resolve().parent / ".tmp-context")
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    dst = root / "new"
    migrate.migrate(REAL_SRC, dst)  # 幂等，这里就是"先搬再测"
    print(f"临时目录：{root}")
    print(f"现在是：{context.fmt_day(int(time.time() * 1000))} {context.fmt_clock(int(time.time() * 1000))}")

    def life_of(old_id: str) -> Life:
        return Life.load(dst / "humans" / templates.stable_id("dh", old_id))

    sun = life_of("dh_a932252c3521")  # 孙宇晨 on deepseek，人格 1591 字符
    kimi = life_of("dh_bbf32be17c42")  # 裸模型，人格为空
    sun_tpl = templates.load(dst, sun.identity["templateId"])
    kimi_tpl = templates.load(dst, kimi.identity["templateId"])

    # ── 1. 一切正常的大窗口：什么都别丢 ──
    print("\n── 窗口够大时：一句原文都不该丢 ──")
    big = context.build_context(
        sun, session_id="th_b7f5896a0e6b", question="那你觉得我该怎么做？",
        window=128000, persona=sun_tpl.system, bare=sun_tpl.bare,
    )
    total = len([e for e in sun.read_session("th_b7f5896a0e6b") if e.get("kind") != "summary"])
    check(big.dropped == 0, f"128k 窗口下没丢东西（2 条会话共 {total} 条原文全带上了）")
    check(big.session_entries == total, f"带上了全部 {total} 条（实际 {big.session_entries}）")
    check(alternation_ok(big.messages) == "", f"user/assistant 交替正常（{alternation_ok(big.messages) or '无毛病'}）")

    # ── 2. 人格真的被放在系统消息里，没被截断 ──
    print("\n── 人格与时间感 ──")
    check(big.system is not None and big.system.startswith(sun_tpl.system[:50]),
          "system 是以他的人格开头的（模型第一眼看到的是他是谁）")
    check(len(sun_tpl.system) == 1591 and sun_tpl.system in big.system,
          "1591 字符的人格一字不少地进去了（没有被预算裁掉）")
    check("【现在】" in big.system, "带上了「现在几点」——他才知道自己活在什么时候")
    check("【你上次和他说话】" in big.system, "带上了「你上次和他说话是什么时候」")

    # ── 3. 迁移过来的会话还没有摘要和记忆（事实，不是 bug）──
    print("\n── 四档记忆里现在实际有哪几档 ──")
    check(big.older_summaries == 0, "「更早的对话」是空的——迁移只搬了原文，摘要还没生成")
    check(big.memory_lines == 0, "「你记得的事」是空的——长期记忆条目还没提取")
    check(sun.index_text().strip() == "", "他的记忆目录确实是空的（等下一步提炼）")
    # 空的档位应该是**整段不出现**，而不是留一个「你记得的事：（无）」的空标题占窗口。
    # 注意不能靠数 "【" 的个数来判断：人格正文里本来就有小标题（苏格拉底那份有 3 个）。
    check("【更早的对话" not in big.system and "【你记得的事" not in big.system,
          "空的档位整段不出现，不留空标题占窗口")

    # ── 4. 窗口不够时：从最老的那句开始砍，最新那句必须保住 ──
    print("\n── 窗口不够时砍哪头 ──")
    small = context.build_context(
        sun, session_id="th_b7f5896a0e6b", question="那你觉得我该怎么做？",
        window=5000, output_reserve=1024, persona=sun_tpl.system, bare=sun_tpl.bare,
    )
    check(small.dropped > 0, f"5000 窗口下确实要砍（砍了 {small.dropped} 条）")
    check(small.session_entries + small.dropped == total,
          f"账对得上：带上 {small.session_entries} + 砍掉 {small.dropped} = {total} 条")
    check(small.session_entries < big.session_entries, "带的比大窗口时少")
    check(small.session_entries >= 2,
          f"但不是砍到只剩一句残句——至少保住最近一来一回（实际 {small.session_entries} 条）")
    last_self = [e for e in sun.read_session("th_b7f5896a0e6b") if e.get("kind") == "self"][-1]["text"]
    check(any(last_self[:30] in m["content"] for m in small.messages),
          "他上一轮说的最后那段话还在（砍的是更早的，不是最新的）")
    check(alternation_ok(small.messages) == "", f"砍完交替仍然正常（{alternation_ok(small.messages) or '无毛病'}）")
    check(small.estimated_input + 1024 <= 5000,
          f"估算占用 {small.estimated_input} + 输出预留 1024 ≤ 窗口 5000")
    show_built("孙宇晨 · 6 轮桌子 · 5000 窗口（砍过的）", small)

    # ── 5. 窗口小到装不下任何历史：问题本身决不能被丢掉 ──
    print("\n── 窗口再小，这一轮的问题也不能丢 ──")
    tiny = context.build_context(
        sun, session_id="th_b7f5896a0e6b", question="在吗？",
        window=3000, output_reserve=512, persona=sun_tpl.system, bare=sun_tpl.bare,
    )
    check(tiny.session_entries == 0, f"历史一条都装不下（实际 {tiny.session_entries} 条）")
    check(tiny.messages == [{"role": "user", "content": "在吗？"}],
          f"只剩问题本身，而且是干净的（实际 {tiny.messages}）")
    check(tiny.budget >= 0, f"预算被夹到不小于 0（实际 {tiny.budget}）——负数会让后面的判断全乱")
    check(alternation_ok(tiny.messages) == "", "仍然合法")

    # ── 6. 裸模型：连 system 字段都不发 ──
    print("\n── 裸模型（探针）──")
    bare = context.build_context(
        kimi, session_id="th_7411ecd7de34", question="1+1 等于几？",
        window=128000, persona=kimi_tpl.system, bare=kimi_tpl.bare,
    )
    check(kimi_tpl.bare is True, "这位用的是标了 bare 的模板")
    check(bare.system is None, "system 是 None——**不是发个空字符串，是压根不构造**")
    check(bare.messages == [{"role": "user", "content": "1+1 等于几？"}],
          f"只发问题本身，连时间感和记忆都不注入（实际 {len(bare.messages)} 条）")
    check(bare.session_entries == 0, "裸模型不读历史（它是对打的探针，不是陪伴）")

    # ── 7. 对照：同一个问题，裸的和有记忆的，发出去的东西差多少 ──
    print("\n── 有记忆 vs 没记忆，同一个问题 ──")
    rich = context.build_context(
        kimi, session_id="th_7411ecd7de34", question="1+1 等于几？",
        window=128000, persona=None, bare=False,
    )
    check(rich.system is not None and "【现在】" in rich.system,
          "非裸的即使人格为空，也仍然带时间感（他仍然活在某个时刻）")
    check(rich.session_entries > 0, f"非裸的会带上历史（带了 {rich.session_entries} 条）")
    check(len(rich.messages) > len(bare.messages), "两者的消息条数不同——这就是裸模型能当对照组的原因")
    show_built("孙宇晨 · 6 轮桌子 · 128k 窗口（没砍）", big)

    # ── 8. 每一场真实会话都装一遍，看有没有组合会炸 ──
    print("\n── 15 位生命 × 他们各自所有的会话，全跑一遍 ──")
    combos = 0
    slack = 10 ** 9  # 窗口减掉"估算占用 + 输出预留"之后还剩多少余量
    worst = ""
    bad: list[str] = []
    for life in Life.list_all(dst / "humans"):
        tpl = templates.load(dst, life.identity["templateId"])
        for sid in life.sessions():
            combos += 1
            for window, reserve in ((128000, 8192), (32768, 4096), (8192, 1024)):
                b = context.build_context(
                    life, session_id=sid, question="继续说说？", window=window,
                    output_reserve=reserve, persona=tpl.system, bare=tpl.bare,
                )
                tag = f"{life.identity['name']}/{sid}/{window}"
                why = alternation_ok(b.messages)
                if why:
                    bad.append(f"{tag}：{why}")
                if "继续说说？" not in b.messages[-1]["content"]:
                    bad.append(f"{tag}：最后一个问题不在最后一条消息里")
                left = window - reserve - b.estimated_input
                if left < slack:
                    slack, worst = left, tag
    check(not bad, f"{combos} 个生命×会话组合 × 3 种窗口，全部合法"
                   + (f"，但有 {len(bad)} 处问题：{bad[:3]}" if bad else ""))
    check(slack > 0, f"没有一个组合超出窗口，最紧的那次（{worst}）还剩 {slack} token 余量")

    # ── 9. 账目审计：报了几条，就得真发出去几条 ──
    # 这一段是被一个真 bug 逼出来的：预算正好停在「他自己说的」那句上时，
    # 那条孤零零的助手发言会被合并逻辑丢掉，但账没跟着减，
    # 于是界面报 3 条、实际只发了 2 条（window=2800 那档真出现过）。
    print("\n── 账目审计：报了几条就得真发几条（含边界窗口扫描）──")
    problems: list[str] = []
    scanned = 0
    for life in Life.list_all(dst / "humans"):
        tpl = templates.load(dst, life.identity["templateId"])
        for sid in life.sessions():
            entries = [e for e in life.read_session(sid) if e.get("kind") != "summary"]
            # 从"几乎装不下"到"全都装得下"，每 100 token 扫一遍，
            # 专门撞那些正好停在半个来回上的窗口
            for window in range(1400, 13000, 100):
                scanned += 1
                b = context.build_context(
                    life, session_id=sid, question="Q", window=window,
                    output_reserve=512, persona=tpl.system, bare=tpl.bare,
                )
                tag = f"{life.identity['name']}/{sid}/window={window}"
                if tpl.bare:
                    # 裸模型走的是"压根不构造"的早退分支：不读历史、不发 system。
                    # 这不是漏账，是产品承诺（探针要看到模型的本来面目），顺手在这里验。
                    if (b.session_entries, b.dropped, b.history, b.system) != (0, 0, [], None):
                        problems.append(f"{tag}：裸模型竟然带了历史或 system")
                    continue
                if len(b.history) != b.session_entries:
                    problems.append(f"{tag}：报 {b.session_entries} 条，实际装了 {len(b.history)} 条")
                if b.history and b.history[0]["role"] != "user":
                    problems.append(f"{tag}：历史第一条是助手发言（不能这么开头）")
                joined = "\n".join(m["content"] for m in b.messages)
                if any(h["content"] not in joined for h in b.history):
                    problems.append(f"{tag}：报了的历史里有没真发出去的")
                # 顺序必须仍然是时间顺序，而且不许同一条装两遍。
                # 这里故意自己重算一遍消息内容（不复用 context 的内部函数），
                # 这样万一那边的拼装规则被改坏了，这里能独立发现。
                where = []
                for h in b.history:
                    hit = [k for k, e in enumerate(entries) if expect_message(e) == h["content"]]
                    if len(hit) != 1:
                        problems.append(f"{tag}：历史里有一条在原文里对不上号「{h['content'][:16]}」")
                        break
                    where.append(hit[0])
                if where != sorted(where) or len(set(where)) != len(where):
                    problems.append(f"{tag}：装进去的顺序乱了或重复了")
                if b.session_entries + b.dropped != len(entries):
                    problems.append(f"{tag}：带的 {b.session_entries} + 砍的 {b.dropped} ≠ 总共 {len(entries)}")
    check(not problems,
          f"{scanned} 次组装（每个会话扫了 116 档窗口）账目全部对得上"
          + (f"，但发现 {len(problems)} 处：{problems[:3]}" if problems else ""))

    # 这一档是那个真 bug 的原发地，单独钉住：非裸地组装 kimi 那场三问三答，
    # 窗口小到只装得下最后半个来回——正是"停在助手发言上"的边界。
    boundary = [
        context.build_context(kimi, session_id="th_7411ecd7de34", question="Q",
                              window=w, output_reserve=512, persona=None, bare=False)
        for w in range(2600, 3000, 20)
    ]
    check(all(len(b.history) == b.session_entries for b in boundary),
          "边界窗口（2600–3000）里报的条数和真装的条数一致")
    check(all(b.history[0]["role"] == "user" for b in boundary if b.history),
          "边界窗口里装出来的历史都以用户发言开头（没有孤零零的助手发言）")
    check(any(b.session_entries > 0 and b.dropped > 0 for b in boundary),
          "其中确实有需要砍的档位（不是全空或全带，否则这一段没测到东西）")

    print(f"\n{'=' * 46}\n通过 {passed}，失败 {failed}")
    if not SHOW:
        print("（加 --show 可以看到组装出来的原文）")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
