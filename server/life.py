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


# ─────────────────────── 记忆条目（Markdown + YAML 头）───────────────────────


@dataclass
class Memory:
    """一条长期记忆。正文是可以直接读的 Markdown。"""

    id: str
    name: str
    desc: str = ""  # 一句话说明——**目录里放的是它，不是正文**
    type: str = "user"
    scope: str = "persistent"  # persistent=长期有效 | session=只在这个话题里有效
    at: int = 0  # 什么时候写下的
    source: str = ""  # 从哪次会话来的（会话 id）
    text: str = ""

    def to_markdown(self) -> str:
        head = [
            "---",
            f"name: {self.name}",
            f"desc: {self.desc}",
            f"type: {self.type}",
            f"scope: {self.scope}",
            f"at: {self.at or now_ms()}",
            f"source: {self.source}",
            "---",
            "",
            self.text.strip(),
            "",
        ]
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
    def create(humans_dir, *, template_id: str, provider_id: str, model: str, name: str, emoji: str = "🙂") -> "Life":
        """用某个模板 + 某家 API 创建出来。**这一刻起他有了自己的记忆。**"""
        hid = new_id("dh")
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
            "bornAt": now_ms(),
        }
        store.write_json(root / "identity.json", identity)
        store.write_json(root / "memory" / "INDEX.md", "")  # 占位，保持目录结构稳定
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

    def append(self, session_id: str, kind: str, **fields) -> dict:
        """往这次会话的记录里追加一条。**带时间戳**——记忆递减和时间感都靠它。"""
        entry = {"t": now_ms(), "kind": kind, **fields}
        store.append_jsonl(self.session_path(session_id), entry)
        return entry

    def read_session(self, session_id: str, warn: bool = True) -> list[dict]:
        entries, skipped = store.read_jsonl(self.session_path(session_id))
        if skipped and warn:
            print(f"[life] {self.id}/{session_id}：跳过了 {skipped} 行坏记录（多半是崩在写入中间）")
        return entries

    def sessions(self) -> list[str]:
        d = self.root / "sessions"
        return sorted((p.stem for p in d.glob("*.jsonl")), reverse=True) if d.exists() else []

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
                 scope: str = "persistent", source: str = "") -> Memory:
        """写一条记忆，并重建目录。

        去重放在上层（压缩那次调用）做；这里只保证"写完目录是新的"。
        `desc` 是目录里那"一句话"——由写入那次模型调用给，别省。
        """
        mid = new_id("m")
        mem = Memory(id=mid, name=name, desc=desc, type=type, scope=scope, at=now_ms(),
                     source=source, text=text)
        store.write_json(self.root / "identity.json", self.identity)  # 确保目录存在（首次）
        (self.memory_dir() / f"{mid}.md").write_text(mem.to_markdown(), encoding="utf-8")
        self.rebuild_index()
        return mem

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
        """所有会话的摘要条目：`{session, t, text}`。

        "记忆递减"的原料——越老的会话，越只剩这里的一行。

        TODO（等精修记忆编排时做）：现在是每次请求都把每个会话文件读一遍。
        生命活得久了这里会变慢。两条路：加一层进程内缓存（按文件 mtime 失效），
        或者把摘要单独存一个 `summaries.jsonl`。先记着，别现在优化。
        """
        out = []
        for sid in self.sessions():
            for e in self.read_session(sid, warn=False):
                if e.get("kind") == KIND_SUMMARY and sid != exclude_session:
                    out.append({"session": sid, "t": e.get("t", 0), "text": e.get("text", "")})
        return sorted(out, key=lambda x: x["t"], reverse=True)
