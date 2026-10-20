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
    id: str
    root: Path
    identity: dict = field(default_factory=dict)
    _history: list | None = field(default=None, repr=False)
    active_rounds: set = field(default_factory=set, repr=False)

    @staticmethod
    def create(humans_dir, *, template_id: str, provider_id: str, model: str,
               name: str, emoji: str = "🙂") -> "Life":
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
        root = Path(root)
        identity = store.read_json(root / "identity.json", {})
        return Life(identity.get("id") or root.name, root, identity)

    @staticmethod
    def list_all(humans_dir) -> list["Life"]:
        base = Path(humans_dir)
        return [Life.load(p.parent) for p in sorted(base.glob("*/identity.json"))]

    def summary(self, template=None) -> dict:
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
        self.read_history()
        store.append_jsonl(self.history_path(), entry)
        if self._history is not None:
            self._history.append(entry)

    def record_activity(self, at: int) -> None:
        identity = {**self.identity, "lastTalkedAt": max(self.identity.get("lastTalkedAt", 0), at)}
        store.write_json(self.root / "identity.json", identity)
        self.identity = identity

    def trash(self) -> Path:
        destination = self.root.parent.parent / ".trash" / f"{self.id}-{now_ms()}"
        with store.lock_for(self.id):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(self.root), str(destination))
        return destination
