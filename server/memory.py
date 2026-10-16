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

import time
from dataclasses import dataclass, field

from . import store
from .context import estimate_tokens, fmt_clock, fmt_day
from .life import KIND_HEARD, KIND_SELF, KIND_SUMMARY, KIND_YOU, Life, headline
from .model import as_reply

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
