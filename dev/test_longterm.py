"""长期记忆测试：提取（写入）与召回（读取）。

    py -3 dev/test_longterm.py

设计文档把这两条路径定得很具体：

    写入：会话结束 / 聊得太长 → 模型提候选 → **逐条校验**（长期？重复？临时的？）
          → 写 memory/m_*.md → 重建 INDEX.md
    读取：本轮问题 + 最近几轮你的话 → 和 name / 一句话做关键词匹配
          → 时间衰减加权 → 取前几条，加载正文（总量封顶）→ 放进 system
          **不额外调用模型**（多一次调用就是每次对话都多花钱）

所以这个测试的重点不是"能提取出来"，而是三件容易出事的：
  1. **模型输出是整套系统里最不可靠的输入**：包代码块、前言后语、截断、
     单条而不是数组、类型不认识、字段缺一半——这些都得有确定的处理，
     而且绝不做"尽力猜"（猜错的记忆会一直留在他的记忆里）
  2. **去重要收敛**：同一段对话提两遍，不能越提越多
  3. **召回要"该想起来的想起来"**：相关的排在无关的前面，正文总量封顶，
     而且**不调模型**（这一条要能测出来：调用次数必须是 0）
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import context, memory, store  # noqa: E402
from server.life import Life  # noqa: E402
from server.model import ModelReply  # noqa: E402

passed = failed = 0
DAY = 86400000


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {msg}")
    else:
        failed += 1
        print(f"  ❌ {msg}")


class FakeModel:
    """假模型。返回什么由测试指定——重点验的是"我们怎么处理它的回话"。"""

    def __init__(self, text: str):
        self.text = text
        self.calls: list[dict] = []

    def __call__(self, *, system, messages):
        self.calls.append({"system": system, "messages": messages})
        return ModelReply(text=self.text, prompt_tokens=900, completion_tokens=120, model="fake-1")


class Boom(Exception):
    pass


class CountingModel(FakeModel):
    """一被调用就炸。用来证明"召回路径不许调模型"。"""

    def __call__(self, *, system, messages):
        raise Boom("召回路径不该调用模型！")


def talk(life: Life, session_id: str, pairs: list[tuple[str, str]], *, at: int) -> None:
    """造一段对话。**时间戳是给的，不是"现在"**——记忆的年龄靠它。"""
    for i, (q, a) in enumerate(pairs):
        store.append_jsonl(life.session_path(session_id),
                           {"t": at + i * 120000, "kind": "you", "text": q})
        store.append_jsonl(life.session_path(session_id),
                           {"t": at + i * 120000 + 1000, "kind": "self", "text": a})


def main() -> int:
    root = Path(os.environ.get("DL_LT_DIR") or Path(__file__).resolve().parent / ".tmp-longterm")
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    humans = root / "humans"
    humans.mkdir(parents=True)

    def new_life(name="苏格拉底"):
        return Life.create(humans, template_id="tpl_x", provider_id="pv_x", model="m1", name=name)

    # ── 1. 正常提取 ──
    print("── 提取：模型守规矩的时候 ──")
    life = new_life()
    sid = "th_1"
    talk(life, sid, [
        ("我是计算机研二的学生，导师不管我，我很迷茫。", "那先别谈迷茫，谈你手里有什么牌。"),
        ("以后别给我列清单，我看着烦。", "行，那我不列。"),
    ], at=int(time.time() * 1000) - 2 * DAY)
    good = json.dumps([
        {"name": "研二学生", "desc": "计算机专业研二，导师基本不管", "type": "user",
         "scope": "persistent", "text": "他在读研二，计算机专业，导师基本不管他。"},
        {"name": "不喜欢列清单", "desc": "明确要求不要给列表式的回答", "type": "feedback",
         "scope": "persistent", "text": "他明确说过别给他列清单，看着烦。"},
    ], ensure_ascii=False)
    fake = FakeModel(good)
    res = memory.extract(life, sid, caller=fake)
    check(res.called, "调用了一次模型")
    check(len(res.written) == 2, f"写下了 2 条（实际 {len(res.written)}）")
    check(len(life.memories()) == 2, "磁盘上确实是 2 条")
    check((life.memory_dir() / "INDEX.md").read_text(encoding="utf-8").count("\n") == 2,
          "目录里有 2 行")
    by_name = {m.name: m for m in life.memories()}
    check(by_name["研二学生"].type == "user" and by_name["不喜欢列清单"].type == "feedback",
          "类型按模型给的存下来（user / feedback）")
    check(all(m.source == sid for m in life.memories()), "每条都记着来源会话（记忆编辑器要显示）")
    check(all(m.at > 0 for m in life.memories()), "每条都带时间戳")
    body = fake.calls[0]["messages"][0]["content"]
    check("【对话】" in body and "[20" in body, "喂进去的对话带时间戳")
    check("【已有的记忆】" not in body, "第一次提取时还没有已有记忆可参照")

    # ── 2. 模型不守规矩的六种回话 ──
    print("\n── 提取：模型输出最不可靠（这一节是重点）──")
    one = json.dumps([{"name": "单条", "desc": "说明", "type": "self",
                       "scope": "session", "text": "正文"}], ensure_ascii=False)
    cases = [
        ("```json\n" + good + "\n```", 2, "包着 markdown 代码块"),
        ("好的，我挑出这些：\n" + good + "\n希望有用。", 2, "前后带话"),
        (json.dumps({"memories": json.loads(good)}, ensure_ascii=False), 2, "包在对象里"),
        (json.dumps(json.loads(good)[0], ensure_ascii=False), 1, "只给了一个对象而不是数组"),
        (one[:-12], 0, "JSON 被截断了"),
        ("我今天不想干活。", 0, "压根没给 JSON"),
        ("", 0, "什么都没返回"),
        (json.dumps([{"name": "", "text": "没名字"},
                     {"name": "有名字但没正文", "text": ""},
                     {"name": "类型不认识", "text": "正文在这", "type": "胡说八道", "scope": "胡来"}],
                    ensure_ascii=False), 1, "字段缺一半、类型不认识"),
        (json.dumps([{"name": f"第{i}条", "text": f"正文{i}"} for i in range(9)],
                    ensure_ascii=False), 5, "一口气给了 9 条（要截到 5 条）"),
    ]
    for idx, (reply, expect, label) in enumerate(cases):
        lf = new_life(f"测试{idx}")
        talk(lf, "th_x", [("问题", "回答")], at=int(time.time() * 1000))
        r = memory.extract(lf, "th_x", caller=FakeModel(reply))
        check(len(r.written) == expect, f"{label} → 写 {len(r.written)} 条（期望 {expect}）")
        check(len(lf.memories()) == len(r.written), f"{label} → 磁盘和记账一致")
        if expect == 0:
            check(r.rejected, f"{label} → 而且说明了为什么（{'、'.join(r.rejected)[:40]}）")

    # 类型不认识的那条要退化成 user，而不是丢掉整条
    lf = new_life("退化")
    talk(lf, "th_x", [("问题", "回答")], at=int(time.time() * 1000))
    r = memory.extract(lf, "th_x", caller=FakeModel(json.dumps(
        [{"name": "类型不认识", "text": "正文在这", "type": "胡说八道", "scope": "胡来"}],
        ensure_ascii=False)))
    got = r.written[0]
    check(got.type == "user" and got.scope == "persistent",
          f"不认识的 type/scope 退化成默认值，而不是丢掉整条（{got.type}/{got.scope}）")

    # ── 3. 去重必须收敛 ──
    print("\n── 去重：同一段对话提两遍不能越提越多 ──")
    r2 = memory.extract(life, sid, caller=FakeModel(good))
    check(len(r2.written) == 0, f"第二次一条都没新写（实际 {len(r2.written)}）")
    check(len(r2.skipped_duplicate) == 2, f"两条都识别为已经记过（实际 {len(r2.skipped_duplicate)}）")
    check(len(life.memories()) == 2, "记忆条数还是 2，没有翻倍")

    # 换个说法（标点不同）也算同一条
    same_other_punct = json.dumps([
        {"name": "研二学生。", "desc": "计算机专业研二，导师基本不管",
         "type": "user", "scope": "persistent",
         "text": "他在读研二，计算机专业，导师基本不管他"},
    ], ensure_ascii=False)
    r3 = memory.extract(life, sid, caller=FakeModel(same_other_punct))
    check(len(r3.written) == 0 and len(life.memories()) == 2,
          "只差标点和句号的同一条，也认出来了（规范化比对）")

    # 同名但内容变了 → 修正那一条，而不是新增
    revised = json.dumps([
        {"name": "研二学生", "desc": "计算机研二，导师不管，最近开始准备实习",
         "type": "user", "scope": "persistent",
         "text": "他在读研二，计算机专业，导师基本不管他；最近开始准备找实习。"},
    ], ensure_ascii=False)
    before = {m.name: m for m in life.memories()}["研二学生"]
    r4 = memory.extract(life, sid, caller=FakeModel(revised))
    after = {m.name: m for m in life.memories()}["研二学生"]
    check(len(r4.written) == 1 and len(life.memories()) == 2, "同名不同内容 → 修正，不新增")
    check(after.id == before.id, "修正的是同一条（id 没变）")
    check(after.at == before.at and after.updated_at >= before.at,
          f"第一次知道的时间没变（{after.at}），另记了修正时间（{after.updated_at}）")
    check("准备找实习" in after.text, "新内容写进去了")
    check((life.memory_dir() / f"{after.id}.md").read_text(encoding="utf-8").count("---") == 2,
          "文件仍然是合法的 front-matter 格式")

    # ── 4. 已有记忆会喂给模型（避免重复提）──
    print("\n── 提取时会带上已有的记忆 ──")
    fake2 = FakeModel("[]")
    memory.extract(life, sid, caller=fake2)
    body2 = fake2.calls[0]["messages"][0]["content"]
    check("【已有的记忆】" in body2, "带上了「已有的记忆」这一段")
    check("研二学生" in body2, "里面就是现有的目录")

    # ── 5. 召回：纯机械，不许调模型 ──
    print("\n── 召回：关键词 + 时间衰减，零模型调用 ──")
    now = int(time.time() * 1000)
    q_life = new_life("芒格")
    q_life.remember(name="导师关系", desc="他和导师处不好，考虑退出课题组",
                    type="user", text="他和导师关系紧张，一度考虑退出课题组。", at=now - 20 * DAY)
    q_life.remember(name="喜欢的城市", desc="他喜欢有河的城市", type="user",
                    text="他说过他喜欢有河的城市。", at=now - 3 * DAY)
    q_life.remember(name="读书方法", desc="他读书喜欢先看目录", type="reference",
                    text="他读书习惯先翻目录再决定读哪章。", at=now - 1 * DAY)
    got = memory.recall(q_life, question="我该不该跟导师摊牌？", now=now)
    check(bool(got), f"召回有结果（{len(got)} 条）")
    check(got[0]["name"] == "导师关系", f"最相关的那条排第一（实际 {got[0]['name']}）")
    check(all("score" in x for x in got), "每条带了分数（可解释：为什么它被想起来）")

    unrelated = memory.recall(q_life, question="今天天气不错", now=now)
    check(all(x["name"] != "导师关系" for x in unrelated),
          f"问天气就不该想起导师的事（实际召回 {[x['name'] for x in unrelated]}）")

    # 时间衰减：同样相关时，新的排前面
    q_life.remember(name="导师评价", desc="他对导师的看法", type="self",
                    text="他觉得导师不管事。", at=now - 300 * DAY)
    two = memory.recall(q_life, question="导师这个人怎么样", now=now)
    names = [x["name"] for x in two]
    check(names.index("导师关系") < names.index("导师评价"),
          f"同等相关时，新的排前面（{names}）")

    # 最近几轮用户的话也参与匹配（"那这件事怎么办"这种问题）
    got2 = memory.recall(q_life, question="那这件事怎么办？", recent="我跟导师吵架了", now=now)
    check(any(x["name"] == "导师关系" for x in got2),
          "靠最近几轮他说的话也能召回（不然「那件事」这种问法匹配不到任何东西）")

    # 上限与封顶
    for i in range(12):
        q_life.remember(name=f"导师相关{i}", desc=f"第 {i} 条和导师有关的记忆",
                        type="reference", text="导师相关的内容。" * 30, at=now - i * DAY)
    many = memory.recall(q_life, question="导师", limit=5, now=now)
    check(len(many) == 5, f"最多 5 条（实际 {len(many)}）")
    capped = memory.recall(q_life, question="导师", limit=50, max_chars=300, now=now)
    total_chars = sum(len(x["text"]) for x in capped)
    check(total_chars <= 300, f"正文总量封顶（{total_chars} ≤ 300 字符）")
    check(len(capped) < len(memory.recall(q_life, question="导师", limit=50, max_chars=100000, now=now)),
          "封顶确实起了作用")

    # ── 6. 召回路径上一个模型调用都没有 ──
    print("\n── 「不额外调用模型」要能测出来 ──")
    built = context.build_context(q_life, session_id="th_never", question="导师的事该怎么办",
                                  window=128000, output_reserve=4096, persona="你是芒格。")
    check(built.recalled >= 1, f"组装时召回到了 {built.recalled} 条")
    check("【和这次有关的旧事】" in built.system, "system 里有「和这次有关的旧事」这一档")
    check("【你记得的事】" in built.system, "目录那一档也在")
    check(built.system.index("【你记得的事】") < built.system.index("【和这次有关的旧事】"),
          "目录在前、召回的正文在后（先「你都有哪些」，再「想起了哪几条」）")
    boom = CountingModel("")
    try:
        memory.recall(q_life, question="导师", now=now)
        ok = True
    except Boom:
        ok = False
    check(ok, "recall 全程没有调用模型（把模型换成「一调用就炸」的也一样跑完）")

    # ── 7. 目录是给人读的：一行一条，正文不进去 ──
    print("\n── 目录：一行一条，不放正文 ──")
    idx = q_life.index_text()
    lines = [x for x in idx.splitlines() if x.strip()]
    check(len(lines) == len(q_life.memories()), f"{len(lines)} 行 == {len(q_life.memories())} 条记忆")
    long_body = [m for m in q_life.memories() if len(m.text) > 100][0]
    check(long_body.text[:20] not in idx,
          "正文没有漏进目录（目录只放 name + 一句话 + 类型 + 日期）")
    check(all(x.count("·") >= 2 for x in lines), f"每行格式统一：{lines[0][:50]}")
    check(len(idx) < sum(len(m.text) for m in q_life.memories()),
          "所以目录远小于正文总量——这就是「记忆几百条也不炸窗口」的关键")

    # ── 8. 删掉一条 ──
    print("\n── 用户能直接删掉一条 ──")
    victim = q_life.memories()[0]
    check(q_life.forget(victim.id), "删掉了")
    check(not (q_life.memory_dir() / f"{victim.id}.md").exists(), "文件没了")
    check(victim.name not in q_life.index_text(), "目录里也没了")
    check(not q_life.forget(victim.id), "再删一次返回 False（不报错）")

    print(f"\n{'=' * 46}\n通过 {passed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
