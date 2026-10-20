"""UTF-8 文件存储：原子写 JSON、追加 JSONL、按字节读取与进程内锁。"""

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
        return json.loads(json.dumps(fallback))


def write_json(path, data) -> None:
    """在同一目录写临时文件，再原子替换目标 JSON。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
        os.replace(tmp, p)  # 同盘原子
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ─────────────────────────── JSONL ──────────────────────────


def append_jsonl(path, obj) -> None:
    """在调用方持锁时追加并 fsync；拒绝拼接不完整尾行，保留既有字节位置。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists() and p.stat().st_size:
        with p.open("rb") as f:
            f.seek(-1, 2)
            if f.read(1) != b"\n":
                raise OSError(f"{p.name} 尾部不完整，请检查备份；未继续追加")
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path) -> tuple[list[dict], int]:
    """读取原文并报告坏行数量，由调用方决定如何显示错误。"""
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
    """按 key 取一把进程内锁。

    用途：同一个实体的记录写入需要串行。
    单进程应用，进程内锁就够——不需要文件锁。
    """
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]
