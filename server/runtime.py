"""应用持有身份和会话概况；打开会话后复用记录，完整一生归档按需读取。"""

from __future__ import annotations

from pathlib import Path
import threading

from . import sessions, settings, store, templates
from .life import Life


class Runtime:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.settings = settings.load(self.data_dir)
        self.templates = {t.id: t for t in templates.load_all(self.data_dir)}
        self.lives = {l.id: l for l in Life.list_all(self.data_dir / "humans")}
        self.sessions = {s.id: s for s in sessions.list_all(self.data_dir)}
        self.messages: dict[str, list[dict]] = {}
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._guard = threading.RLock()

    def lock(self, sid):
        with self._locks_guard:
            return self._locks.setdefault(sid, threading.RLock())

    def round_lock(self, sid):
        return store.lock_for(f"round:{self.data_dir.resolve()}:{sid}")

    def ensure_idle(self, sid):
        if self.round_lock(sid).locked():
            from .adapters import AdapterError
            raise AdapterError(409, "这一轮仍在后台回答，结束后再调整或删除")

    def life_summary(self, life):
        return life.summary(self.templates.get(life.identity.get("templateId")))

    def open_session(self, sid):
        with self.lock(sid):
            if sid not in self.sessions:
                raise KeyError(sid)
            if sid not in self.messages:
                if self.sessions[sid].rounds and not sessions.messages_path(self.data_dir, sid).exists():
                    raise OSError(f"会话 {sid} 的原文缺失，请从备份恢复")
                self.messages[sid] = sessions.read_messages(self.data_dir, sid)
            return list(self.messages[sid])

    def write_message(self, sid, message):
        """会话保存完整问答；参与实体各保存自己收到的问题和回答。"""
        with self.lock(sid):
            self.open_session(sid)
            store.append_jsonl(sessions.messages_path(self.data_dir, sid), message)
            self.messages[sid].append(message)
            session = self.sessions[sid]
            session.rounds = sum(m.get("kind") == "you" for m in self.messages[sid])
            sessions.save(self.data_dir, session)
            for life in list(self.lives.values()):
                entry = sessions.personal_message(message, life.id)
                if entry is not None:
                    with store.lock_for(life.id):
                        life.append_history({**entry, "sessionId": sid})
                        life.record_activity(message["t"])

    def refresh_templates(self):
        self.templates = {t.id: t for t in templates.load_all(self.data_dir)}

    def update_settings(self, **changes):
        with self._guard:
            saved = settings.update(self.data_dir, **changes)
            self.settings.update(saved)
            return saved

    def save_session(self, session):
        with self._guard, self.lock(session.id):
            self.ensure_idle(session.id)
            sessions.save(self.data_dir, session)
            self.sessions[session.id] = session

    def delete_session(self, sid):
        with self._guard, self.lock(sid):
            self.ensure_idle(sid)
            result = sessions.delete(self.data_dir, sid)
            self.sessions.pop(sid, None)
            self.messages.pop(sid, None)
            return result

    def delete_life(self, lid):
        with self._guard:
            for session in self.sessions.values():
                if any(s["lifeId"] == lid for s in session.seats):
                    self.ensure_idle(session.id)
            destination = self.lives[lid].trash()
            self.lives.pop(lid)
            return destination
