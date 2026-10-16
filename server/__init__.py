"""圆桌 Roundtable · 后端（FastAPI）。

分层（HTTP 只是最外面那层壳）：

    main.py       FastAPI 应用、路由、三层安全、静态托管
    actions/      各个接口的实现
    adapters/     上游协议适配（OpenAI 兼容 / Anthropic），统一产出同一套事件
    life.py       数字生命：身份 + 对话记录 + 长期记忆      ← 纯标准库
    context.py    按预算组装一次请求的 system / messages    ← 纯标准库
    store.py      原子写 JSON / 只追加 JSONL               ← 纯标准库
"""

__version__ = "0.1.0"
