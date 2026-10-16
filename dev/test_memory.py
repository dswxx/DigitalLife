"""压缩层测试：用**真实生命 + 假模型**验"什么时候压、压哪段、压完怎么记"。

    py -3 dev/test_memory.py

为什么这一层能离线测完：压缩的模型调用是注入的（`model.ModelReply`），
所以除了"真去发一次 HTTP"以外，其余全部能验——
而真正容易出错的地方（水位线算错、把一整句话切成半句、压完原文丢了、
重跑压两遍、崩在中间）恰恰都不在 HTTP 那一层。

这个测试模拟的是完整的一生：
  一位生命和用户聊了很多轮 → 水位超过 60% → 后台压一次 →
  下一次请求只带摘要 + 最近的原文 → 再聊几轮 → 再压一次（新旧摘要合成一段）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import context, memory, migrate, presets, store, templates  # noqa: E402
from server.life import Life  # noqa: E402
from server.model import ModelReply  # noqa: E402

passed = failed = 0
REAL_SRC = Path("D:/AAsanboxForDsh/data")


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {msg}")
    else:
        failed += 1
        print(f"  ❌ {msg}")


class FakeModel:
    """假模型：把"它看到的"记下来，并返回一段可辨认的摘要。

    它同时是断言工具——真实调用里最容易出错的不是"模型回了什么"，
    而是"我们喂给它的到底是什么"（有没有带时间戳、有没有带旧摘要）。
    """

    def __init__(self, text="【摘要】这次主要在谈辞职和写作的事。", usage=(1200, 80)):
        self.text = text
        self.usage = usage
        self.calls: list[dict] = []

    def __call__(self, *, system, messages):
        self.calls.append({"system": system, "messages": messages})
        return ModelReply(text=self.text, prompt_tokens=self.usage[0],
                          completion_tokens=self.usage[1], model="fake-1")


class EmptyModel(FakeModel):
    """返回空摘要的坏模型。用来验"宁可下次再压，也不能把一段历史压没了"。"""

    def __init__(self):
        super().__init__(text="   ")


def add_round(life: Life, session_id: str, q: str, a: str, *, at: int) -> None:
    """造一轮对话（直接写记录，不经过模型）。"""
    store.append_jsonl(life.session_path(session_id), {"t": at, "kind": "you", "text": q})
    store.append_jsonl(life.session_path(session_id), {"t": at + 1000, "kind": "self", "text": a})


def main() -> int:
    root = Path(os.environ.get("RT_MEM_DIR") or Path(__file__).resolve().parent / ".tmp-memory")
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)

    # ── 造一位生命，聊够多轮 ──
    humans = root / "humans"
    life = Life.create(humans, template_id="tpl_x", provider_id="pv_x", model="m1",
                       name="苏格拉底", emoji="🏛️")
    sid = "th_long"
    base = 1789000000000
    for i in range(12):
        add_round(life, sid,
                  f"第 {i + 1} 个问题：我该怎么安排时间？" + "背景" * 20,
                  f"第 {i + 1} 个回答：先做最重要的一件事。" + "理由" * 40,
                  at=base + i * 60000)

    raw_all = life.read_session(sid)
    print(f"造好了：{len(raw_all)} 条记录（12 轮），"
          f"约 {sum(context.estimate_tokens(e['text']) for e in raw_all)} token\n")

    # ── 1. 水位没到就不该压 ──
    print("── 什么时候压 ──")
    p = memory.plan(life, sid, budget=100000)
    check(not p.needed, f"预算很大时不压：{p.reason}")
    check("水位没到" in p.reason, "理由说的是「水位没到」")

    p = memory.plan(life, sid, budget=2000)
    check(p.needed, f"预算不够时该压：{p.reason}")
    check(p.through > 0, f"摘要要覆盖到某个时间点（{p.through}）")
    check(len(p.compress_entries) + len(p.keep_entries) == len(raw_all),
          f"两段加起来正好是全部（{len(p.compress_entries)} 压 + {len(p.keep_entries)} 留）")
    check(p.compress_entries[0]["t"] < p.compress_entries[-1]["t"], "压的是较老的那一段")
    check(p.keep_entries[0]["t"] > p.compress_entries[-1]["t"], "留的是较新的那一段")
    # ── 2. 切点不能落在半句话上 ──
    print("\n── 切点：不能把一问一答切成半句 ──")
    check(p.keep_entries[0]["kind"] in ("you", "heard"),
          f"留下的第一句必须是用户说的（实际 {p.keep_entries[0]['kind']}）")
    check(all(p.compress_entries[i]["t"] < p.compress_entries[i + 1]["t"]
              for i in range(len(p.compress_entries) - 1)), "压的那段仍是时间顺序")
    for budget in range(1200, 9000, 200):
        pp = memory.plan(life, sid, budget=budget)
        if pp.needed and pp.keep_entries:
            if pp.keep_entries[0]["kind"] not in ("you", "heard"):
                check(False, f"预算 {budget} 时切出了半句：留下的第一条是 {pp.keep_entries[0]['kind']}")
                break
    else:
        check(True, "扫了 40 档预算，每一档留下的都是一整句话的开头")

    # ── 3. 压一次 ──
    print("\n── 压一次：喂进去什么、记下来什么 ──")
    fake = FakeModel()
    meta = memory.compress(life, sid, caller=fake, model="fake-1", budget=2000)
    check(meta is not None, "压成功了")
    check(len(fake.calls) == 1, "只调了一次模型")
    body = fake.calls[0]["messages"][0]["content"]
    check("【之前的摘要】" not in body, "第一次压没有旧摘要可带")
    check(re.match(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] 用户：", body) is not None,
          "喂进去的对话**带时间戳**（记忆的年龄靠它）：" + body[:24])
    check("] 苏格拉底：" in body, "他的发言标的是他自己的名字，而不是「助手」")
    check("system" in fake.calls[0] and fake.calls[0]["system"].startswith("你在帮一位数字生命"),
          "用的是压缩专用的提示词，不是他的人格")

    check(meta["summary"] == fake.text, "摘要存下来了")
    check(meta["times"] == 1, "记录压过 1 次")
    check(meta["usage"]["completionTokens"] == 80, "压缩花的 token 进账了（否则越老越贵而看不见）")
    check(meta["compactedOffset"] > 0, f"水位线的字节位置记下来了（{meta['compactedOffset']}）")
    check(meta["model"] == "fake-1", "记下是哪个模型压的")

    # ── 4. 原文一个字都不能少 ──
    print("\n── 原文是真相：压缩不删原文 ──")
    raw_after = life.read_session(sid)
    check(len(raw_after) == len(raw_all) + 1, f"原文 {len(raw_all)} 条一条没少，只多了一条摘要（{len(raw_after)}）")
    check([e["text"] for e in raw_after[: len(raw_all)]] == [e["text"] for e in raw_all],
          "而且前 24 条逐字符没变过（只追加，永不改写）")
    check(raw_after[-1]["kind"] == "summary", "多出来的那条是 summary")
    check(raw_after[-1]["through"] == meta["compactedThrough"], "摘要记着它覆盖到哪个时间点")

    # ── 5. 下一次请求只带尾巴 + 摘要 ──
    print("\n── 下一次请求：老原文不再解析，摘要进 system ──")
    tail = life.read_session(sid, only_tail=True)
    check(all(e["kind"] == "summary" or e["t"] > meta["compactedThrough"] for e in tail),
          f"尾巴里只有水位线之后的原文（{len([e for e in tail if e['kind'] != 'summary'])} 条）")
    check(all(e["t"] <= meta["compactedThrough"] for e in p.compress_entries),
          "被压的那些原文都在水位线之前")
    check(all(e["t"] > meta["compactedThrough"] for e in p.keep_entries),
          "留下的那些都在水位线之后（所以它们仍然在请求里）")
    check(len(tail) == len(p.keep_entries) + 1,
          f"尾巴 = 留下的 {len(p.keep_entries)} 条 + 那条摘要（实际 {len(tail)}）")

    tpl = templates.Template(id="tpl_x", name="苏格拉底", system="你是苏格拉底。", emoji="🏛️")
    built = context.build_context(life, session_id=sid, question="那我明天先做哪件？",
                                  window=4000, output_reserve=1024,
                                  persona=tpl.system, bare=False)
    check(built.compacted is True, "组装结果标着「这次会话已整理过」")
    check("【这次对话更早的部分，你只记得这些】" in built.system, "system 里带上了摘要")
    check(fake.text.strip()[:20] in built.system, "带的就是那段摘要")
    check(built.session_entries + built.dropped == len(p.keep_entries),
          f"账只覆盖水位线之后的原文（{len(p.keep_entries)} 条），而不是全部 24 条")
    check(built.session_entries < len(raw_all), f"要装的历史确实变少了（{built.session_entries} 条，原来 24 条）")

    # ── 6. 紧接着再压一次：水位已经推过去了，不该重复压 ──
    print("\n── 不会重复压 ──")
    again = memory.plan(life, sid, budget=2000)
    check(not again.needed, f"刚压完不用再压：{again.reason}")
    fake2 = FakeModel()
    check(memory.compress(life, sid, caller=fake2, model="fake-1", budget=2000) is None,
          "compress 也不会真的调模型")
    check(len(fake2.calls) == 0, "一次模型调用都没发生")

    # ── 7. 又聊了几轮，第二次压缩：新旧摘要合成一段 ──
    print("\n── 再聊几轮，第二次压缩 ──")
    for i in range(12, 20):
        add_round(life, sid, f"第 {i + 1} 个问题：换个话题。" + "追问" * 20,
                  f"第 {i + 1} 个回答：那就换个角度。" + "分析" * 40, at=base + i * 60000)
    p2 = memory.plan(life, sid, budget=2000)
    check(p2.needed, f"又超水位了：{p2.reason}")
    check(all(e["t"] > meta["compactedThrough"] for e in p2.compress_entries),
          "这次只压上次水位线之后的（老的那段不重复压）")

    fake3 = FakeModel(text="【摘要】辞职、写作、换话题这几件事都谈过了。")
    meta2 = memory.compress(life, sid, caller=fake3, model="fake-1", budget=2000)
    check(meta2 is not None and meta2["times"] == 2, f"记成第 2 次（实际 {meta2 and meta2['times']}）")
    body3 = fake3.calls[0]["messages"][0]["content"]
    check("【之前的摘要】" in body3 and fake.text.strip()[:12] in body3,
          "**把旧摘要一起喂进去了**——新旧要合成一段，不能越压越多段")
    check("【之后的对话】" in body3, "旧摘要和新对话分得清")
    check(meta2["compactedOffset"] > meta["compactedOffset"], "水位线往前推了")
    check(meta2["totalUsage"]["completionTokens"] == 160, "两次压缩的用量累加（80+80）")

    built2 = context.build_context(life, session_id=sid, question="接着说说",
                                   window=4000, output_reserve=1024, persona="你是苏格拉底。")
    check(built2.system.count("【这次对话更早的部分") == 1,
          "system 里只有**一段**摘要（滚动合成，不是叠好几层）")
    check(meta2["summary"][:16] in built2.system, "用的是最新的那段")

    # ── 8. 坏模型不能把历史压没 ──
    print("\n── 坏模型（返回空摘要）──")
    life2 = Life.create(humans, template_id="tpl_x", provider_id="pv_x", model="m1", name="测试者")
    sid2 = "th_bad"
    for i in range(12):
        add_round(life2, sid2, f"问题 {i}" + "字" * 60, f"回答 {i}" + "字" * 100, at=base + i * 1000)
    got = memory.compress(life2, sid2, caller=EmptyModel(), budget=2000)
    check(got is None, "返回 None，不认这个摘要")
    check(life2.compaction(sid2) == {}, "水位线没有推进（宁可下次再压，也不能把一段历史压没了）")
    check(len(life2.read_session(sid2, only_tail=True)) == 24, "原文一条不少，下次还会重压")

    # ── 9. 真实数据也走一遍：拿迁移过来的生命压 ──
    if REAL_SRC.exists():
        print("\n── 真实数据：给迁移过来的生命压一次 ──")
        dst = root / "real"
        migrate.migrate(REAL_SRC, dst)
        sun = Life.load(dst / "humans" / templates.stable_id("dh", "dh_a932252c3521"))
        real_sid = "th_b7f5896a0e6b"
        before = sun.read_session(real_sid)
        real_fake = FakeModel(text="【摘要】他反复在算账：导师、室友、身份、性。核心一句是「行动 > 思考」。")
        m = memory.compress(sun, real_sid, caller=real_fake, model="fake-1", budget=3000)
        check(m is not None, "真实的那 12 条历史压成功了")
        check(len(sun.read_session(real_sid)) == len(before) + 1, "原文没丢，只多了一条摘要")
        built3 = context.build_context(sun, session_id=real_sid, question="继续", window=128000,
                                      output_reserve=1024,
                                      persona=templates.load(dst, sun.identity["templateId"]).system)
        check("【这次对话更早的部分，你只记得这些】" in built3.system, "真实生命的 system 里出现了摘要")
        check(built3.compacted and built3.older_summaries == 0,
              "而且「更早的会话」那一档仍然空着（只压了当前这场）")
        other = sun.read_session("th_c1325ffc374e")
        check(other and all(e.get("kind") != "summary" for e in other),
              "同一生命的另一场会话没被牵连")
        # 另一场也压掉之后，「更早的会话」那一档就该出现一行了
        for i in range(10):
            add_round(sun, "th_c1325ffc374e", "那轮只留下了问题。" + "字" * 40,
                      "补一段更早的对话用来验跨会话那一档。" + "字" * 80, at=base + i * 7000)
        memory.compress(sun, "th_c1325ffc374e", model="fake-1", budget=1500,
                        caller=FakeModel(text="【摘要】那轮他调用失败了，后来补谈了一段。"))
        line = sun.summaries(exclude_session=real_sid)
        check(bool(line), f"另一场会话的摘要能被读到（读到 {len(line)} 条）")
        check(all("\n" not in x["text"] for x in line),
              f"但进提示词的是**一行**（不长篇搬过去）：{line[0]['text'][:40] if line else ''}")
        ns = context.build_context(sun, session_id=real_sid, question="继续", window=128000,
                                   output_reserve=1024,
                                   persona=templates.load(dst, sun.identity["templateId"]).system)
        check(ns.older_summaries == len(line),
              f"组装时把这一行带进了 system（{ns.older_summaries} 条）")
        check("【更早的对话，你只记得这些】" in ns.system, "而且归在「更早的对话」那一档")

    print(f"\n{'=' * 46}\n通过 {passed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
