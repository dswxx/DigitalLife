# 圆桌 Roundtable · 新架构

> **这是一次完全重构**：技术栈换成 Python（FastAPI）+ Vue 3，产品从一个"多模型聊天工具"
> 换成**一群有独立记忆的数字生命**。
> 老项目（Node 版）停在 `v0.4.2`，完整保留在另一个目录里，随时能回去对照。

## 现在到哪一步了

**前后端都能跑了。** 后端能真流式对话，前端能看库、能聊天、能编辑记忆、能看"一生"。

| 层 | 文件 | 状态 |
|---|---|---|
| 落盘 | `server/store.py` | ✅ 原子写、只追加、容忍末行截断、按字节读尾巴 |
| 生命 | `server/life.py` | ✅ 身份 + 对话记录 + 长期记忆 + 水位线索引 |
| 蓝图 | `server/templates.py` | ✅ 人格模板（确定性 id，播种/迁移可重跑） |
| 桌子 | `server/sessions.py` | ✅ 谁和谁坐一起（并排 / 会议两种规矩） |
| 预算 | `server/settings.py` | ✅ 密钥掩码、窗口从哪来、输出上限三者取最小 |
| 上下文 | `server/context.py` | ✅ 四档记忆递减 + 预算裁剪 + 召回（**请求路径绝不调模型**） |
| 压缩 | `server/memory.py` | ✅ 前置水位线 + 后台压缩（模型调用注入，离线可测） |
| 长期记忆 | `server/memory.py` | ✅ 提取（去重收敛）+ 召回（关键词 + 时间衰减，零模型调用） |
| 适配器 | `server/adapters/` | ✅ OpenAI 兼容 / Anthropic，含重试与报错翻译 |
| 迁移 | `server/migrate.py` | ✅ 老数据 → 蓝图 + 个体 + 历史，只读老目录、可重跑 |
| HTTP | `server/main.py` | ✅ 上线了，接口见下 |
| 前端 | `web/` | ✅ 库 / 圆桌对话 / 记忆编辑器 / 一生 / 设置 |
| 提炼工作台 | — | ⬜ 把一本书炼成一份人格（**你说先不做**；提示词已搬过来并核过，想做时直接接上） |

### 跑起来

```bash
py -3 -m pip install fastapi uvicorn httpx    # 后端依赖，只有这三个
cd web && npm install && npm run build         # 前端构建一次（之后不用再构建）
cd .. && py -3 run.py                          # 或双击 启动圆桌.cmd
```

打开它打印的 `http://127.0.0.1:8787`。没构建前端时它会给一张说清楚的提示页，
并在启动信息里告诉你差哪两步。

### 测试

```bash
# 后端（不需要联网；装了上面三个依赖就能全跑）
py -3 dev/selftest.py        # 34 项  落盘 / 生命 / 上下文 / 记忆递减
py -3 dev/check_presets.py   # 49 项  预设提示词与老项目逐字一致（对着老文件核）
py -3 dev/test_migrate.py    # 62 项  拿真实老数据迁移，含"老目录一个字节没变"
py -3 dev/test_context.py    # 46 项  真实生命 × 各种窗口，账目逐笔核对 + 单轮超大
py -3 dev/test_memory.py     # 58 项  压缩：什么时候压、压哪段、推水位线、坏模型怎么办
py -3 dev/test_longterm.py   # 64 项  记忆提取（模型乱说话怎么办）+ 召回（零模型调用）
py -3 dev/test_adapters.py   # 69 项  两种协议的请求组装与流解析（含老项目踩过的坑）
py -3 dev/test_http.py       # 105 项 **端到端**：真起两个服务、真 socket、真流式

# 界面（真浏览器）
py -3 dev/test_ui.py --cdp-port 9333   # 52 项：库 / 记忆 / 一生 / 对话 / 设置 / 新建
```

合计 **539 项**。界面那一套要一个开着调试端口的浏览器：

```bash
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --headless=new --remote-debugging-port=9333 `
  --user-data-dir=D:\某处\chrome-profile --disable-gpu about:blank
py -3 dev/test_ui.py --cdp-port 9333 --shots    # --shots 会把截图存到 dev/.tmp-ui/shots
```

> 为什么界面测试不用 Playwright：它的 Python 绑定要靠**命名管道**跟 Node 驱动通信，
> 在受限环境里起不来。这个测试自己用 CDP over TCP 驱动浏览器，能力上只少一点，
> 但**不依赖任何浏览器自动化框架**（连 playwright 都不用装）。

> ⚠️ **别用 `python`**：这台机器上 PATH 里的 `python` 是 3.9，而项目要求 **Python ≥ 3.11**。
> 用 `py -3`（你机器上是 3.14，够用）。版本不够时包入口会直接拦下来并说明原因，
> 而不是抛一堆看不懂的语法错。

## 接口一览

全是新契约（老前端不再兼容——反正要重写）。除 `/` 之外都要带 `x-roundtable-token`，
它由服务端注入到 HTML 里（每次启动随机生成）。

```
GET    /api/health                            版本、数据版本
GET    /api/bootstrap                         启动要的一切（设置+模板+生命+桌子）
GET    /api/settings                          平台/高级设置（**密钥只回掩码**）
POST   /api/settings                          保存（没传的字段一律不动）
POST   /api/providers/{id}/models             拉这家有哪些模型
POST   /api/providers/{id}/test               连通性测试
GET    /api/templates            POST         人格模板
PATCH  /api/templates/{id}       DELETE       （改内容会改 id，用着它的生命自动跟着走）
GET    /api/lives                POST         数字人（生命）
GET    /api/lives/{id}           PATCH  DELETE
GET    /api/lives/{id}/memory    POST         记忆：看 / 手写一条
PATCH  /api/lives/{id}/memory/{mid}  DELETE
GET    /api/lives/{id}/sessions/{sid}         他在那次会话里的亲历（?tail=1 只要未压缩的）
GET    /api/lives/{id}/timeline                「一生」：按天聚合
POST   /api/lives/{id}/sessions/{sid}/compact 手动整理（压缩 + 提炼记忆）
GET    /api/sessions             POST         桌子
PATCH  /api/sessions/{id}        DELETE
POST   /api/ask                               **流式对话**（NDJSON，一行一个事件）
```

`/api/ask` 产出的事件：`seat_start` / `text` / `reasoning` / `notice` / `limit` /
`seat_done` / `error` / `round_done`。每块都带 `seatNo`——并排模式下多个席位是
**真的同时在跑**，字是交错来的，前端照着 `seatNo` 往各自的卡片上贴。

## 目录

```
run.py           启动（检查依赖、挑端口、播种模板、只绑 127.0.0.1）
server/
├── store.py      原子写 JSON / 只追加 JSONL / 按字节读尾巴                  ← 纯标准库
├── life.py       一个数字生命：身份 + 对话记录 + 长期记忆 + 水位线           ← 纯标准库
├── templates.py  人格模板：蓝图，不绑厂商、没有记忆                          ← 纯标准库
├── sessions.py   桌子：谁和谁坐一起、并排还是会议                            ← 纯标准库
├── presets.py    预设（从老项目逐字搬过来，对着老文件核过）                   ← 纯标准库
├── context.py    按预算组装一次请求的 system / messages                      ← 纯标准库
├── memory.py     水位线 + 压缩 + 记忆提取/召回（模型调用靠注入）              ← 纯标准库
├── model.py      模型调用的公共契约（ModelReply / ModelError）               ← 纯标准库
├── text.py       token 估算 / 时间格式化（抽出来是为了打断循环依赖）          ← 纯标准库
├── settings.py   设置与预算：密钥掩码、窗口、输出上限                        ← 纯标准库
├── migrate.py    老数据 → 新结构（只读老目录、确定性 id、可重跑）             ← 纯标准库
├── chat.py       一次提问的全过程（并发席位 / 会议顺序 / 自动重试 / 落记录）
├── adapters/     OpenAI 兼容 / Anthropic：请求组装、SSE 解析、重试、报错翻译
└── main.py       FastAPI 应用、路由、三层安全、静态托管
dev/              九套测试 + 一个假上游（真的 HTTP 服务）
web/              前端（Vue 3 + Vite + TS + Pinia）
├── index.html    里面那行 `window.__RT_BOOT__ = /*__RT_BOOT__*/null;` 是令牌占位符
└── src/
    ├── api.ts      接口客户端（含 NDJSON 流式读取）
    ├── store.ts    全局状态（启动数据 / 当前桌子 / 正在流式的那一轮）
    ├── md.ts       极小的 Markdown 渲染（先转义 HTML，不引依赖）
    └── views/      库 / 对话 / 记忆 / 一生 / 设置
```

## 数据长什么样

```
data/
├── settings.json                   平台 / 密钥 / 高级设置 / 从平台学到的模型上限
├── templates/<模板>.json            人格蓝图（不绑厂商、没有记忆）
├── sessions/<会话>.json             桌子：有谁、并排还是会议
└── humans/<生命>/
    ├── identity.json               身份：来自哪个模板、绑哪家 API、什么时候出生
    ├── memory/
    │   ├── INDEX.md                目录，一条一行（**只放摘要，不放正文**）
    │   └── m_xxx.md                一条记忆：YAML 头 + Markdown 正文
    └── sessions/
        ├── <会话>.jsonl            他在那次会话里的亲历，带时间戳
        └── <会话>.meta.json        水位线索引：压到哪儿了、摘要是什么、花了多少 token
```

**格式是有意区分的**：

| 文件 | 格式 | 为什么 |
|---|---|---|
| 对话记录 | JSONL，只追加 | 机器日志：能 grep、能 `tail -f`、崩了不丢前面的 |
| 长期记忆 | Markdown + YAML 头 | 人要读要改的东西：**他记错了你能直接删掉那一条** |
| 桌子 / 水位线 | 小 JSON，原子替换 | 每次请求都要读，所以必须小；原文永远不删 |

**原文是真相，摘要和记忆都是可以重算的派生物。** 所以压缩从来不删原文，
只是把水位线往前推一格：`compactedOffset` 之后的字节才是"还没压过的尾巴"，
请求路径只读那一段（老原文归档在磁盘上，根本不解析）。

设计细节见 [`数字生命与记忆设计.md`](数字生命与记忆设计.md)（数据布局、上下文编排、
记忆递减、写入/召回路径、桌子与水位线的补充设计、对标两个开源项目的逐条取舍、以及还没定的事）。

## 四条从老项目继承的铁律

1. **JSON 一律 `ensure_ascii=False`** —— 否则「苏格拉底」会变成 `\u82cf…`，记事本就看不懂了。
   "数据是你的"全靠这一个参数
2. **密钥永不出本机**：只存 `data/settings.json`，接口只回掩码；掩码回传不会覆盖真密钥
3. **纯基线的请求里一个 system 字段都不发** —— 不是发空串，是压根不构造
4. **请求路径永远不调用模型做压缩** —— 压缩在两轮之间后台跑；裁剪是保底，永不失败
