"""会话信息、完整问答原文与按轮次分页。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import store
from .life import new_id, now_ms

DEFAULT_TITLE = "新的对话"

_ID_SAFE = re.compile(r"[^0-9A-Za-z_\u4e00-\u9fff-]+")


def sessions_dir(data_dir):
    return Path(data_dir) / "sessions"


def session_path(data_dir, session_id: str):
    if not isinstance(session_id, str) or not session_id or _ID_SAFE.search(session_id):
        raise ValueError("会话 id 只能包含字母、数字、中文、下划线和短横线")
    return sessions_dir(data_dir) / session_id / "info.json"


def messages_path(data_dir, session_id: str):
    return session_path(data_dir, session_id).with_name("messages.jsonl")


@dataclass
class Session:
    id: str
    title: str = DEFAULT_TITLE
    seats: list[dict] = field(default_factory=list)
    created_at: int = 0
    updated_at: int = 0
    rounds: int = 0

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "seats": list(self.seats),
            "createdAt": self.created_at or now_ms(),
            "updatedAt": self.updated_at or now_ms(),
            "rounds": self.rounds,
        }

    @staticmethod
    def from_json(d: dict) -> "Session":
        return Session(
            id=d.get("id") or new_id("th"),
            title=str(d.get("title") or DEFAULT_TITLE),
            seats=normalize_seats(d.get("seats") or []),
            created_at=int(d.get("createdAt") or 0),
            updated_at=int(d.get("updatedAt") or 0),
            rounds=int(d.get("rounds") or 0),
        )

    def ordered(self) -> list[dict]:
        """按展示顺序返回参与者。模型调用仍然并发。"""
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
    seen = set()
    for i, s in enumerate(seats):
        if not s.get("lifeId"):
            continue
        if s["lifeId"] in seen:
            raise ValueError("同一会话不能重复加入同一个体")
        seen.add(s["lifeId"])
        out.append({
            "seatNo": int(s.get("seatNo") or i + 1),
            "lifeId": s["lifeId"],
            "providerId": s.get("providerId") or "",
            "model": s.get("model") or "",
        })
    if len({s["seatNo"] for s in out}) != len(out) or any(s["seatNo"] < 1 for s in out):
        raise ValueError("参与者序号必须为互不重复的正整数")
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
    for p in sorted(d.glob("*/info.json")):
        got = store.read_json(p, None)
        if isinstance(got, dict) and got.get("id"):
            out.append(Session.from_json(got))
    return sorted(out, key=lambda s: s.updated_at, reverse=True)


def summary(session: Session) -> dict:
    """概况不读取聊天记录，启动和侧栏列表保持轻量。"""
    return {**session.to_json(), "seatCount": len(session.seats)}


def delete(data_dir, session_id: str) -> bool:
    p = session_path(data_dir, session_id)
    if p.parent.resolve().parent != sessions_dir(data_dir).resolve():
        raise ValueError("会话目录越界")
    if not p.exists():
        return False
    import shutil

    shutil.rmtree(p.parent)
    return True


def read_messages(data_dir, sid: str) -> list[dict]:
    entries, bad = store.read_jsonl(messages_path(data_dir, sid))
    if bad:
        raise OSError(f"会话 {sid} 有 {bad} 条损坏记录，请从备份恢复；原文件未改动")
    return entries


def personal_message(message: dict, life_id: str) -> dict | None:
    """只把实际收到的问题和本人的回答写入个人经历。"""
    if message["kind"] == "you":
        if not any(p["lifeId"] == life_id for p in message.get("participants", [])):
            return None
        return {k: v for k, v in message.items() if k != "participants"}
    if message["kind"] == "self" and message.get("lifeId") == life_id:
        return dict(message)
    return None


def round_views(messages: list[dict], *, before: str = "", limit: int = 3) -> dict:
    """按稳定轮次 ID 展示历史；没有收尾的调用明确显示未完成。"""
    rounds = []
    by_id = {}
    for entry in messages:
        if entry.get("kind") == "you":
            r = {"id": entry["roundId"], "t": entry["t"], "question": entry["text"],
                 "participants": entry.get("participants", []), "answers": []}
            rounds.append(r)
            by_id[r["id"]] = r
        elif entry.get("kind") == "self" and entry.get("roundId") in by_id:
            by_id[entry["roundId"]]["answers"].append({**entry, "content": entry.get("text", "")})
    if before:
        index = next((i for i, r in enumerate(rounds) if r["id"] == before), None)
        if index is None:
            raise ValueError("找不到历史游标，请重新打开会话")
        rounds = rounds[:index]
    limit = max(1, min(100, limit))
    page = rounds[-limit:]
    for r in page:
        answered = {a["lifeId"] for a in r["answers"]}
        r["answers"].extend({**p, "content": "", "error": "未收到完整回答（调用仍在进行或已中断）"}
                            for p in r["participants"] if p["lifeId"] not in answered)
        r["answers"].sort(key=lambda a: a["seatNo"])
    return {"rounds": page, "hasMore": len(rounds) > limit,
            "before": page[0]["id"] if page else ""}
