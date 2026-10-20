"""底层文件操作：JSON 原子替换、JSONL 追加刷盘与进程内锁。

本模块不理解人格或会话；调用方决定路径、写入顺序与哪些文件需要同一把锁。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path

# ─────────────────────────── JSON ───────────────────────────


def read_json(path, fallback):
    """文件不存在时返回默认值；损坏时直接报错，不改动原文件。"""
    try:
        with Path(path).open("r", encoding="utf-8") as file:
            return json.load(file)
    except FileNotFoundError:
        # 复制可变默认值，避免多个首次读取者共享同一个字典或列表。
        return json.loads(json.dumps(fallback))


def write_json(path, data) -> None:
    """在目标同目录写完临时 JSON 再替换，避免读者看到写到一半的文件。

    原子性只覆盖这一个文件，不覆盖一次业务操作中的多个文件。
    UTF-8 与 LF 让中文内容可直接阅读，并保持跨平台文件格式一致。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, p)  # 临时文件在同目录，替换发生于同一文件系统。
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─────────────────────────── JSONL ──────────────────────────


def append_jsonl(path, obj) -> None:
    """【核心】调用方持锁后追加一整行，并 flush / fsync 到文件。

    先检查末行边界，拒绝把新 JSON 拼接到崩溃遗留的半行后面。
    不自动删除坏行或重试追加；是否保存成功由异常向业务层反馈。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.stat().st_size:
        with p.open("rb") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                raise OSError(f"{p.name} 尾部不完整，请检查备份；未继续追加")
    # JSON 会转义正文内部换行，因此一个消息始终占一个物理行。
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path) -> tuple[list[dict], int]:
    """返回（可解析的消息、坏行数）；不重写文件，也不自动吞掉数据异常。

    Life 与会话读取层会在坏行数非零时拒绝使用这份不完整结果。
    """
    p = Path(path)
    if not p.exists():
        return [], 0
    entries, bad = [], 0
    for raw in p.read_bytes().splitlines():
        if not raw.strip():
            continue
        try:
            entry = json.loads(raw.decode("utf-8"))
            if not isinstance(entry, dict):
                raise ValueError("消息必须是对象")
            entries.append(entry)
        except (ValueError, UnicodeDecodeError):
            bad += 1
    return entries, bad


# ─────────────────────────── 锁 ─────────────────────────────

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock_for(key: str) -> threading.Lock:
    """按业务键复用进程内锁，锁表本身由 _locks_guard 保护。

    只适用于当前单进程运行方式，不提供多进程或多实例之间的文件互斥。
    """
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]
