"""一个数字生命：身份 + 对话记录 + 长期记忆。

和老项目最根本的区别：**记忆属于这个生命，不属于任何一次会话。**

    data/
    ├── templates/<模板>.json           人格蓝图（不绑厂商、没有记忆）
    └── humans/<生命>/
        ├── identity.json               身份：来自哪个模板、绑哪家 API
        ├── memory/                     长期记忆：索引 + 正文分离
        │   ├── INDEX.md                目录，一条一行
        │   └── m_xxx.md                一条记忆（YAML 头 + Markdown 正文）
        └── sessions/<会话>.jsonl        对话记录：他在那次会话里的亲历

两个格式是有意区分的：
  · 对话记录用 JSONL  —— 机器日志：只追加、能 grep、能 tail、崩了不丢前面的
  · 长期记忆用 Markdown + YAML 头 —— 人要读要改的东西：他记错了你能直接删掉那一条

**这层是纯标准库**，不依赖 FastAPI。HTTP 只是套在它外面的壳。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import store

# 对话记录里一行可以是这几种
KIND_YOU = "you"  # 用户说的
KIND_SELF = "self"  # 他自己说的
KIND_HEARD = "heard"  # 会议里他听到别人说的
KIND_SUMMARY = "summary"  # 这一段会话被压成的摘要

# 长期记忆的类型（借 learn-claude-code 的四分类）
MEM_TYPES = ("user", "self", "feedback", "reference")

_ID_SAFE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff-]+")


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    """短 id：时间戳(base36) + 随机尾巴，肉眼看得出先后，也不会撞。"""
    import secrets

    n = now_ms()
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    s = ""
    while n:
        n, r = divmod(n, 36)
        s = alphabet[r] + s
    return f"{prefix}_{s}_{secrets.token_hex(3)}"


_NORM = re.compile(r"[\s\W_]+")


def norm_key(s: str) -> str:
    """规范化后用来比对：去掉空白和标点，统一小写。

    中文在 Unicode 下算"词字符"，所以 `\\W` 去掉的正是标点和空格，不会误伤汉字。
    记忆去重要靠它："用户不喜欢被列清单" 和 "用户不喜欢被列清单。" 是同一条。
    """
    return _NORM.sub("", (s or "")).lower()


def headline(text: str, limit: int = 80) -> str:
    """取一段文字的"一行版"：第一行非空内容，超长就截断。

    用途是"今天更早的对话"那一档——那里只该有一行。
    这是**临时做法**：真正的"一句话摘要"应该由压缩那次调用给（就像记忆条目的
    `desc` 那样），等精修记忆编排时换掉。现在先保证它一定只有一行。
    """
    for line in (text or "").splitlines():
        line = line.strip().lstrip("-·* ").strip()
        if line:
            return line[:limit] + ("…" if len(line) > limit else "")
    return ""


# ─────────────────────── 记忆条目（Markdown + YAML 头）───────────────────────


@dataclass
class Memory:
    """一条长期记忆。正文是可以直接读的 Markdown。"""

    id: str
    name: str
    desc: str = ""  # 一句话说明——**目录里放的是它，不是正文**
    type: str = "user"
    scope: str = "persistent"  # persistent=长期有效 | session=只在这个话题里有效
    at: int = 0  # 什么时候写下的（第一次知道这件事的时间）
    source: str = ""  # 从哪次会话来的（会话 id）
    text: str = ""
    updated_at: int = 0  # 后来修正过的时间（0 = 从没改过）

    def to_markdown(self) -> str:
        head = [
            "---",
            f"name: {self.name}",
            f"desc: {self.desc}",
            f"type: {self.type}",
            f"scope: {self.scope}",
            f"at: {self.at or now_ms()}",
            f"source: {self.source}",
        ]
        if self.updated_at:
            head.append(f"updatedAt: {self.updated_at}")
        head += ["---", "", self.text.strip(), ""]
        return "\n".join(head)

    @staticmethod
    def from_markdown(mid: str, raw: str) -> "Memory":
        """解析 front-matter。**不引 PyYAML**——只认 `key: value`，够用且零依赖。"""
        meta: dict[str, str] = {}
        body = raw
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) >= 3:
                for line in parts[1].strip().splitlines():
                    if ":" in line:
                        k, v = line.split(":", 1)
                        meta[k.strip()] = v.strip()
                body = parts[2]
        return Memory(
            id=mid,
            name=meta.get("name", mid),
            desc=meta.get("desc", ""),
            type=meta.get("type", "user"),
            scope=meta.get("scope", "persistent"),
            at=int(meta.get("at") or 0),
            source=meta.get("source", ""),
            text=body.strip(),
            updated_at=int(meta.get("updatedAt") or 0),
        )

    def index_line(self) -> str:
        """目录里的一行。

        **只放 name + 一句话说明 + 时间，不放正文**——这是"记忆攒到几百条也不炸窗口"
        的关键。摘要由写入那次模型调用给（`desc`），不要拿正文首行充数：
        正文首行常常是细节，读起来既不像摘要，也会把窗口吃掉。
        """
        when = time.strftime("%m-%d", time.localtime(self.at / 1000)) if self.at else "—"
        tail = f" · {self.desc}" if self.desc else ""
        return f"- {self.name}{tail} · {self.type} · {when}"


# ─────────────────────────────── 生命 ───────────────────────────────


@dataclass
class Life:
    """一个活着的个体。"""

    id: str
    root: Path
    identity: dict = field(default_factory=dict)

    # ── 打开 / 创建 ──
    @staticmethod
    def create(humans_dir, *, template_id: str, provider_id: str, model: str, name: str,
               emoji: str = "🙂", life_id: str | None = None, born_at: int | None = None) -> "Life":
        """用某个模板 + 某家 API 创建出来。**这一刻起他有了自己的记忆。**

        `life_id` / `born_at` 只在迁移时给：老数据里他是哪天出生的、他叫什么 id，
        都由老数据说了算，不能在这里重新发一个——否则重跑迁移就会再造一批新生命，
        而"出生时间"会集体变成今天。
        """
        hid = life_id or new_id("dh")
        root = Path(humans_dir) / hid
        (root / "memory").mkdir(parents=True, exist_ok=True)
        (root / "sessions").mkdir(parents=True, exist_ok=True)
        identity = {
            "id": hid,
            "templateId": template_id,
            "providerId": provider_id,
            "model": model,
            "name": name,
            "emoji": emoji,
            "bornAt": born_at or now_ms(),
        }
        store.write_json(root / "identity.json", identity)
        # 占位，保持目录结构稳定。**用 write_text 而不是 write_json**——
        # write_json 会把空字符串写成 `""`（两个引号字符），
        # 而 index_text() 会把它原样塞进系统提示词里。
        idx = root / "memory" / "INDEX.md"
        if not idx.exists():
            idx.write_text("", encoding="utf-8")
        return Life(id=hid, root=root, identity=identity)

    @staticmethod
    def load(root) -> "Life":
        root = Path(root)
        identity = store.read_json(root / "identity.json", {})
        return Life(id=identity.get("id") or root.name, root=root, identity=identity)

    @staticmethod
    def list_all(humans_dir) -> list["Life"]:
        base = Path(humans_dir)
        if not base.exists():
            return []
        return [Life.load(d) for d in sorted(base.iterdir()) if (d / "identity.json").exists()]

    # ── 对话记录（JSONL，只追加）──
    def session_path(self, session_id: str) -> Path:
        return self.root / "sessions" / f"{_ID_SAFE.sub('_', session_id)}.jsonl"

    def meta_path(self, session_id: str) -> Path:
        return self.root / "sessions" / f"{_ID_SAFE.sub('_', session_id)}.meta.json"

    def session_size(self, session_id: str) -> int:
        p = self.session_path(session_id)
        return p.stat().st_size if p.exists() else 0

    def append(self, session_id: str, kind: str, **fields) -> dict:
        """往这次会话的记录里追加一条。**带时间戳**——记忆递减和时间感都靠它。"""
        entry = {"t": now_ms(), "kind": kind, **fields}
        store.append_jsonl(self.session_path(session_id), entry)
        return entry

    def read_session(self, session_id: str, warn: bool = True, only_tail: bool = False) -> list[dict]:
        """读这次会话的记录。

        `only_tail=True` 只读水位线之后的**未压缩原文**（请求路径用这个）。
        更老的部分已经压成摘要了，没必要再解析一遍——生命活得越久，
        这个区别越明显。要读全部（记忆编辑器、"一生"时间线、重算摘要）就用默认值。
        """
        offset = self.compacted_offset(session_id) if only_tail else 0
        entries, skipped = store.read_jsonl(self.session_path(session_id), offset=offset)
        if skipped and warn:
            print(f"[life] {self.id}/{session_id}：跳过了 {skipped} 行坏记录（多半是崩在写入中间）")
        return entries

    def read_session_spans(self, session_id: str, only_tail: bool = False) -> list[tuple[int, dict]]:
        """和 `read_session` 一样，但每条带上它在文件里的**起始字节位置**。

        压缩要用：水位线得落在"被压内容的末尾"那个字节上。
        """
        offset = self.compacted_offset(session_id) if only_tail else 0
        spans, _ = store.read_jsonl_spans(self.session_path(session_id), offset=offset)
        return spans

    def sessions(self) -> list[str]:
        d = self.root / "sessions"
        return sorted((p.stem for p in d.glob("*.jsonl")), reverse=True) if d.exists() else []

    # ── 水位线：已经压成摘要的那一段 ──
    def compaction(self, session_id: str) -> dict:
        """这次会话的压缩状态：`{summary, compactedThrough, compactedOffset, times, usage, model}`。

        没有压过就是空字典。
        """
        return store.read_json(self.meta_path(session_id), {})

    def compacted_offset(self, session_id: str) -> int:
        """水位线的字节位置。从这里往后读就是"还没压过的尾巴"。"""
        return int(self.compaction(session_id).get("compactedOffset") or 0)

    def summary_text(self, session_id: str) -> str:
        """这次会话较早部分压出来的摘要（没有就是空串）。"""
        return (self.compaction(session_id).get("summary") or "").strip()

    def advance_compaction(self, session_id: str, *, summary: str, through: int, offset: int,
                           times: int = 1, model: str = "", usage: dict | None = None) -> dict:
        """压完一段：往记录里追加一条 `summary`（留痕），再原子地推进水位线。

        `offset` 是**被压内容的末尾在文件里的字节位置**（由 `memory.plan` 给出），
        不是当时的文件末尾——那之后通常还有没压过的原文。

        顺序是有意的——**先留痕，再推水位线**。中间崩了最多是记录里多一条重复的摘要
        （摘要本来就是可以重算的派生物），而不会出现"水位线说压过了、记录里却没有"。
        """
        entry = self.append(
            session_id, KIND_SUMMARY, text=summary, through=int(through),
            throughOffset=int(offset), model=model, usage=usage or {},
        )
        meta = {
            "sessionId": session_id,
            "summary": summary,
            "compactedThrough": int(through),
            "compactedOffset": int(offset),
            "times": times,
            "model": model,
            "usage": usage or {},
            "updatedAt": now_ms(),
        }
        prev = self.compaction(session_id)
        meta["totalUsage"] = {
            "promptTokens": int(prev.get("totalUsage", {}).get("promptTokens", 0))
            + int((usage or {}).get("promptTokens", 0)),
            "completionTokens": int(prev.get("totalUsage", {}).get("completionTokens", 0))
            + int((usage or {}).get("completionTokens", 0)),
        }
        store.write_json(self.meta_path(session_id), meta)
        return meta

    # ── 长期记忆 ──
    def memory_dir(self) -> Path:
        return self.root / "memory"

    def memories(self) -> list[Memory]:
        d = self.memory_dir()
        out = []
        for p in sorted(d.glob("m_*.md")):
            out.append(Memory.from_markdown(p.stem, p.read_text(encoding="utf-8")))
        return sorted(out, key=lambda m: m.at)

    def remember(self, *, name: str, text: str, desc: str = "", type: str = "user",
                 scope: str = "persistent", source: str = "", mid: str | None = None,
                 at: int = 0) -> Memory:
        """写一条记忆，并重建目录。

        `mid` 给上就是**修正已有的一条**（去重时走这条路）：保留原来的 `at`
        （第一次知道这件事的时间）不动，另记 `updated_at`（什么时候改的）。
        所以"他现在怎么想"和"他什么时候开始这么想"不会混在一起。

        `at` 是"他什么时候知道这件事的"。从旧对话里重算记忆时要用**那段对话的时间**，
        不能用"现在"——否则所有记忆都会变成"今天刚知道的"，时间衰减就失效了。

        去重的**判断**放在上层（`server/memory.py` 的提取逻辑）做；
        这里只保证"写完目录是新的"，以及"写记忆只有一个入口"。
        """
        if mid:
            old_path = self.memory_dir() / f"{mid}.md"
            old = Memory.from_markdown(mid, old_path.read_text(encoding="utf-8")) if old_path.exists() else None
            mem = Memory(id=mid, name=name, desc=desc, type=type, scope=scope,
                         at=(old.at if old else (at or now_ms())),
                         source=source or (old.source if old else ""),
                         text=text, updated_at=now_ms())
        else:
            mid = new_id("m")
            mem = Memory(id=mid, name=name, desc=desc, type=type, scope=scope,
                         at=at or now_ms(), source=source, text=text)
        self.memory_dir().mkdir(parents=True, exist_ok=True)
        (self.memory_dir() / f"{mid}.md").write_text(mem.to_markdown(), encoding="utf-8")
        self.rebuild_index()
        return mem

    def memory_named(self, name: str) -> Memory | None:
        """按名字找一条（去重要用）。两边都做规范化后再比。"""
        wanted = norm_key(name)
        for m in self.memories():
            if norm_key(m.name) == wanted:
                return m
        return None

    def forget(self, mid: str) -> bool:
        """删掉一条记忆。用户在记忆编辑器里点删除时走这里。"""
        p = self.memory_dir() / f"{mid}.md"
        if not p.exists():
            return False
        p.unlink()
        self.rebuild_index()
        return True

    def index_path(self) -> Path:
        return self.memory_dir() / "INDEX.md"

    def index_text(self) -> str:
        """目录：只放一行行的摘要，不放正文——**这是"记忆几百条也不炸窗口"的关键**。"""
        p = self.index_path()
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def rebuild_index(self) -> None:
        lines = [m.index_line() for m in self.memories()]
        self.index_path().write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    # ── 供 Context 组装用 ──
    def summaries(self, *, exclude_session: str | None = None) -> list[dict]:
        """其他会话各压出来的一句话：`{session, t, text}`。

        "记忆递减"的原料——越老的会话，越只剩这里的一行。

        **读 meta，不读原文**：这里以前是把每个会话文件都解析一遍（原地的 TODO）。
        生命活得久了那就是每次请求都要重读几十个文件。水位线进了 meta 之后，
        这里只读几十个几百字节的小文件，而且只用读带 meta 的那些。
        """
        d = self.root / "sessions"
        if not d.exists():
            return []
        out = []
        for p in d.glob("*.meta.json"):
            sid = p.name[: -len(".meta.json")]
            if sid == exclude_session:
                continue
            m = store.read_json(p, {})
            text = headline(m.get("summary") or "")
            if text:
                out.append({"session": sid, "t": int(m.get("compactedThrough") or 0), "text": text})
        return sorted(out, key=lambda x: x["t"], reverse=True)
