"""圆桌 Roundtable · 后端（FastAPI）。

分层（HTTP 只是最外面那层壳），依赖方向是一条直线，**不许成环**：

    main.py       FastAPI 应用、路由、三层安全、静态托管      ← 最外层
      ↓
    chat.py       一次提问的全过程（并发席位 / 会议顺序 / 后台整理）
      ↓
    context.py    按预算组装一次请求的 system / messages
    memory.py     压缩 / 记忆提取 / 召回（模型调用靠注入，所以能离线测）
      ↓
    sessions.py   桌子（谁和谁坐一起）
    settings.py   设置与预算（密钥掩码、窗口、输出上限）
    templates.py  人格蓝图
    life.py       数字生命：身份 + 对话记录 + 长期记忆        ← 纯标准库
      ↓
    store.py      原子写 JSON / 只追加 JSONL / 按字节读尾巴  ← 纯标准库
    text.py       token 估算 / 时间格式化（抽出来就是为了打断环）
    model.py      模型调用的公共契约（ModelReply / ModelError）

为什么 `text.py` 单独存在：`context` 要调 `memory` 做召回，`memory` 要估算 token
和格式化时间戳——直接互相 import 就成环了。把这几样抽出来，方向就是直的。
"""

import sys

# **版本号只在这里改。** `/api/health` 和前端的页脚都读它，别再各写一份字面量
# （写两份的下场就是有一天改了这里、页脚还显示旧版本）。
#
# 注意和「接口版本」（`main.API_VERSION`）是两件事：那个是给前端做兼容检查用的，
# 只在接口形状变了的时候 +1；这个是产品版本。
__version__ = "0.5.0"

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

