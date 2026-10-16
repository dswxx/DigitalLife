"""数据落盘：纯 JSON / JSONL，原子替换，损坏归档。

两条铁律（从老项目继承）：
  1. **写文件先写同目录临时文件，再 os.replace** —— 同盘替换是原子的，断电不会留半个文件
  2. **JSON 一律 ensure_ascii=False** —— 否则「苏格拉底」会被写成 \\u82cf\\u683c\\u62c9\\u5e95，
     文件还是合法 JSON，但用户打开就看不懂了。"数据是你的、能用记事本改"全靠这一个参数

另外 JSONL 有一条新规矩：
  3. **读的时候容忍末行是半截的** —— 崩溃正好停在 append 中间会留下半个 JSON，
     跳过它，不要因为一行坏掉就读不出整个生命的历史
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

# ─────────────────────────── JSON ───────────────────────────


def read_json(path, fallback):
    """读 JSON。不存在返回 fallback 的深拷贝；解析失败**归档现场**再返回 fallback。"""
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return json.loads(json.dumps(fallback, ensure_ascii=False))
    except (json.JSONDecodeError, UnicodeDecodeError):
        # 文件坏了：留一份现场，别让整个应用起不来
        backup = p.with_name(f"{p.name}.corrupt-{int(time.time() * 1000)}")
        try:
            p.replace(backup)
            print(f"[store] 文件解析失败，已备份到 {backup}")
        except OSError:
            pass
        return json.loads(json.dumps(fallback, ensure_ascii=False))


def write_json(path, data) -> None:
    """原子写 JSON：同目录临时文件 + os.replace。

    `newline="\\n"` 是**故意的**：文本模式下 Windows 会把 `\\n` 翻译成 `\\r\\n`，
    于是同一份数据在 Windows 和 Linux 上字节不同。这不只是难看——水位线存的是
    **字节位置**，把 data 目录整拷到另一个平台继续用，偏移就会错位。
    数据是你的，那就该"拷到哪都一样"。
    """
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
    """追加一行。**只追加，永不改写**——这是"原文是真相"的物理保证。

    用 O_APPEND 打开：同一进程内多线程追加不会互相覆盖（行要小，
    实际用到的都是几百字节的对话事件）。

    `newline="\\n"` 的理由和 `write_json` 一样：别让平台把行尾悄悄改掉。
    水位线是按字节算的，行尾差一个字节就不一致了。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False).replace("\n", " ") + "\n"
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def read_jsonl(path, tolerate_tail: bool = True, offset: int = 0) -> tuple[list[dict], int]:
    """逐行读 JSONL，返回 (条目列表, 跳过的坏行数)。

    末行截断（崩溃留下的）默认跳过——**这是这套存储能"崩了也不丢前面"的关键**。

    `offset` 是**字节位置**，用来只读尾巴：压缩过的老对话归档在磁盘上，
    每次请求却都要读一遍整个文件的话，"越老的生命越贵"就变成了"越老越慢"。
    水位线（`compactedOffset`）永远落在行的边界上（= 追加前那一刻的文件长度），
    所以从这里读到的第一行必然是一整行。

    用二进制模式而不是文本模式：文本模式的 `seek` 只认 `tell()` 给出的
    cookie，不认字节位置。UTF-8 的多字节序列里不会出现 0x0A，
    所以在二进制下按行切一定切在真正的位置上。
    """
    p = Path(path)
    if not p.exists():
        return [], 0
    entries: list[dict] = []
    skipped = 0
    with p.open("rb") as f:
        if offset:
            f.seek(offset)
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entries.append(json.loads(raw.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                if not tolerate_tail:
                    raise
                skipped += 1
    return entries, skipped


def read_jsonl_spans(path, offset: int = 0) -> tuple[list[tuple[int, dict]], int]:
    """逐行读，并带上**每一行在文件里的起始字节位置**。

    用途只有一个，但很关键：压缩的时候要知道"我压到的那个点，在文件里是第几个字节"。
    水位线必须落在**被压内容的末尾**，不是"压缩那一刻的文件末尾"——
    那之后通常还有一堆没压过的原文，用文件末尾当水位线会把它们整段跳过。
    """
    p = Path(path)
    if not p.exists():
        return [], 0
    out: list[tuple[int, dict]] = []
    skipped = 0
    with p.open("rb") as f:
        if offset:
            f.seek(offset)
        while True:
            at = f.tell()
            raw = f.readline()
            if not raw:
                break
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                out.append((at, json.loads(stripped.decode("utf-8"))))
            except (json.JSONDecodeError, UnicodeDecodeError):
                skipped += 1
    return out, skipped


# ─────────────────────────── 锁 ─────────────────────────────

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def lock_for(key: str) -> threading.Lock:
    """按 key 取一把进程内锁。

    用途：同一个生命的"追加记录"和"后台压缩"不能同时改它的记忆。
    单进程应用，进程内锁就够——不需要文件锁。
    """
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]
