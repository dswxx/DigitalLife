"""人格模板：可复用的蓝图。**不绑厂商、没有记忆。**

和「生命」的区别是这个产品的核心：

    模板 = 蓝图（写一次，可以用来创建很多个生命）
    生命 = 用某个模板 + 某家 API 创建出来的个体，有自己的记忆

所以数据里：
    data/templates/tpl_xxx.json          人格本身
    data/humans/dh_yyy/identity.json     这个生命用了哪个模板、绑了哪家、什么时候出生

**id 是确定性的**（由内容算出来，不是随机数）：这样播种和迁移都可以随时重跑，
不会重复插一份。中文名字没法可靠地转拼音，所以不追求 id 可读——
用户看到的是 name，id 只在文件里用。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import presets, store


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
    origin: str = ""  # user=自己写的 | seed=内置 | distill=提炼出来的 | imported=导入的
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


def load(data_dir, tid: str) -> Template | None:
    p = template_path(data_dir, tid)
    if not p.exists():
        return None
    return Template.from_json(json.loads(p.read_text(encoding="utf-8")))


def save(data_dir, tpl: Template) -> None:
    store.write_json(template_path(data_dir, tpl.id), tpl.to_json())


def make(name: str, system: str, **kw) -> Template:
    """从一段人格造一个模板。id 由「名字 + 人格正文」算出来。"""
    return Template(id=stable_id("tpl", name, system), name=name, system=system, **kw)


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
