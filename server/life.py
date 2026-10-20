"""数字实体的身份与唯一一份个人历史。记录按需加载，在运行期间复用。"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import store


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


@dataclass
class Life:
    """一个独立实体：identity 是轻量身份，_history 首次需要时才读入。

    sessionId 只标记个人经历来源，不决定文件位置；全部经历连续存于 history.jsonl。
    active_rounds 仅存在于进程内，供上下文排除仍在作答的其他轮次。
    """
    id: str
    root: Path
    identity: dict = field(default_factory=dict)
    _history: list | None = field(default=None, repr=False)
    active_rounds: set = field(default_factory=set, repr=False)

    @staticmethod
    def create(humans_dir, *, template_id: str, provider_id: str, model: str,
               name: str, emoji: str = "🙂") -> "Life":
        """创建身份与空历史；同一人格可以产生多个互不共享历史的实体。"""
        hid = new_id("dh")
        root = Path(humans_dir) / hid
        root.mkdir(parents=True)
        (root / "history.jsonl").touch()
        identity = {"id": hid, "templateId": template_id, "providerId": provider_id,
                    "model": model, "name": name, "emoji": emoji, "bornAt": now_ms()}
        store.write_json(root / "identity.json", identity)
        return Life(hid, root, identity)

    @staticmethod
    def load(root) -> "Life":
        """只读取身份，保持个人历史未加载，供启动和列表使用。"""
        root = Path(root)
        identity = store.read_json(root / "identity.json", {})
        return Life(identity.get("id") or root.name, root, identity)

    @staticmethod
    def list_all(humans_dir) -> list["Life"]:
        base = Path(humans_dir)
        return [Life.load(p.parent) for p in sorted(base.glob("*/identity.json"))]

    def summary(self, template=None) -> dict:
        """从身份和人格生成列表信息，不为显示一张卡片而扫描完整历史。"""
        return {
            "id": self.id,
            **{key: self.identity.get(key) or "" for key in
               ("name", "providerId", "model", "templateId")},
            "emoji": self.identity.get("emoji") or "🙂",
            "bornAt": self.identity.get("bornAt") or 0,
            "lastTalkedAt": self.identity.get("lastTalkedAt") or 0,
            "templateName": template.name if template else "",
            "templateBare": bool(template.bare) if template else False,
        }

    def history_path(self) -> Path:
        return self.root / "history.jsonl"

    def read_history(self) -> list[dict]:
        """【核心】个人历史首次读取后复用；返回浅拷贝，消息字典按只读使用。

        空文件代表没有交流记录；文件缺失或损坏代表数据异常，不能当作空历史。
        多线程读写同一实体时由调用方持有 store.lock_for(self.id)。
        """
        if self._history is not None:
            return list(self._history)
        if not self.history_path().exists():
            raise OSError(f"{self.id}/history.jsonl 缺失，请从备份恢复")
        entries, bad = store.read_jsonl(self.history_path())
        if bad:
            raise OSError(f"{self.id}/history.jsonl 有 {bad} 条损坏记录；原文件未改动")
        self._history = entries
        return list(entries)

    def append_history(self, entry: dict) -> None:
        """调用方持实体锁；确认原文可读，再追加磁盘，成功后更新内存列表。"""
        self.read_history()
        store.append_jsonl(self.history_path(), entry)
        if self._history is not None:
            self._history.append(entry)

    def record_activity(self, at: int) -> None:
        """单独维护轻量活动时间，使数字人列表无需读取历史即可展示最近交流时间。"""
        identity = {**self.identity, "lastTalkedAt": max(self.identity.get("lastTalkedAt", 0), at)}
        store.write_json(self.root / "identity.json", identity)
        self.identity = identity

    def trash(self) -> Path:
        """删除操作移动整个目录，保留身份和历史，不影响会话中保存的回答快照。"""
        destination = self.root.parent.parent / ".trash" / f"{self.id}-{now_ms()}"
        with store.lock_for(self.id):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.root), str(destination))
        return destination
