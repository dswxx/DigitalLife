"""增量解码 UTF-8 和 SSE 数据，支持字符及事件跨网络分块。"""

from __future__ import annotations

import codecs
from collections.abc import Iterable, Iterator


def iter_sse_data(chunks: Iterable[bytes | str]) -> Iterator[str]:
    """【核心】将上游网络块还原为 data 行，服务于一行一个 JSON 的模型流。

    一个 UTF-8 字符和一行文本都可能跨块；解码器保留半个字符，buf 保留半行。
    其他行忽略；本函数不实现将多条 data 行合并成一个多行 SSE 事件。
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
