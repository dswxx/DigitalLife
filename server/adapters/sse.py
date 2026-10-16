"""SSE 解析：把字节流切成一个个 `data:` 载荷。**纯函数，不碰网络。**

单独一个文件，是因为这一层最容易出"看起来在工作、其实在丢字"的错：

1. **多字节字符会被 HTTP 分块切断。** 中文一个字 3 个字节，平台完全可能
   在一个字的中间切开两块发过来。每块各自 `decode("utf-8")` 就会把这个字
   变成替换字符——而且**只在中文、只在特定分块位置**出现，测试里很容易漏掉。
   所以这里用**增量解码器**（`codecs.getincrementaldecoder`）把半个字留在缓冲里。
   （老项目用 JS 的 `TextDecoder({stream:true})` 就是干这个的。）

2. **一行可能跨块。** `data: {...}` 中间断开是常态，所以按行缓冲，只在见到
   换行符时才吐出一行。

3. **末尾没有换行也要吐。** 有些平台最后一块不带换行符，漏了它就丢了最后一句话。
"""

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


def split_sse_line(line: str) -> str:
    """单行版本：给"已经按行切好"的输入用（测试和假传输更顺手）。"""
    line = line.rstrip("\r\n")
    return line[5:].strip() if line.startswith("data:") else ""
