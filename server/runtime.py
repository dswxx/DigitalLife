"""应用持有身份和会话概况；打开会话后复用记录，完整一生归档按需读取。"""

from __future__ import annotations

from pathlib import Path
import threading

from . import sessions, settings, store, templates
from .life import Life


class Runtime:
    """应用生命周期内共享的状态；磁盘文件负责持久化，列表负责运行期间复用。"""
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        # 【核心】启动只读配置、人格、身份和会话概况，聊天原文保持未加载。
        self.settings = settings.load(self.data_dir)
        self.templates = {t.id: t for t in templates.load_all(self.data_dir)}
        self.lives = {l.id: l for l in Life.list_all(self.data_dir / "humans")}
        self.sessions = {s.id: s for s in sessions.list_all(self.data_dir)}
        self.messages: dict[str, list[dict]] = {}  # 已打开会话的原文；实体历史由 Life 自己持有。
        # 会话 RLock 保护短时读写；round_lock 保护一整轮；实体锁保护个人历史。
        # 写记录时的顺序是“会话锁 → 实体锁”，不要在实体锁内反向写会话。
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()
        self._guard = threading.RLock()

    def lock(self, sid):
        """同一会话复用一把可重入锁，允许 write_message 内部再次调用 open_session。"""
        with self._locks_guard:
            return self._locks.setdefault(sid, threading.RLock())

    def round_lock(self, sid):
        """整轮占用锁，键包含数据目录，隔离同一进程中的不同测试或应用实例。"""
        return store.lock_for(f"round:{self.data_dir.resolve()}:{sid}")

    def ensure_idle(self, sid):
        """修改或删除会话前检查；已开始的后台回答仍视为忙碌。"""
        if self.round_lock(sid).locked():
            from .adapters import AdapterError
            raise AdapterError(409, "这一轮仍在后台回答，结束后再调整或删除")

    def life_summary(self, life):
        return life.summary(self.templates.get(life.identity.get("templateId")))

    def open_session(self, sid):
        """【核心】首次打开读盘，之后返回内存列表的浅拷贝；读取不触发补写。"""
        with self.lock(sid):
            if sid not in self.sessions:
                raise KeyError(sid)
            if sid not in self.messages:
                if self.sessions[sid].rounds and not sessions.messages_path(self.data_dir, sid).exists():
                    raise OSError(f"会话 {sid} 的原文缺失，请从备份恢复")
                self.messages[sid] = sessions.read_messages(self.data_dir, sid)
            # 返回新列表避免调用方增删缓存行；消息字典仍共享，调用方按只读使用。
            return list(self.messages[sid])

    def write_message(self, sid, message):
        """【核心】会话存完整问答，实体各存自己实际收到的问题和自己的回答。

        顺序：会话原文 → 内存记录与概况 → 相关实体历史与活动时间。
        这是两处普通文件保存，不是跨文件事务；任一步失败都向上报错，
        已写入的部分不回滚，也不通过自动重试追加来掩盖失败。
        """
        with self.lock(sid):
            self.open_session(sid)
            store.append_jsonl(sessions.messages_path(self.data_dir, sid), message)
            self.messages[sid].append(message)
            session = self.sessions[sid]
            session.rounds = sum(m.get("kind") == "you" for m in self.messages[sid])
            sessions.save(self.data_dir, session)
            # 使用实体列表快照；无关实体由 personal_message 排除。
            for life in list(self.lives.values()):
                entry = sessions.personal_message(message, life.id)
                if entry is not None:
                    with store.lock_for(life.id):
                        life.append_history({**entry, "sessionId": sid})
                        life.record_activity(message["t"])

    def refresh_templates(self):
        self.templates = {t.id: t for t in templates.load_all(self.data_dir)}

    def update_settings(self, **changes):
        """以磁盘最新值合并更新，再同步共享字典，避免并发请求覆盖新配置。"""
        with self._guard:
            saved = settings.update(self.data_dir, **changes)
            self.settings.update(saved)
            return saved

    def save_session(self, session):
        """管理操作与整轮调用互斥，避免回答途中改变参与者归属。"""
        with self._guard, self.lock(session.id):
            self.ensure_idle(session.id)
            sessions.save(self.data_dir, session)
            self.sessions[session.id] = session

    def delete_session(self, sid):
        """移除会话文件和运行时记录；个人历史是独立档案，不跟随删除。"""
        with self._guard, self.lock(sid):
            self.ensure_idle(sid)
            result = sessions.delete(self.data_dir, sid)
            self.sessions.pop(sid, None)
            self.messages.pop(sid, None)
            return result

    def delete_life(self, lid):
        """关联会话空闲后将实体移入回收目录，既有会话回答仍可读取。"""
        with self._guard:
            for session in self.sessions.values():
                if any(s["lifeId"] == lid for s in session.seats):
                    self.ensure_idle(session.id)
            destination = self.lives[lid].trash()
            self.lives.pop(lid)
            return destination
