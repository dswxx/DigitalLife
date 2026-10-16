"""一次对话：**问过哪些人**。

新数据布局里原本没有这个东西——每一位生命只记"我亲历了什么"
（`humans/<生命>/sessions/<会话>.jsonl`）。但"这次问了谁"是**这次对话自己的属性**，
不能塞进任何一位生命的记忆里（那会变成"我以为别人也在"）。

所以：

    data/sessions/<对话>.json          这次对话：问了谁、什么时候开的
    data/humans/<生命>/sessions/<对话>.jsonl   那一位亲历的记录（各记各的）

两边用同一个对话 id 对上。**记忆属于个体，对话只记录"当时问了谁"。**

（0.8.0 之前这里叫"桌子"，还有第二种规矩 `council`——按顺序说、后面的人听得到前面。
圆桌那套取消了，所以只剩一种行为：一次提问、选中的几位并排作答、各自独立。
老文件里的 `mode` 字段不再读也不再写。）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import store
from .life import new_id, now_ms

DEFAULT_TITLE = "新的对话"

_ID_SAFE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff-]+")


def sessions_dir(data_dir):
    from pathlib import Path

    return Path(data_dir) / "sessions"


def session_path(data_dir, session_id: str):
    return sessions_dir(data_dir) / f"{_ID_SAFE.sub('_', session_id)}.json"


@dataclass
class Session:
    id: str
    title: str = DEFAULT_TITLE
    seats: list[dict] = field(default_factory=list)
    created_at: int = 0
    updated_at: int = 0

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "seats": list(self.seats),
            "createdAt": self.created_at or now_ms(),
            "updatedAt": self.updated_at or now_ms(),
        }

    @staticmethod
    def from_json(d: dict) -> "Session":
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
            title=str(d.get("title") or DEFAULT_TITLE),
            seats=seats,
            created_at=int(d.get("createdAt") or 0),
            updated_at=int(d.get("updatedAt") or 0),
        )

    def seat(self, seat_no: int) -> dict | None:
        return next((s for s in self.seats if s["seatNo"] == seat_no), None)

    def ordered(self) -> list[dict]:
        """按序号排序——**只为顺序稳定**（谁在第几位），不再代表谁先说。"""
        return sorted(self.seats, key=lambda s: s["seatNo"])


def create(data_dir, *, title: str = DEFAULT_TITLE,
           seats: list[dict] | None = None, session_id: str | None = None) -> Session:
    s = Session(
        id=session_id or new_id("th"),
        title=title or DEFAULT_TITLE,
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


def summary(data_dir, session: Session) -> dict:
    """给左侧栏那张小卡用的摘要：`N 轮 · M 位 · 什么时候动过`。

    轮数从**记录里数**（每位生命每轮各记一条 `you`，所以数一位的就够了）——
    对话本身只记"问了谁"，不记轮数，那份原文在生命自己的记录里。
    """
    rounds = 0
    if session.seats:
        from .life import KIND_YOU, Life

        first = Life.load(Path(data_dir) / "humans" / session.seats[0]["lifeId"])
        if (first.root / "identity.json").exists():
            rounds = sum(1 for e in first.read_session(session.id, warn=False)
                         if e.get("kind") == KIND_YOU)
    return {**session.to_json(), "rounds": rounds, "seatCount": len(session.seats)}


def delete(data_dir, session_id: str) -> bool:
    p = session_path(data_dir, session_id)
    if not p.exists():
        return False
    p.unlink()
    return True


def touch(data_dir, session_id: str) -> None:
    """有人说过话了：把这次对话往前排。"""
    s = load(data_dir, session_id)
    if s:
        save(data_dir, s)
