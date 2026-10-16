"""圆桌 Roundtable · 后端（FastAPI）。

分层（HTTP 只是最外面那层壳）：

    main.py       FastAPI 应用、路由、三层安全、静态托管
    actions/      各个接口的实现
    adapters/     上游协议适配（OpenAI 兼容 / Anthropic），统一产出同一套事件
    life.py       数字生命：身份 + 对话记录 + 长期记忆      ← 纯标准库
    context.py    按预算组装一次请求的 system / messages    ← 纯标准库
    store.py      原子写 JSON / 只追加 JSONL               ← 纯标准库
"""

import sys

__version__ = "0.1.0"

# ── 版本门槛 ──────────────────────────────────────────────
#
# 放在包的 __init__ 里：任何入口（服务、自测、脚本）一 import 就会过这一关，
# 不需要每个入口各写一遍。
#
# 为什么要显式拦：这台机器上 `python` 是 3.9、`py -3` 才是 3.11+ 。
# 要是直接用 `python` 起服务，报的会是一堆看不懂的语法/类型错误，
# 而不是一句"你用的 Python 太旧了"。
MIN_PYTHON = (3, 11)

if sys.version_info < MIN_PYTHON:
    _cur = ".".join(str(x) for x in sys.version_info[:3])
    _need = ".".join(str(x) for x in MIN_PYTHON)
    raise RuntimeError(
        f"\n\n  ✗ Python 版本过低：当前 {_cur}，需要 {_need} 或更高。\n"
        f"    请用 `py -3` 启动（PATH 里的 `python` 可能是个旧的），"
        f"或到 https://www.python.org/downloads/ 装一个 {_need}+。\n"
    )

