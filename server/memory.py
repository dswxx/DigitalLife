"""压缩与记忆：**请求路径之外的那些活**。
这是设计文档里那条硬规矩的落地点：

> 请求路径永远不调用模型来做压缩。

原因很实在：压缩要花一次模型调用（几秒），如果放在请求路径上，用户每问一句
都要多等几秒，而且那次调用还可能失败——"用户无感"就没了。所以：

    一次对话结束
        ↓  算水位：未压缩原文的 token 估算
        ↓  超过阈值（可用预算的 60%）? → 起后台任务
        │     锁住这个生命 → 压最老的一段 → 写摘要 + 推水位线 → 解锁
        ↓
    下一次对话：只读水位线之后的尾巴（字节定位，老原文根本不解析）

**这个模块不 import httpx。** 模型调用是注入进来的（见 `model.ModelReply`），
所以"什么时候压、压哪一段、压完怎么推水位线、崩了怎么办"这些真正的逻辑
全部可以用假模型离线验完，不必等网络那一层写好。

三层保障，从好到坏（设计文档里的"压缩阶梯"）：
  1. 本会话中段 → 摘要（这个模块干的）
  2. 更早的会话 → 一行（`Life.summaries()` 干的）
  3. 兜底：确定性裁剪，不需要模型参与（`context.build_context` 干的）
第 3 层永远在，所以**不会出现"压不动就发不出去"**。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from . import store
from .life import (KIND_HEARD, KIND_SELF, KIND_SUMMARY, KIND_YOU, MEM_TYPES, Life, Memory,
                   headline, norm_key)
from .model import as_reply
from .text import estimate_tokens, fmt_clock, fmt_day

# 未压缩原文占到可用预算的这个比例，就该压了。
# 设计文档定的是 60%——留 40% 的余量，是为了"前置"：等到 100% 才动手，
# 那就已经站在请求路径上了（那次请求只能靠裁剪保底）。
DEFAULT_WATERMARK = 0.6

# 压完之后，尾巴保留多少（占可用预算）。留一半原文，
# 是因为最近的对话要读起来是"活人对话"，不能全变成摘要。
DEFAULT_KEEP_RATIO = 0.5

# 少于这些就不值得花一次模型调用：太短的一段压了也省不下什么。
MIN_ENTRIES = 4
MIN_TOKENS = 600

# 一次压缩最多喂进去多少原文（防止第一压就把十万字塞给模型）
MAX_BATCH_TOKENS = 60000

# 摘要的提示词。**这是 v1 的基线，不是定稿**——"记忆内容的编排"是设计文档里
# 明确留到后面精修的部分。但框架要能跑，就得有一条基线，所以先这么写。
COMPRESS_SYSTEM = """你在帮一位数字生命整理记忆。

给你的是一段他和用户的旧对话。请把它压成一段他自己记得住的回忆。

规则：
1. **保留开头**：这段是从什么话题、什么事开始的——开头决定了整段在谈什么。
2. **保留结论和决定**：谈出了什么结论、决定了什么、答应过什么。
3. **保留具体事实**：人名、书名、时间、数字、事情的经过。不要模糊成"聊了一些事"。
4. **保留他的立场和语气**：他当时是什么态度、对什么反感或在意。
5. 丢掉寒暄、重复的话、以及用户长篇背景里与结论无关的部分。
6. 用**他本人的视角**写，像他事后回想这件事。不要写成会议纪要，不要加标题。
7. 如果里面已经有一段旧摘要，把新旧合成**一段**，不要分成两段。
8. 只写回忆本身。不要解释你在做什么，不要写"以下是摘要"。

中文，300 字以内。"""


@dataclass
class Plan:
    """该怎么压。**纯计算结果，不碰磁盘、不调模型**——所以可以直接断言。"""

    needed: bool
    reason: str
    budget: int
    raw_tokens: int
    # 要压进摘要的（较老的那一段）
    compress_entries: list[dict] = field(default_factory=list)
    # 压完仍然留作原文的（较新的那一段，一定从"用户说的"那句开始）
    keep_entries: list[dict] = field(default_factory=list)
    # 摘要要覆盖到哪个时间点（= compress_entries 最后一条的 t）
    through: int = 0
    # 被压内容的**末尾在文件里的字节位置**——水位线要用它。
    # 不能用"压缩那一刻的文件末尾"：那之后通常还有没压过的原文。
    keep_offset: int = 0

    @property
    def compress_tokens(self) -> int:
        return sum(estimate_tokens(e.get("text", "")) for e in self.compress_entries)

    @property
    def keep_tokens(self) -> int:
        return sum(estimate_tokens(e.get("text", "")) for e in self.keep_entries)


def plan(life: Life, session_id: str, *, budget: int, watermark: float = DEFAULT_WATERMARK,
         keep_ratio: float = DEFAULT_KEEP_RATIO) -> Plan:
    """该不该压、压哪一段。**只读，不改任何东西。**

    `budget` 是这次会话能给历史用的额度（`context.build_context` 算出来的那个）。
    """
    if budget <= 0:
        return Plan(False, "没有可用预算（窗口太小），交给裁剪保底", budget, 0)

    prev = life.compaction(session_id)
    # 已经压过的部分不重复压：只看水位线之后的原文
    spans = life.read_session_spans(session_id, only_tail=bool(prev.get("summary")))
    pairs = [(at, e) for at, e in spans if e.get("kind") != KIND_SUMMARY]
    entries = [e for _, e in pairs]
    raw_tokens = sum(estimate_tokens(e.get("text", "")) for e in entries)

    if raw_tokens <= budget * watermark:
        pct = round(raw_tokens / budget * 100) if budget else 0
        return Plan(False, f"水位没到（原文 {raw_tokens} / 预算 {budget}，{pct}%）", budget, raw_tokens)
    if len(entries) < MIN_ENTRIES + 2:
        return Plan(False, f"只有 {len(entries)} 条，压了也留不下什么", budget, raw_tokens)

    # 从最新往回数，留够 keep_ratio 那一段
    keep_goal = budget * keep_ratio
    cut = len(entries)  # 切点：entries[:cut] 进摘要，entries[cut:] 留原文
    kept = 0
    while cut > 0 and kept < keep_goal:
        cut -= 1
        kept += estimate_tokens(entries[cut].get("text", ""))
    # **切点必须落在一整句话的开始**：如果尾巴的第一条是"他自己说的"，
    # 那它前面那句用户发言已经进了摘要，这条就成了一句接不上的话。
    # 往后挪到下一个"用户说的"（或"听到的"）为止。
    cut = _snap_to_turn_start(entries, cut)
    if cut <= 0 or cut >= len(entries):
        return Plan(False, "切不出干净的两段（留不出完整的一来一回）", budget, raw_tokens)

    compress_entries = entries[:cut]
    keep_entries = entries[cut:]
    if sum(estimate_tokens(e.get("text", "")) for e in compress_entries) < MIN_TOKENS:
        return Plan(False, "要压的那段太短，不值得", budget, raw_tokens)
    if sum(estimate_tokens(e.get("text", "")) for e in keep_entries) < 1:
        return Plan(False, "压完就什么都不剩了", budget, raw_tokens)

    return Plan(
        needed=True,
        reason=f"原文 {raw_tokens} / 预算 {budget}（超过 {int(watermark * 100)}% 的水位线）",
        budget=budget,
        raw_tokens=raw_tokens,
        compress_entries=compress_entries,
        keep_entries=keep_entries,
        through=int(compress_entries[-1].get("t") or 0),
        keep_offset=int(pairs[cut][0]),
    )


def _snap_to_turn_start(entries: list[dict], cut: int) -> int:
    """把切点挪到"一整句话的开始"：往后找到最近的 user 侧条目。

    往后挪（而不是往前）是为了保证**尾巴至少有一整句用户发言**——
    往前挪的话尾巴可能变成一条孤立的助手发言，那比少留一句更糟。
    找不到就返回一个越界值，让调用方放弃这次压缩。
    """
    for i in range(cut, len(entries)):
        if entries[i].get("kind") in (KIND_YOU, KIND_HEARD):
            return i
    return len(entries)


def transcript(entries: list[dict], *, who: str = "他") -> str:
    """把一段记录排成给人（模型）读的样子。**带上时间戳**——记忆的年龄靠它。"""
    lines = []
    for e in entries:
        clock = f"{fmt_day(e['t'])} {fmt_clock(e['t'])}" if e.get("t") else "很早以前"
        kind = e.get("kind")
        if kind == KIND_YOU:
            speaker = "用户"
        elif kind == KIND_HEARD:
            speaker = f"{e.get('who') or '另一个人'}（另一个人）"
        else:
            speaker = who
        lines.append(f"[{clock}] {speaker}：{e.get('text', '').strip()}")
    return "\n\n".join(lines)


def compress(life: Life, session_id: str, *, caller, budget: int, model: str = "",
             watermark: float = DEFAULT_WATERMARK, keep_ratio: float = DEFAULT_KEEP_RATIO) -> dict | None:
    """压一次：算出该压哪段 → 调一次模型 → 推水位线。返回新的压缩状态（没压返回 None）。

    `caller` 是注入进来的模型调用，签名 `caller(system=..., messages=[...]) -> ModelReply | str`。
    真实实现是 `adapters` 里的那两个；测试里是一个假模型。

    要不要压**只由 `plan` 决定**，没有"强制压一次"的开关：
    没人用的开关会烂在那里，而且评审时看不出它有没有被验过。

    **整个过程锁住这个生命**：压缩在后台跑，而用户随时可能发下一句，
    两边都在改同一个会话文件。单进程应用，进程内锁就够（`store.lock_for`）。
    """
    with store.lock_for(life.id):
        p = plan(life, session_id, budget=budget, watermark=watermark, keep_ratio=keep_ratio)
        if not p.needed:
            return None

        # 压过就把旧摘要一起带上：摘要**滚动合成一段**，而不是越压越多段。
        # 带多段摘要进提示词的话，同一个生命的过去会分成好几层叙述，
        # 既占窗口又容易自相矛盾。
        old = life.summary_text(session_id)
        batch = _cap(p.compress_entries)
        body = transcript(batch, who=life.identity.get("name") or "他")
        if old:
            body = f"【之前的摘要】\n{old}\n\n【之后的对话】\n{body}"
        messages = [{"role": "user", "content": body}]

        reply = as_reply(caller(system=COMPRESS_SYSTEM, messages=messages))
        summary = (reply.text or "").strip()
        if not summary:
            return None  # 空摘要不推水位线：宁可下次再压，也不能把一段历史压没了

        prev_times = int(life.compaction(session_id).get("times") or 0)
        return life.advance_compaction(
            session_id,
            summary=summary,
            through=p.through,
            offset=p.keep_offset,
            times=prev_times + 1,
            model=model or reply.model,
            usage=reply.usage,
        )


def _cap(entries: list[dict]) -> list[dict]:
    """一次最多喂这么多，从**老的那头**开始截（要压的是老的一段，近的那头留在原文里）。

    注意：如果**单条**就超过上限（用户一上来粘贴了一份十万字的文档），
    仍然会把这一条整条发出去——那正是最该压的情况。代价是这次压缩调用可能
    因为超长而失败（那就什么也不写，水位线不动，下次再试）。
    分块摘要是以后的活，先记在文档的待办里。
    """
    out: list[dict] = []
    spent = 0
    for e in entries:
        cost = estimate_tokens(e.get("text", ""))
        if spent + cost > MAX_BATCH_TOKENS:
            break
        spent += cost
        out.append(e)
    return out or entries[:1]


def pressure(life: Life, session_id: str, *, budget: int) -> float:
    """水位百分比（0.0~）。给界面用：让人看得见"这位生命快该整理了"。"""
    if budget <= 0:
        return 0.0
    entries = [e for e in life.read_session(session_id, only_tail=True, warn=False)
               if e.get("kind") != KIND_SUMMARY]
    raw = sum(estimate_tokens(e.get("text", "")) for e in entries)
    return round(raw / budget, 3)


def status_line(life: Life, session_id: str) -> str:
    """一行话描述这次会话的压缩状态。给"看得到但不用知道"那个原则用。"""
    m = life.compaction(session_id)
    if not m.get("summary"):
        return "还没整理过"
    when = time.strftime("%m-%d %H:%M", time.localtime(int(m.get("updatedAt") or 0) / 1000))
    return (f"整理过 {m.get('times', 1)} 次，最近一次 {when}，"
            f"压到 {fmt_day(int(m.get('compactedThrough') or 0))}；"
            f"一句话：{headline(m.get('summary') or '')}")


# ═══════════════════════ 长期记忆：提取 ═══════════════════════
#
# 设计文档里的写入路径：
#
#     会话结束 / 或一次会话聊得太长
#        ↓  让模型从最近的对话里提候选记忆
#        ↓  逐条校验：是不是长期的？重复吗？只是这次的临时要求吗？
#        ↓  写 memory/m_*.md，重建 INDEX.md
#
# 触发时机跟压缩是**同一件事**（设计文档："锁住这个生命 → 压最老的一段 →
# 写摘要和记忆条目 → 推进水位 → 解锁"）。所以这里不自己找时机，
# 由调用方把"刚压过的那一段"递进来。
#
# 为什么不在请求路径上提取：一轮问答多一次模型调用，成本直接翻倍。
# 而**原始记录永不丢**，所以提取随时可以重跑——这个性质让"少做几次"是安全的。

# 一次最多提取几条。设计文档：宁可少而准。
MAX_PER_EXTRACT = 5

# 提取的提示词。**v1 基线，不是定稿**（记忆内容的编排是留给后面精修的部分）。
EXTRACT_SYSTEM = """你在帮一位数字生命把对话里值得长期记住的东西挑出来。

请从给你的对话里挑出**他以后还用得上**的信息，按 JSON 输出。

值得记的：
- 用户的稳定事实：身份、处境、长期目标、在意什么、反复出现的困扰
- 用户明确表达过的偏好和要求（比如"别绕弯子""别给我列表""叫我小名"）
- 这位生命自己下过的判断、给过的结论、答应过的事
- 关于具体人事物的关键事实：人名、书名、时间、数字、事情的经过

不值得记的（一律不要）：
- 只对这一次有效的临时要求
- 寒暄、客套、他这一次的具体措辞
- 【已有的记忆】里已经有的内容

输出 JSON 数组。每项这些字段：
  "name"   短标题，不超过 12 字，一眼看出是什么事
  "desc"   一句话说明。**它会被放进记忆目录，所以必须能独立读懂**（别写"上文提到的事"）
  "type"   user（关于用户的事）| self（他自己的判断）| feedback（用户的偏好要求）| reference（外部事实）
  "text"   正文，把这件事写清楚，可以带时间点。不超过 200 字
  "scope"  persistent（长期有效）| session（只在这个话题里有效）

最多 5 条。**宁可少而准，不要多而杂。**只输出 JSON 数组本身，不要解释，不要 markdown 代码块。"""


@dataclass
class ExtractResult:
    """提了一次的结果。**跳过了什么也要看得见**——否则用户会以为模型忘了。"""

    written: list[Memory] = field(default_factory=list)
    skipped_duplicate: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # 不合规的候选（缺字段、类型不认识…）
    called: bool = False
    usage: dict = field(default_factory=dict)
    raw: str = ""  # 模型原话，解析失败时用来查

    def lines(self) -> list[str]:
        out = [f"记住 {len(self.written)} 条"]
        for m in self.written:
            out.append(f"  + {m.name}：{m.desc}")
        if self.skipped_duplicate:
            out.append(f"已经记过（跳过）：{'、'.join(self.skipped_duplicate)}")
        if self.rejected:
            out.append(f"不合规丢掉：{'、'.join(self.rejected)}")
        return out


def parse_candidates(text: str) -> tuple[list[dict], str]:
    """从模型的回话里抠出候选记忆。

    返回 `(候选列表, 问题说明)`。**模型输出是整套系统里最不可靠的输入**，
    所以这里不假设它守规矩：可能包着 ```json 代码块、前面有一句"好的"、
    也可能整个就是个对象而不是数组。抠不出来就返回空并说明原因，
    绝不"尽力猜"——猜错的记忆会一直留在他的记忆里。
    """
    if not (text or "").strip():
        return [], "模型什么也没返回"
    raw = text.strip()
    # 去掉 markdown 代码块围栏
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, re.S)
    if fence:
        raw = fence.group(1).strip()
    # 从第一个 [ 或 { 开始，到最后一个 ] 或 } 结束
    start = min([i for i in (raw.find("["), raw.find("{")) if i >= 0], default=-1)
    if start < 0:
        return [], "回话里没有 JSON"
    end = max(raw.rfind("]"), raw.rfind("}"))
    if end <= start:
        return [], "JSON 没写完（截断了）"
    try:
        data = json.loads(raw[start:end + 1])
    except json.JSONDecodeError as e:
        return [], f"JSON 解析失败：{e}"
    if isinstance(data, dict):
        # 常见包装：{"memories": [...]} / {"记忆": [...]}
        for key in ("memories", "记忆", "items", "entries", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = [data]  # 单条
    if not isinstance(data, list):
        return [], "JSON 不是数组"
    return [x for x in data if isinstance(x, dict)], ""


def _clean(cand: dict) -> dict | None:
    """校验一条候选。**逐条校验**是设计文档里的原话，不是可选项。"""
    name = str(cand.get("name") or "").strip()[:24]
    text = str(cand.get("text") or "").strip()
    desc = str(cand.get("desc") or "").strip()[:120]
    if not name or not text:
        return None
    type_ = str(cand.get("type") or "user").strip()
    if type_ not in MEM_TYPES:
        type_ = "user"  # 不认识的类型当成"关于用户的事"，不为了这个丢掉整条
    scope = str(cand.get("scope") or "persistent").strip()
    if scope not in ("persistent", "session"):
        scope = "persistent"
    if not desc:
        # desc 是目录里那一行，缺了目录就没法读。退而求其次取正文首句，
        # 但**只在模型没给的时候**——正常情况必须让它自己写。
        desc = text.splitlines()[0][:80]
    return {"name": name, "desc": desc, "type": type_, "scope": scope, "text": text}


def extract(life: Life, session_id: str, *, caller, entries: list[dict] | None = None,
            model: str = "", limit: int = MAX_PER_EXTRACT) -> ExtractResult:
    """从一段对话里提出长期记忆并写盘。

    `entries` 是要提的那一段（通常就是刚压过的那一批）。不给就用这次会话
    还没压过的尾巴——"会话结束"那个触发点走这条路。

    去重（设计文档："重复提取由记忆去重（name + 规范化文本比对）收敛"）：
      · 名字规范化后一样、正文也一样  → 跳过（什么都没变）
      · 名字规范化后一样、正文不一样  → **修正那一条**（保留第一次知道的时间）
      · 正文规范化后一样、名字不同    → 跳过（同一件事换个说法）
      · 都不是                        → 新写一条
    """
    res = ExtractResult()
    if entries is None:
        entries = [e for e in life.read_session(session_id, only_tail=True, warn=False)
                   if e.get("kind") != KIND_SUMMARY]
    entries = [e for e in entries if e.get("kind") != KIND_SUMMARY and (e.get("text") or "").strip()]
    if not entries:
        return res

    index = life.index_text().strip()
    body = transcript(entries, who=life.identity.get("name") or "他")
    head = f"【已有的记忆】\n{index}\n\n" if index else ""
    reply = as_reply(caller(system=EXTRACT_SYSTEM,
                            messages=[{"role": "user", "content": f"{head}【对话】\n{body}"}]))
    res.called = True
    res.usage = reply.usage
    res.raw = reply.text or ""

    cands, why = parse_candidates(reply.text)
    if why:
        res.rejected.append(why)
        return res
    learned_at = int(entries[-1].get("t") or 0)
    if len(cands) > limit:
        # 模型给了太多就按顺序取前几条（顺序是它自己的优先级），并说明截断了
        res.rejected.append(f"给了 {len(cands)} 条，只取前 {limit} 条")
        cands = cands[:limit]

    for cand in cands:
        item = _clean(cand)
        if item is None:
            res.rejected.append(str(cand.get("name") or cand.get("desc") or "（没有名字）")[:24])
            continue
        same_name = life.memory_named(item["name"])
        if same_name is not None:
            if norm_key(same_name.text) == norm_key(item["text"]):
                res.skipped_duplicate.append(item["name"])
                continue
            res.written.append(life.remember(source=session_id, mid=same_name.id, **item))
            continue
        twin = next((m for m in life.memories() if norm_key(m.text) == norm_key(item["text"])), None)
        if twin is not None:
            res.skipped_duplicate.append(item["name"])
            continue
        # `at` 用**那段对话的时间**，不是"现在"——从旧对话重算时，
        # 记忆的年龄必须是它真正发生的时间，否则时间衰减就失效了。
        res.written.append(life.remember(source=session_id, at=learned_at, **item))
    return res


# ═══════════════════════ 长期记忆：召回 ═══════════════════════
#
# 设计文档定的第一版：**纯机械检索，不额外调用模型**。
#
#     本轮问题 + 最近几轮你的话
#        ↓  和记忆的 name / 一句话做关键词匹配
#        ↓  按时间衰减加权（越新越优先）
#        ↓  取前若干条，加载正文（总量封顶）
#
# 为什么不让模型挑：多一次调用就是每次对话都多花钱、多等一次。
# 机械检索可解释、可预测、零额外成本。等记忆多到关键词不够用，再升级成
# "把目录给模型、让它挑 id"。**不做向量检索**（要引依赖）。

RECALL_LIMIT = 5
RECALL_MAX_CHARS = 20000  # 正文总量封顶
RECALL_HALF_LIFE_DAYS = 45.0  # 时间衰减的半衰期：45 天前的记忆权重减半


def _bigrams(s: str) -> set[str]:
    """粗粒度"关键词"：相邻两字一组。

    中文没有空格，不引分词库就只能这样。两个字一组对中文够用：
    "辞职写作" 和 "打算辞职去写作" 会共享「辞职」「辞写」这几个组。
    英文和数字按空白切开（"deepseek" 这种整词命中才有意义）。
    """
    s = norm_key(s)
    grams = {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())
    for word in re.findall(r"[a-z0-9]{2,}", (s or "")):
        grams.add(word)
    return grams


def recall(life: Life, *, question: str, recent: str = "", limit: int = RECALL_LIMIT,
           max_chars: int = RECALL_MAX_CHARS, half_life_days: float = RECALL_HALF_LIFE_DAYS,
           now: int | None = None) -> list[dict]:
    """从长期记忆里挑出"和这一次有关的"。

    打分 = 关键词重合度 + 时间衰减。**权重偏向关键词**：一个三十天前但正相关的
    记忆，比昨天一条无关的更该被想起来。时间衰减只用来给同等相关度分先后。
    """
    mems = life.memories()
    if not mems:
        return []
    now = now or int(time.time() * 1000)
    qgrams = _bigrams(f"{question}\n{recent}")
    if not qgrams:
        return []
    scored: list[tuple[float, Memory]] = []
    for m in mems:
        # 按设计文档，只用目录里那两样（name + 一句话）打分
        overlap = len(qgrams & _bigrams(f"{m.name} {m.desc}"))
        if overlap == 0:
            continue
        # 年龄按**最近一次修正**算：一条记忆最近还被提到、被修正过，说明它现在还活着，
        # 不该因为"最早是三年前知道的"就被时间衰减压下去。
        # （目录里显示的是 `at`——那是"他什么时候知道这件事的"，是人读的时候想知道的那个日期。）
        age_days = max(0.0, (now - (m.updated_at or m.at or now)) / 86400000)
        recency = 0.5 ** (age_days / half_life_days)  # 1.0 → 0.5 → 0.25…
        scored.append((overlap + recency, m))
    scored.sort(key=lambda x: (-x[0], -(x[1].updated_at or x[1].at)))

    out: list[dict] = []
    chars = 0
    for score, m in scored[:limit]:
        if chars + len(m.text) > max_chars:
            break
        chars += len(m.text)
        out.append({"id": m.id, "name": m.name, "desc": m.desc, "type": m.type,
                    "at": m.at, "text": m.text, "score": round(score, 2)})
    return out
