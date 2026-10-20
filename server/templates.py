"""可复用的人格蓝图。内容生成稳定 id，更新时同步个体引用。"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import presets, store
from .life import Life


def stable_id(prefix: str, *parts: str) -> str:
    """由内容算出来的稳定 id。同样的输入永远得到同样的 id。"""
    h = hashlib.sha1("\u0000".join(parts).encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{h}"


@dataclass
class Template:
    id: str
    name: str
    system: str = ""
    emoji: str = "🙂"
    tagline: str = ""
    tags: list[str] = field(default_factory=list)
    bare: bool = False  # 纯基线：服务端**不构造** system 字段
    builtin: bool = False  # 内置（可删，但重新播种会再出现）
    origin: str = ""  # user=用户编写 | seed=内置
    created_at: int = 0

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "emoji": self.emoji,
            "tagline": self.tagline,
            "tags": list(self.tags),
            "system": self.system,
            "bare": self.bare,
            "builtin": self.builtin,
            "origin": self.origin,
            "createdAt": self.created_at or int(time.time() * 1000),
        }

    @staticmethod
    def from_json(d: dict) -> "Template":
        return Template(
            id=d.get("id", ""),
            name=d.get("name", ""),
            system=d.get("system", ""),
            emoji=d.get("emoji", "🙂"),
            tagline=d.get("tagline", ""),
            tags=list(d.get("tags") or []),
            bare=bool(d.get("bare")),
            builtin=bool(d.get("builtin")),
            origin=d.get("origin", ""),
            created_at=int(d.get("createdAt") or 0),
        )


def templates_dir(data_dir) -> Path:
    return Path(data_dir) / "templates"


def template_path(data_dir, tid: str) -> Path:
    return templates_dir(data_dir) / f"{tid}.json"


def load_all(data_dir) -> list[Template]:
    d = templates_dir(data_dir)
    if not d.exists():
        return []
    return [
        Template.from_json(json.loads(p.read_text(encoding="utf-8")))
        for p in sorted(d.glob("tpl_*.json"))
    ]


def save(data_dir, tpl: Template) -> None:
    store.write_json(template_path(data_dir, tpl.id), tpl.to_json())


def make(name: str, system: str, **kw) -> Template:
    """从一段人格造一个模板。id 由「名字 + 人格正文」算出来。"""
    return Template(id=stable_id("tpl", name, system), name=name, system=system, **kw)


def update(data_dir, old: Template, changes: dict, *, lives=None) -> Template:
    """先保存新内容，再更新引用，最后移除旧文件；写入失败时旧人格仍在。"""
    new = make(
        (changes.get("name") or old.name).strip(), changes.get("system", old.system),
        emoji=changes.get("emoji", old.emoji), tagline=changes.get("tagline", old.tagline),
        tags=list(changes.get("tags", old.tags)), bare=bool(changes.get("bare", old.bare)),
        builtin=old.builtin, origin=old.origin or "user", created_at=old.created_at,
    )
    save(data_dir, new)
    if new.id != old.id:
        for life in (lives if lives is not None else Life.list_all(Path(data_dir) / "humans")):
            with store.lock_for(life.id):
                if not (life.root / "identity.json").exists():
                    continue
                if life.identity.get("templateId") == old.id:
                    identity = {**life.identity, "templateId": new.id}
                    store.write_json(life.root / "identity.json", identity)
                    life.identity = identity
        template_path(data_dir, old.id).unlink(missing_ok=True)
    return new


def seed(data_dir) -> int:
    """把内置模板写进去（幂等）。返回新写了几个。

    幂等靠确定性 id：已经存在的跳过，所以随时可以再调一次而不会重复。
    """
    added = 0
    for t in presets.SEED_TEMPLATES:
        tpl = make(t["name"], t["system"], emoji=t["emoji"], tagline=t["tagline"],
                   tags=t["tags"], builtin=True, origin="seed")
        if not template_path(data_dir, tpl.id).exists():
            save(data_dir, tpl)
            added += 1
    b = presets.BARE_TEMPLATE
    bare = make(b["name"], b.get("system", ""), emoji=b["emoji"], tagline=b["tagline"],
                tags=b["tags"], bare=True, builtin=True, origin="seed")
    if not template_path(data_dir, bare.id).exists():
        save(data_dir, bare)
        added += 1
    return added
