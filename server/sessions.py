"""桌子：**谁和谁坐在一起说话**。

新数据布局里原本没有这个东西——每一位生命只记"我亲历了什么"
（`humans/<生命>/sessions/<会话>.jsonl`）。但"桌子上有谁、按什么规矩轮"是
**桌子自己的属性**，不能塞进任何一位生命的记忆里（那会变成"我以为别人也在"）。

所以：

    data/sessions/<会话>.json          桌子：有谁、什么规矩、什么时候开的
    data/humans/<生命>/sessions/<会话>.jsonl   那一位亲历的记录（各记各的）

两边用同一个会话 id 对上。**记忆属于个体，桌子只记录"当时有谁"。**
老数据里的 `threads/*.json` 是桌子的老形状，迁移时按这个结构重建一份。

两种规矩（继承老项目，产品特性不能丢）：
  · parallel 并排：所有人同时听到同一句问题，**互相听不到这一轮的回答**
  · council  会议：按座位顺序一个一个说，**后面的人听得到前面的人**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import store
from .life import new_id, now_ms

MODES = ("parallel", "council")

_ID_SAFE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff-]+")


def sessions_dir(data_dir):
    from pathlib import Path

    return Path(data_dir) / "sessions"


def session_path(data_dir, session_id: str):
    return sessions_dir(data_dir) / f"{_ID_SAFE.sub('_', session_id)}.json"


@dataclass
class Session:
    id: str
    title: str = "新的圆桌"
    mode: str = "parallel"
    seats: list[dict] = field(default_factory=list)
    created_at: int = 0
    updated_at: int = 0

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "mode": self.mode,
            "seats": list(self.seats),
            "createdAt": self.created_at or now_ms(),
            "updatedAt": self.updated_at or now_ms(),
        }

    @staticmethod
    def from_json(d: dict) -> "Session":
        mode = d.get("mode") if d.get("mode") in MODES else "parallel"
        seats = []
        for i, s in enumerate(d.get("seats") or []):
            if not s.get("lifeId"):
                continue  # 老数据里有空座位（还没放数字人），跳过
            seats.append({
                "seatNo": int(s.get("seatNo") or i + 1),
                "lifeId": s["lifeId"],
                "providerId": s.get("providerId") or "",
                "model": s.get("model") or "",
                "temperature": s.get("temperature"),
            })
        return Session(
            id=d.get("id") or new_id("th"),
            title=str(d.get("title") or "新的圆桌"),
            mode=mode,
            seats=seats,
            created_at=int(d.get("createdAt") or 0),
            updated_at=int(d.get("updatedAt") or 0),
        )

    def seat(self, seat_no: int) -> dict | None:
        return next((s for s in self.seats if s["seatNo"] == seat_no), None)

    def ordered(self) -> list[dict]:
        """按座位号排序。**会议模式的顺序就是它**——谁先说谁后说。"""
        return sorted(self.seats, key=lambda s: s["seatNo"])


def create(data_dir, *, title: str = "新的圆桌", mode: str = "parallel",
           seats: list[dict] | None = None, session_id: str | None = None) -> Session:
    s = Session(
        id=session_id or new_id("th"),
        title=title or "新的圆桌",
        mode=mode if mode in MODES else "parallel",
        seats=normalize_seats(seats or []),
        created_at=now_ms(),
        updated_at=now_ms(),
    )
    save(data_dir, s)
    return s


def normalize_seats(seats: list[dict]) -> list[dict]:
    out = []
    for i, s in enumerate(seats):
        if not s.get("lifeId"):
            continue
        out.append({
            "seatNo": int(s.get("seatNo") or i + 1),
            "lifeId": s["lifeId"],
            "providerId": s.get("providerId") or "",
            "model": s.get("model") or "",
            "temperature": s.get("temperature"),
        })
    return out


def load(data_dir, session_id: str) -> Session | None:
    p = session_path(data_dir, session_id)
    if not p.exists():
        return None
    return Session.from_json(store.read_json(p, {}))


def save(data_dir, session: Session) -> None:
    session.updated_at = now_ms()
    store.write_json(session_path(data_dir, session.id), session.to_json())


def list_all(data_dir) -> list[Session]:
    d = sessions_dir(data_dir)
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        got = store.read_json(p, None)
        if isinstance(got, dict) and got.get("id"):
            out.append(Session.from_json(got))
    return sorted(out, key=lambda s: s.updated_at, reverse=True)


def delete(data_dir, session_id: str) -> bool:
    p = session_path(data_dir, session_id)
    if not p.exists():
        return False
    p.unlink()
    return True


def touch(data_dir, session_id: str) -> None:
    """有人说过话了：把桌子往前排。"""
    s = load(data_dir, session_id)
    if s:
        save(data_dir, s)
