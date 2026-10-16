"""迁移测试：拿**真实的老数据**跑一遍，逐项验数字。

    py -3 dev/test_migrate.py

为什么这个测试值得写这么细：迁移是整个重构里唯一"错了就很难回头"的一步。
它把 15 位数字人和 23 轮对话从老结构搬成新结构，搬错了用户会直接发现
"我的数字人失忆了"。所以这里不测"函数返回了东西"，而是测**具体数字**：

    9 份蓝图 / 15 位生命 / 22 个会话 / 108 条历史 / 6 条失败答案被跳过

这些数字是我先用只读探针把老数据数过一遍记下来的，不是为了迁就实现而写的。

测四件事：
  1. 搬对了（数量、时间戳、共用蓝图、出生时间）
  2. **老目录一个字节都没变**（迁移只许读它）
  3. **能重跑**（再跑一次不会重复插入，这是"随时可以重来"的保证）
  4. **试跑说实话**（--dry-run 报的数字跟真跑一样，只是不落盘）
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import migrate, presets, store, templates  # noqa: E402
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


def tree_hash(root: Path) -> dict[str, str]:
    """目录指纹。跳过 .pid——老应用启动时会重写它，跟迁移无关，
    把它算进来会让这个测试变得看运气（老应用正好在跑就红）。"""
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.name != ".pid":
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def entry_count(dst: Path) -> int:
    return sum(
        len(store.read_jsonl(p)[0]) for p in dst.rglob("*.jsonl")
    )


REAL_SRC = Path("D:/AAsanboxForDsh/data")


def test_synthetic_council(root: Path) -> None:
    """合成一桌**会议模式**的老数据：验"听见前面人的发言"。

    真实数据里没有这种场面（唯一的会议桌只有 1 个座位），
    所以这条路径只能靠自己造——不然"heard"永远是段没跑过的代码。
    """
    print("\n── 会议模式：他听得见排在他前面的人（合成数据）──")
    src = root / "syn_old"
    (src / "threads").mkdir(parents=True)
    store.write_json(src / "settings.json", {
        "version": 7,
        "providers": [{"id": "pv_x", "name": "某家", "kind": "openai", "apiKey": "sk-测试",
                       "baseUrl": "http://127.0.0.1:1/v1", "models": []}],
        "advanced": {"maxTokens": 1234},
    })
    store.write_json(src / "humans.json", [
        {"id": "dh_A", "name": "甲", "system": "甲的人格", "emoji": "🅰️", "tags": [],
         "builtin": False, "createdAt": 1111, "providerId": "pv_x", "model": "m1",
         "temperature": 0.7},
        {"id": "dh_B", "name": "乙", "system": "乙的人格", "emoji": "🅱️", "tags": [],
         "builtin": False, "createdAt": 2222, "providerId": "pv_x", "model": "m2",
         "temperature": None},
    ])
    store.write_json(src / "threads" / "th_syn.json", {
        "id": "th_syn", "title": "合成会议", "mode": "council", "createdAt": 3333,
        "seats": [
            {"id": "st_1", "seatNo": 1, "humanId": "dh_A", "providerId": "pv_x",
             "model": "", "temperature": None},
            {"id": "st_2", "seatNo": 2, "humanId": "dh_B", "providerId": "pv_x",
             "model": "", "temperature": None},
        ],
        "rounds": [{"id": "r1", "at": 5000, "question": "问题", "answers": [
            {"seatId": "st_1", "name": "甲", "content": "甲的回答", "error": None, "ms": 100},
            {"seatId": "st_2", "name": "乙", "content": "乙的回答", "error": None, "ms": 200},
        ]}],
    })

    dst = root / "syn_new"
    rep = migrate.migrate(src, dst)
    check(rep.lives_created == 2, f"两位生命都搬过来了（实际 {rep.lives_created}）")

    life_a = Life.load(dst / "humans" / templates.stable_id("dh", "dh_A"))
    life_b = Life.load(dst / "humans" / templates.stable_id("dh", "dh_B"))
    a = life_a.read_session("th_syn", warn=False)
    b = life_b.read_session("th_syn", warn=False)

    check([e["kind"] for e in a] == ["you", "self"], f"①号座：只有「他说」和「他自己说」，实际 {[e['kind'] for e in a]}")
    check([e["kind"] for e in b] == ["you", "heard", "self"],
          f"②号座：多了「他听到①号说」（实际 {[e['kind'] for e in b]}）")
    heard = next((e for e in b if e["kind"] == "heard"), {})
    check(heard.get("who") == "甲", f"记住是谁说的：{heard.get('who')}")
    check(heard.get("text") == "甲的回答", "听到的是原话，不是摘要")
    check(a[1]["t"] == 5100 and b[2]["t"] == 5200, "各自说的时间 = 提问时间 + 各自耗时")
    check(rep.providers == 1 and store.read_json(dst / "settings.json", {})["providers"][0]["apiKey"] == "sk-测试",
          "密钥原样带过来（没被打码、没被丢）")
    check(store.read_json(dst / "settings.json", {})["advanced"]["maxTokens"] == 1234,
          "老的高级设置带过来")
    check(store.read_json(dst / "settings.json", {})["advanced"]["historyRounds"]
          == presets.DEFAULT_SETTINGS["advanced"]["historyRounds"],
          "老数据里没有的项，用新默认值补齐（而不是留空）")

    print("\n── 用户的设置不能被静默丢掉 ──")
    check(life_a.identity.get("temperature") == 0.7,
          f"甲自己设的温度 0.7 带进身份里了（实际 {life_a.identity.get('temperature')}）")
    check("temperature" not in life_b.identity,
          "乙那边是 null，所以不凭空造一个默认温度")
    check(life_a.identity["bornAt"] == 1111 and life_b.identity["bornAt"] == 2222,
          "出生时间取老数据的创建时间，而不是今天")
    check(life_a.index_path().read_bytes() == b"",
          "新建生命的 INDEX.md 是 0 字节空文件（不是 JSON 的空字符串，两个引号字符会被塞进提示词）")


def main() -> int:
    root = Path(os.environ.get("RT_MIGRATE_DIR") or Path(__file__).resolve().parent / ".tmp-migrate")
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True)
    print(f"临时目录：{root}")

    test_synthetic_council(root)

    if not REAL_SRC.exists():
        print(f"\n⚠️ 找不到老数据 {REAL_SRC}，跳过真实数据迁移测试")
        print(f"\n通过 {passed}，失败 {failed}")
        return 1 if failed else 0

    print(f"\n── 用真实老数据迁移：{REAL_SRC} ──")
    src = root / "old"
    shutil.copytree(REAL_SRC, src)  # 始终在副本上跑，不碰真身
    before = tree_hash(src)

    dst = root / "new"
    rep = migrate.migrate(src, dst)
    for line in rep.lines():
        print(f"    {line}")

    print("\n── 数量对不对 ──")
    check(rep.humans_total == 15, f"老数据里 15 位数字人（实际 {rep.humans_total}）")
    check(rep.templates_created == 9,
          f"15 位合成 9 份蓝图（实际新建 {rep.templates_created} 份）")
    check(rep.templates_reused == 6,
          f"6 位用的是别人已经建好的蓝图（另 9 位各自独一份）（实际 {rep.templates_reused}）")
    check(rep.lives_created == 15, f"15 位生命（实际 {rep.lives_created}）")
    check(len(Life.list_all(dst / "humans")) == 15, "磁盘上确实有 15 个 identity.json")
    check(len(templates.load_all(dst)) == 9, "磁盘上确实有 9 个模板文件")
    check(rep.sessions_written == 22, f"22 个会话文件（实际 {rep.sessions_written}）")
    check(rep.entries_backfilled == 108, f"108 条历史（实际 {rep.entries_backfilled}）")
    check(entry_count(dst) == 108, f"磁盘上的行数也是 108（实际 {entry_count(dst)}）")
    # 10 = 6 条调用失败（c1325 三条、f80c 三条）+ 4 个"那一轮他压根没答"的座位
    # （th_66bd3e24a63b 前两轮只有 kimi-k3 答，芒格和孙宇晨两个座位空着）。
    # 两种情况都不该编一条记录出来。
    check(rep.entries_skipped == 10,
          f"10 条没有内容的座位没被编成「他说过」（6 条失败 + 4 条没答，实际 {rep.entries_skipped}）")
    check(rep.providers == 2, "两家的密钥都带过来了")

    print("\n── 蓝图（人格模板）──")
    sun = _find_template(templates.load_all(dst), "孙宇晨")
    # 老数据里这份人格是带 HTML 转义存下来的（"行动 &gt; 思考"），
    # 迁移时按用户确认清掉：20 处 &gt; 各少 3 个字符（&gt;→>），1591 - 60 = 1531
    check(sun is not None and len(sun.system) == 1531,
          f"孙宇晨的人格清掉 HTML 转义后是 1531 字符（实际 {len(sun.system) if sun else '?'}）")
    check(sun is not None and "&gt;" not in sun.system and "行动 > 思考" in sun.system,
          "「&gt;」还原成了「>」，正文其余部分一字未动")
    check(sun is not None and rep.cleaned.get("孙宇晨") == {"&gt;": 20},
          f"报告里逐条说明了改了什么（实际 {rep.cleaned.get('孙宇晨')}）")
    check(len(rep.cleaned) == 1, f"只有这一份人格被改动（实际 {len(rep.cleaned)} 份）")
    check(sun is not None and sun.origin == "imported", "来源标成 imported，跟内置/提炼的分得清")
    soc = [t for t in templates.load_all(dst) if t.name == "苏格拉底"]
    check(len(soc) == 1, f"两位苏格拉底合成了一份蓝图（实际 {len(soc)} 份）")
    meng = [t for t in templates.load_all(dst) if t.name == "查理·芒格"]
    check(len(meng) == 2, f"两位查理·芒格的正文不同（443 / 174 字符），保留成 2 份而不是硬并成 1 份"
                          f"（实际 {len(meng)} 份：{[len(t.system) for t in meng]}）")
    shared = {k: v for k, v in rep.sharing.items() if len(v) > 1}
    check(len(shared) == 6,
          f"6 份蓝图各对应两个个体：苏格拉底、费曼、鲁迅、塔勒布、通用助手、孙宇晨"
          f"（各自一份人格 × 两家平台各建一个）（实际 {len(shared)} 组）")
    check(all(len(v) == 2 for v in shared.values()), "每组正好 2 位")

    print("\n── 生命（个体）──")
    lives = {l.identity["name"] + "/" + l.id: l for l in Life.list_all(dst / "humans")}
    check(len(lives) == 15, "15 个个体各自有身份文件")

    soc_lives = [l for l in Life.list_all(dst / "humans") if l.identity["name"] == "苏格拉底"]
    check(len({l.identity["templateId"] for l in soc_lives}) == 1,
          "两位苏格拉底指向同一个 templateId（同一份蓝图，两个个体）")
    check(len({l.identity["providerId"] for l in soc_lives}) == 2,
          "但他们绑的是不同的平台（这才是当初为什么要建两份）")

    kimi = next(l for l in Life.list_all(dst / "humans") if l.identity["name"] == "kimi-k3")
    check(kimi.identity["bornAt"] == 1789798741319,
          f"出生时间是老数据里的创建时间，不是今天（{kimi.identity['bornAt']}）")
    check(templates.load(dst, kimi.identity["templateId"]).bare is True,
          "裸模型那位仍然是「不构造 system」的裸的")
    check(templates.load(dst, kimi.identity["templateId"]).system == "",
          "裸模型的人格正文是空的——没有被塞进默认提示词")
    check("temperature" not in kimi.identity,
          "老数据里那两位的 temperature 是 null，所以身份里也不该凭空冒出一个默认温度")

    print("\n── 历史（时间戳是老的，不是今天的）──")
    kimi_sess = kimi.read_session("th_7411ecd7de34", warn=False)
    check([e["kind"] for e in kimi_sess] == ["you", "self"] * 3,
          f"三问三答交替（实际 {[e['kind'] for e in kimi_sess]}）")
    check([e["t"] for e in kimi_sess if e["kind"] == "you"]
          == [1789810377734, 1789810419913, 1789829400133],
          "提问时间 = 老数据里那一轮的 at，逐毫秒相同")
    check(all(e["t"] >= 1789810377734 for e in kimi_sess), "没有一条被打上「现在」的时间戳")
    check(all(e.get("imported") is True for e in kimi_sess), "标了 imported，跟以后真实对话分得清")
    lines = kimi.session_path("th_7411ecd7de34").read_text(encoding="utf-8").strip().split("\n")
    check(len(lines) == 6, f"6 条记录就是 6 行（JSONL 一行一条，能 tail、能 grep）（实际 {len(lines)} 行）")
    # 逐字符比对"搬过来的回答"和"老数据里的回答"：换行、Markdown 星号、emoji 一个都不能变。
    # 这比"长度差不多"强得多——提示词存的是原文，动一个字符效果就可能变。
    old_thread = json.loads((src / "threads" / "th_7411ecd7de34.json").read_text(encoding="utf-8"))
    old_text = old_thread["rounds"][1]["answers"][0]["content"]
    new_text = kimi_sess[3]["text"]
    check(new_text == old_text,
          f"回答原文逐字符相同（{new_text.count(chr(10))} 个换行、Markdown 星号、emoji 都在）")

    meng_life = Life.load(dst / "humans" / templates.stable_id("dh", "dh_42005066efca"))
    sess = meng_life.read_session("th_c1325ffc374e", warn=False)
    check([e["kind"] for e in sess] == ["you"],
          f"那轮他调用失败，于是只留下「用户问了什么」，没有伪造他说过话（实际 {[e['kind'] for e in sess]}）")
    check(not meng_life.read_session("th_db6fa4775807", warn=False),
          "一次都没说过话的桌子不建会话文件（没内容就不落空文件）")

    print("\n── 老目录：一个字节都没变 ──")
    check(tree_hash(src) == before, "迁移后老数据目录的文件指纹完全一致")

    print("\n── 重跑：不会重复插入 ──")
    rep2 = migrate.migrate(src, dst)
    check(rep2.lives_created == 0 and rep2.lives_skipped == 15,
          f"第二次跑：新建 0 位、跳过 15 位（实际 {rep2.lives_created}/{rep2.lives_skipped}）")
    check(rep2.templates_created == 0, "模板也不重复")
    check(entry_count(dst) == 108, f"磁盘上还是 108 条，没有翻倍（实际 {entry_count(dst)}）")

    print("\n── 试跑：数字要跟真跑一样，只是不落盘 ──")
    dry_dst = root / "dry"
    rep3 = migrate.migrate(src, dry_dst, dry_run=True)
    check(rep3.templates_created == 9, f"试跑报的模板数跟真跑一样（实际 {rep3.templates_created}）")
    check(rep3.lives_created == 15, "试跑报的生命数跟真跑一样")
    check(rep3.entries_backfilled == 108, f"试跑报的历史条数跟真跑一样（实际 {rep3.entries_backfilled}）")
    check(rep3.sessions_written == 22, "试跑报的会话数跟真跑一样")
    check(rep3.cleaned.get("孙宇晨") == {"&gt;": 20}, "试跑也报出会清掉哪些转义")
    check(not dry_dst.exists() and not (dry_dst / "settings.json").exists(),
          "试跑真的什么都没写（连目录都没建）")

    print("\n── 转义清理可以关掉（不想动数据的人能选）──")
    keep_dst = root / "keep"
    rep4 = migrate.migrate(src, keep_dst, fix_entities=False)
    kept = _find_template(templates.load_all(keep_dst), "孙宇晨")
    check(len(kept.system) == 1591 and "&gt;" in kept.system,
          f"关掉之后原样保留：还是 1591 字符、还是 &gt;（实际 {len(kept.system)}）")
    check(not rep4.cleaned, "报告里也不会说清理过什么")
    check(rep4.lives_created == 15 and rep4.entries_backfilled == 108,
          "只影响人格正文，历史和生命数量都不受这个开关影响")
    check(len(tree_hash(src)) == len(before), "老目录仍然一个字节都没变")

    print(f"\n{'=' * 46}\n通过 {passed}，失败 {failed}")
    return 1 if failed else 0


def _find_template(tpls, name):
    return next((t for t in tpls if t.name == name), None)


if __name__ == "__main__":
    raise SystemExit(main())
