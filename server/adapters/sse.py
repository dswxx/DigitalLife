"""增量解码 UTF-8 和 SSE 数据，支持字符及事件跨网络分块。"""

from __future__ import annotations

import codecs
from collections.abc import Iterable, Iterator


def iter_sse_data(chunks: Iterable[bytes | str]) -> Iterator[str]:
    """把 SSE 字节流切成一个个 `data:` 载荷（不含前缀和换行）。

    非 `data:` 的行（`event:`、`id:`、注释、心跳空行）一律丢掉——
    我们只关心正文，其余交给各自的解析器按类型处理。
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buf = ""
    for chunk in chunks:
        buf += decoder.decode(chunk) if isinstance(chunk, bytes) else chunk
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.rstrip("\r")
            if line.startswith("data:"):
                yield line[5:].strip()
    buf += decoder.decode(b"", True)  # 把最后可能留着的半个字冲出来
    tail = buf.strip()
    if tail.startswith("data:"):
        yield tail[5:].strip()
