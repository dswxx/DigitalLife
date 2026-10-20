# 数字生命 DigitalLife

问一次，听不同 AI 怎么想；读完一本书，试着与作者的思想交流。

项目只保留三件事：配置平台与模型、与多位 AI 同时聊天、创建具有人格和独立对话历史的数字人。
普通 AI 使用本会话的历史；数字人使用自己从创建起积累的历史。各人只看到用户的问题和自己的回答。

## 启动

需要 Python ≥ 3.11，以及 Node.js 20.19+ 或 22.12+。

```powershell
py -3 -m pip install -r requirements.txt
cd web
npm ci
npm run build
cd ..
py -3 run.py
```

也可以双击 `启动数字生命.cmd`。默认地址为 `http://127.0.0.1:8787`，端口被占用时会另选端口。
默认数据目录为项目内的 `data/`；可用 `--data` 指定其他目录。首次启动创建内置人格。

## 结构与数据

```text
server/        Python + FastAPI：接口、运行时、聊天、上下文、文件存储与协议适配
web/           Vue 3 + TypeScript 前端
tests/         合成数据、假上游和回归测试
scripts/       测试入口
docs/          系统设计与开发指南
run.py         本机服务启动入口

data/
├── settings.json                    平台、密钥与模型预算
├── templates/<id>.json               人格蓝图
├── sessions/<id>/info.json           会话标题、参与者与轮数
├── sessions/<id>/messages.jsonl      完整会话问答与参与者快照
├── humans/<id>/identity.json         实体身份、人格与平台绑定
├── humans/<id>/history.jsonl         该实体从创建起的全部亲历对话
└── .trash/                           已删除实体的回收目录
```

聊天原文按行追加。会话保存完整问答，实体历史保存自己收到的问题和自己的回答；保留这两处落盘。
删除会话保留实体历史；删除实体移入回收目录，既有会话仍可查看完整问答。

启动加载设置、人格、身份和会话概况。打开会话后复用它的记录；实体完整历史在首次参与请求时加载并复用。
模型请求只有 `system + 历史原文 + 当前问题`。窗口不足时少带较早的原文，磁盘记录不变；人格和当前问题本身超限时明确报错。

服务仅监听本机，API 使用入口页面注入的令牌。密钥只保存在本机配置中，前端收到掩码。
`data/` 与 `.backups/` 不进入版本库。

## 检查

当前版本 **0.12.0**，API v7，数据格式 v12。

```powershell
py -3 -m pip install -r requirements-dev.txt
py -3 scripts/check.py --unit    # 核心、协议与文档
py -3 scripts/check.py           # 再包含本机 HTTP 测试
py -3 scripts/check.py --ui      # 再包含浏览器测试，需要已构建前端及 Chrome 或 Edge
```

测试使用独立临时目录和本机假上游，不读取个人聊天、不调用真实模型。

如无必要，勿增实体。后续功能从现有 Python 函数和上下文组装入口按需增加。

文档：[系统设计](docs/系统设计.md) · [开发指南](docs/开发指南.md) · [更新日志](CHANGELOG.md)。
采用 [MIT 许可](LICENSE)。
