"""真浏览器 E2E（CDP，不依赖 Playwright）。

    py -3 dev/test_ui.py
    py -3 dev/test_ui.py --shots     # 顺便把截图存到 dev/.tmp-ui/shots/

为什么不用 Playwright：它的 Python 绑定要靠**命名管道**跟 Node 驱动通信，
这台机器的沙箱明确不许开命名管道（PermissionError: WinError 5）。
所以自己起一个 Chrome/Edge（stdio 指向 NUL，不碰管道），
再用 CDP over TCP 驱动它——能力上只少一点，够验界面了。

验的是：**界面在真浏览器里真的长出来、真的能点、真的能流式出字**。
test_http 验的是"接口对不对"，这个验的是"人打开看到的是什么"。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
DEPS = Path(os.environ.get("DL_DEPS") or "D:/新建文件夹/rt-deps")
if DEPS.exists():
    sys.path.insert(0, str(DEPS))

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

from dev import mock_upstream as mock  # noqa: E402
from server import main as appmod  # noqa: E402
from server import migrate, sessions, settings as settings_mod, store, templates  # noqa: E402
from server.life import Life  # noqa: E402

passed = failed = 0
SHOTS = "--shots" in sys.argv
REAL_SRC = Path("D:/AAsanboxForDsh/data")
TOKEN = "ui-test-token"
BROWSERS = [
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    # Linux（GitHub Actions 的 ubuntu runner 自带 Chrome）
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    # macOS
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {msg}")
    else:
        failed += 1
        print(f"  ❌ {msg}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Browser:
    """一个够用的 CDP 客户端。

    **两种用法**：
      · `attach(port)` —— 连一个已经开着的浏览器（推荐）
      · `launch(dir, port)` —— 自己起一个

    为什么推荐前者：受限环境里由脚本 spawn 浏览器会失败
    （Chrome 的 IPC 要命名管道；某些环境下从 Python 里 spawn 会直接退 21）。
    先用一条命令把浏览器起好，之后**跑多少次测试都不用再起**，迭代快得多：

        & "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe" `
          --headless=new --remote-debugging-port=9333 `
          --user-data-dir=D:\\某处\\chrome-profile --disable-gpu about:blank
    """

    def __init__(self, port: int, proc: subprocess.Popen | None = None):
        self.port = port
        self.proc = proc
        self.ws = None
        self.n = 0
        self.session = ""
        self.logs: list[str] = []

    @classmethod
    def attach(cls, port: int) -> "Browser":
        return cls(port)

    @classmethod
    def launch(cls, profile: Path, port: int) -> "Browser":
        exe = next((p for p in BROWSERS if Path(p).exists()), "")
        if not exe:
            raise RuntimeError("找不到 Chrome 或 Edge")
        profile.mkdir(parents=True, exist_ok=True)
        proc = subprocess.Popen(
            [exe, "--headless=new", f"--remote-debugging-port={port}",
             f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
             "--disable-gpu", "--window-size=1440,1000", "about:blank"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return cls(port, proc)

    def wait_ready(self, seconds: float = 30) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/version", timeout=2) as r:
                    info = json.loads(r.read())
                self.ws = connect(info["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024)
                return True
            except Exception:
                time.sleep(0.4)
        return False

    def send(self, method: str, session_id: str | None = None, **params):
        """发一条 CDP 命令。`sessionId` 必须是**消息顶层字段**，不能塞进 params。"""
        self.n += 1
        mid = self.n
        msg: dict = {"id": mid, "method": method, "params": params}
        if session_id:
            msg["sessionId"] = session_id
        self.ws.send(json.dumps(msg))
        while True:
            got = json.loads(self.ws.recv())
            if got.get("id") == mid:
                if "error" in got:
                    raise RuntimeError(f"{method} 失败：{got['error']}")
                return got.get("result", {})
            self._collect(got)

    def _collect(self, event: dict) -> None:
        """顺手收控制台报错——前端坏了最常见的表现就是这里红一片。"""
        method = event.get("method")
        p = event.get("params") or {}
        if method == "Runtime.consoleAPICalled" and p.get("type") == "error":
            text = " ".join(str(a.get("value") or a.get("description") or "") for a in p.get("args") or [])
            self.logs.append(text[:300])
        elif method == "Runtime.exceptionThrown":
            d = p.get("exceptionDetails") or {}
            self.logs.append((d.get("text") or "") + " " + str((d.get("exception") or {}).get("description") or ""))

    @staticmethod
    def _bust(url: str) -> str:
        """加一个变化的查询串防缓存。

        **必须插在 `#` 前面**：这个前端用 hash 路由（`#/chat`），
        拼到 `#` 后面就变成 hash 的一部分了（`#/chat?_=123`），
        路由认不出这是哪一页——表现出来就是"跳过去却是另一个页面"。
        """
        token = f"_={int(time.time() * 1000)}"
        head, _, frag = url.partition("#")
        sep = "&" if "?" in head else "?"
        return f"{head}{sep}{token}#{frag}" if frag else f"{head}{sep}{token}"

    def new_page(self, url: str) -> None:
        tid = self.send("Target.createTarget", url=self._bust(url))["targetId"]
        self.session = self.send("Target.attachToTarget", targetId=tid, flatten=True)["sessionId"]
        self.send("Page.enable", self.session)
        self.send("Runtime.enable", self.session)

    def eval(self, expression: str):
        got = self.send("Runtime.evaluate", self.session,
                        expression=f"(async () => {{ {expression} }})()",
                        awaitPromise=True, returnByValue=True)
        if got.get("exceptionDetails"):
            raise RuntimeError(str(got["exceptionDetails"].get("text") or got["exceptionDetails"])[:300])
        return got.get("result", {}).get("value")

    def wait_for(self, expression: str, seconds: float = 20) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            try:
                if self.eval(f"return !!({expression})"):
                    return True
            except Exception:
                pass
            time.sleep(0.22)
        return False

    def text(self, selector: str) -> str:
        return self.eval(f"const e=document.querySelector({json.dumps(selector)}); return e?e.innerText:''") or ""

    def count(self, selector: str) -> int:
        return int(self.eval(f"return document.querySelectorAll({json.dumps(selector)}).length") or 0)

    def last_round_count(self, selector: str) -> int:
        """只在**最新那一轮**里数。

        时间线上还挂着前几轮（那是设计：问句和回答都留着），
        所以在整页范围里数会把上一轮的东西算进来——断言就骗人了。
        """
        return int(self.eval(
            "const rs=[...document.querySelectorAll('.round')];"
            f"return rs.length ? rs[rs.length-1].querySelectorAll({json.dumps(selector)}).length : 0") or 0)

    def last_round_text(self, selector: str) -> str:
        return self.eval(
            "const rs=[...document.querySelectorAll('.round')];"
            f"if(!rs.length) return ''; const e=rs[rs.length-1].querySelector({json.dumps(selector)});"
            "return e ? e.innerText : ''") or ''

    def click_text(self, selector: str, text: str, scope: str = "document") -> bool:
        """点某个按钮——**按它写的字找**，因为在真页面里没有 test-id 可用。"""
        return bool(self.eval(f"""
            const root = {scope};
            const el = [...root.querySelectorAll({json.dumps(selector)})]
              .find(e => e.innerText.trim().includes({json.dumps(text)}));
            if (!el) return false;
            el.click(); return true"""))

    def type(self, selector: str, value: str) -> bool:
        """塞字并**触发 input 事件**——Vue 的 v-model 靠它才知道值变了。"""
        return bool(self.eval(f"""
            const e = document.querySelector({json.dumps(selector)});
            if (!e) return false;
            e.focus();
            const proto = e.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
            Object.getOwnPropertyDescriptor(proto.prototype, 'value').set.call(e, {json.dumps(value)});
            e.dispatchEvent(new Event('input', {{ bubbles: true }}));
            return true"""))

    def shot(self, path: Path) -> bool:
        got = self.send("Page.captureScreenshot", self.session, format="png")
        if not got.get("data"):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(got["data"]))
        return True

    def goto(self, url: str, wait: str = "") -> None:
        self.send("Page.navigate", self.session, url=self._bust(url))
        if wait:
            self.wait_for(wait)

    def close(self):
        """只断开连接。**浏览器不关**——下一次跑测试还能接着用，起一次要好几秒。"""
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class Server:
    def __init__(self, app, port: int):
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self) -> bool:
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return True
            time.sleep(0.05)
        return False

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


def main() -> int:
    cdp_port = 0
    for i, arg in enumerate(sys.argv):
        if arg == "--cdp-port" and i + 1 < len(sys.argv):
            cdp_port = int(sys.argv[i + 1])
    root = Path(os.environ.get("DL_UI_DIR") or Path(__file__).resolve().parent / ".tmp-ui")
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    data_dir = root / "data"
    data_dir.mkdir(parents=True)

    # ── 数据：迁移老项目那 15 位（真实规模）──
    migrated = migrate.migrate(REAL_SRC, data_dir) if REAL_SRC.exists() else None
    templates.seed(data_dir)
    if migrated:
        print(f"（迁移了 {migrated.humans_total} 位数字人做测试数据）")

    up_port, app_port, dbg_port = free_port(), free_port(), free_port()
    mock.reset()
    mock.serve(up_port)

    st = settings_mod.load(data_dir)
    st["providers"] = [{
        "id": "pv_mock", "name": "假上游", "label": "假上游", "kind": "openai",
        "baseUrl": f"http://127.0.0.1:{up_port}/v1", "apiKey": "sk-mock-secret-1234",
        "models": [{"id": "mock-chat"}],
    }]
    settings_mod.save(data_dir, st)

    # 给两位真实迁移过来的生命配上这家平台
    lives = []
    for old_id in ("dh_a932252c3521", "dh_42005066efca"):
        life = Life.load(data_dir / "humans" / templates.stable_id("dh", old_id))
        life.identity["providerId"] = "pv_mock"
        life.identity["model"] = "mock-chat"
        store.write_json(life.root / "identity.json", life.identity)
        lives.append(life)

    sess = sessions.create(data_dir, title="新的圆桌", mode="parallel", seats=[
        {"seatNo": 1, "lifeId": lives[0].id, "providerId": "pv_mock", "model": "mock-chat"},
        {"seatNo": 2, "lifeId": lives[1].id, "providerId": "pv_mock", "model": "mock-chat"},
    ])

    app = appmod.create_app(data_dir, token=TOKEN)
    srv = Server(app, app_port)
    if not srv.start():
        print("❌ 后端没起来")
        return 1
    base = f"http://127.0.0.1:{app_port}"
    H = {"x-digitallife-token": TOKEN}

    # 先聊一轮，好让对话页一打开就有东西看
    mock.set_script("mock-chat", text="**停。**你在给自己找理由。\n\n第一，先算成本；第二，别自我感动。")
    with httpx.Client(timeout=60, headers=H) as c:
        with c.stream("POST", f"{base}/api/ask", json={"sessionId": sess.id, "question": "我该不该辞职？"}) as r:
            for _ in r.iter_lines():
                pass

    if cdp_port:
        br = Browser.attach(cdp_port)
    else:
        br = Browser.launch(root / "chrome", dbg_port)
    if not br.wait_ready():
        print("❌ 连不上浏览器。可以先用这条命令把它起好，再加 --cdp-port 指过来：\n")
        print(f'  & "{BROWSERS[0]}" --headless=new --remote-debugging-port={dbg_port} '
              f'--user-data-dir="{root / "chrome"}" --disable-gpu about:blank\n')
        srv.stop()
        return 1
    print(f"后端 {base} · 浏览器 CDP {br.port}\n")

    try:
        # ── 1. 库 ──
        print("── 数字人库 ──")
        br.new_page(f"{base}/#/humans")
        check(br.wait_for("document.querySelectorAll('.human-card.is-tpl').length > 0"), "页面起来了，模板卡片渲染出来了")
        check("数字人库" in br.text("h1"), f"标题对：{br.text('h1')}")
        tpl_n, life_n = br.count(".human-card.is-tpl"), br.count(".human-card:not(.is-tpl)")
        check(tpl_n >= 9, f"模板区 {tpl_n} 份蓝图（真实数据迁移来的）")
        check(life_n == 15, f"厂商区 {life_n} 位活着的个体")
        check("孙宇晨" in br.text(".page"), "迁移过来的数字人出现在库里")
        vendors = br.eval("return [...document.querySelectorAll('.category-head .category-name')].map(e=>e.innerText.trim())")
        check(len(vendors) >= 2, f"按厂商分了组：{vendors}")
        card = br.text(".human-card:not(.is-tpl)")
        check("聊过" in card and "记 " in card, f"卡片上有信息：{' / '.join(card.splitlines()[:5])}")
        check("出生" not in card and "2026-" not in card, "没有在卡片上堆日期（你说过不要）")
        check(br.eval("return !!document.querySelector('.human-card:not(.is-tpl) .btn')"), "卡片上有按钮")
        check(not br.eval("return document.body.scrollWidth > window.innerWidth + 4"),
              "没有横向滚动条（横向空间用起来了，没有溢出）")
        # 版本号是**从 /api/health 读的**，不是前端写死的——
        # 盯着它，因为写死过一次（改了后端版本，页面上还在骗人）。
        # 断言方式：跟接口报的值比，**不要写死版本号**（不然每次发版都得改测试）。
        # 它在「模型与密钥」这一页的页头上（老界面把它放在顶栏，骨架改了位置没改）。
        health = httpx.get(f"{base}/api/health", headers=H, timeout=10).json()
        br.eval("location.hash = '#/settings'; return 1")
        check(br.wait_for("document.querySelector('.page-head')"), "设置页起来了")
        foot = br.text(".page-head")
        check(health["version"] in foot, f"页头显示的是后端报的版本 {health['version']}：{foot}")
        check(f"数据格式 v{health['dataVersion']}" in foot, f"数据版本也是从接口来的：{foot}")
        # 版本在设置页头上（老界面挂在顶栏），所以看完得回库——后面几段都在库上跑。
        br.goto(f"{base}/#/humans", "document.querySelectorAll('.human-card:not(.is-tpl)').length > 0")
        if SHOTS:
            br.shot(root / "shots" / "1-library.png")

        # ── 2. 记忆抽屉 ──
        print("\n── 记忆（抽屉）──")
        br.eval("""
            const card = document.querySelectorAll('.human-card:not(.is-tpl)')[0];
            [...card.querySelectorAll('button')].find(b => b.innerText.trim() === '记忆').click();
        """)
        check(br.wait_for("document.querySelector('.modal')"), "记忆抽屉打开了")
        check("的记忆" in br.text(".modal-head"), f"标题对：{br.text('.modal-head')}")
        check(br.eval("return location.hash.includes('/memory')"), f"地址栏记住了位置：{br.eval('return location.hash')}")
        br.click_text(".modal-head button", "手动加一条")
        check(br.wait_for("document.querySelector('.modal-body input')"), "能手动加一条记忆")
        br.type(".modal-body input", "测试记忆")
        br.eval(f"""
            const t = document.querySelector('.modal-body textarea');
            Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype,'value').set.call(t, '他知道自己在测试。');
            t.dispatchEvent(new Event('input', {{ bubbles: true }}));
            [...document.querySelectorAll('.modal-body button')].find(b => b.innerText.includes('写进去')).click();
        """)
        check(br.wait_for("[...document.querySelectorAll('.mem-item .mem-name')].some(e=>e.innerText.includes('测试记忆'))", 12),
              "写进去了，列表里看得到")
        check("他知道自己在测试" in br.text(".modal-body"), "正文也在（记忆是给人读的）")
        check("来自" in br.text(".mem-item .mem-foot"), "每条都标着来源")
        if SHOTS:
            br.shot(root / "shots" / "2-memory.png")
        br.eval("""
            const c = [...document.querySelectorAll('.mem-item')].find(x => x.innerText.includes('测试记忆'));
            [...c.querySelectorAll('button')].find(b => b.innerText.trim() === '删').click();
        """)
        br.wait_for("document.querySelector('.mem-item .btn.danger')")
        br.eval("""
            const c = [...document.querySelectorAll('.mem-item')].find(x => x.innerText.includes('测试记忆'));
            [...c.querySelectorAll('button')].find(b => b.innerText.includes('真的删掉')).click();
        """)
        check(br.wait_for("![...document.querySelectorAll('.mem-item .mem-name')].some(e=>e.innerText.includes('测试记忆'))", 12),
              "删掉那一条（他记错了你能直接删）")

        # ── 3. 一生 ──
        print("\n── 一生（时间线）──")
        br.click_text(".modal-head button", "关闭")
        br.wait_for("!document.querySelector('.modal')")
        br.eval("location.hash = '#/humans'; return 1")
        br.wait_for("document.querySelectorAll('.human-card:not(.is-tpl)').length > 0")
        br.eval("""
            const card = document.querySelectorAll('.human-card:not(.is-tpl)')[0];
            [...card.querySelectorAll('button')].find(b => b.innerText.trim() === '一生').click();
        """)
        check(br.wait_for("document.querySelector('.tl-day')"), "时间线出来了，按天分组")
        check("出生" in br.text(".modal-head"), f"标题带出生时间：{br.text('.modal-head')}")
        check(br.count(".tl-day") >= 1, f"{br.count('.tl-day')} 天有记录")
        check("次对话" in br.text(".modal-body") or "条记忆" in br.text(".modal-body"), "汇总数字在")
        if SHOTS:
            br.shot(root / "shots" / "3-timeline.png")

        # ── 4. 对话：真流式 ──
        print("\n── 圆桌对话（真流式）──")
        br.eval("location.hash = '#/round'; return 1")
        check(br.wait_for("document.querySelectorAll('.answer').length > 0"), "对话页有席位卡")
        check(br.count(".answer") == 2, f"{br.count('.answer')} 位在桌上")
        check(br.wait_for("document.body.innerText.includes('我该不该辞职')", 10),
              "回到这一页能看到上一轮问的是什么（问句也是对话的一半）")
        check("先算成本" in br.text(".answer"), "上次的回答也在")
        check(br.eval("return !!document.querySelector('.answer .answer-body strong')"),
              "Markdown 渲染了（**停。** 变成了粗体）")
        mock.set_script("mock-chat", text="这是新的一轮回答，边写边给你看。", chunk_delay=0.06)
        br.type(".composer textarea", "那第一步做什么？")
        br.click_text(".composer button", "发给全桌")
        check(br.wait_for("document.querySelector('.cursor')", 10),
              "出现光标——**边算边出字**（假上游每块之间停 60ms，才看得见这个瞬间）")
        check(br.wait_for("!document.querySelector('.cursor')", 25), "答完了，光标收了")
        check(br.wait_for("document.body.innerText.includes('这是新的一轮回答')", 15),
              "新回答渲染出来了")
        got = br.last_round_text(".answers")
        check(got.count("这是新的一轮回答") == 2, "两位都答了（并排：同时听到同一句问题）")
        check(br.wait_for("document.querySelector('.answer .answer-foot')", 10), "卡片底部有用量那一行")
        check(br.wait_for(
            "[...document.querySelectorAll('.answer .answer-foot')].some(e => e.innerText.includes('token'))", 10),
            f"用量显示出来了（越老越贵要看得见）：{br.text('.answer .answer-foot')[:40]}")
        check(not br.count(".answer-note.err"), "没有红色错误条")
        check(br.eval("return document.querySelector('.composer textarea').value === ''"),
              "问完清空输入框（不让人以为没发出去）")

        # 加一个席位——这是**用户第一件要做的事**（桌上一开始没人）。
        # 弹窗里是两步：先点那位数字人，再点「加进来」。只点人什么都不会发生，
        # 所以这里两步都走，并且断言座位真的多了一个。
        seats_before = br.count(".seat-chip:not(.add)")
        br.click_text(".seat-bar button", "添加席位")
        check(br.wait_for("document.querySelector('.modal')"), "席位弹窗打开了")
        picked = br.eval(
            "const b=[...document.querySelectorAll('.modal-body button')][0];"
            "if(!b) return ''; const t=b.innerText.trim(); b.click(); return t")
        check(bool(picked), f"在弹窗里挑了一位：{picked}")
        br.click_text(".modal-foot button", "加进来")
        check(br.wait_for(f"document.querySelectorAll('.seat-chip:not(.add)').length === {seats_before + 1}", 10),
              f"「加进来」之后桌上多了一位（{seats_before} → {seats_before + 1}）")
        br.eval("const x=document.querySelectorAll('.seat-chip:not(.add) .seat-del');"
                "x[x.length-1].click(); return 1")
        check(br.wait_for(f"document.querySelectorAll('.seat-chip:not(.add)').length === {seats_before}", 10),
              "点 ✕ 又能把他撤下来（座位号跟着重排）")
        if SHOTS:
            br.shot(root / "shots" / "4-chat.png")

        # ── 5. 黄条不复读 ──
        print("\n── 黄条不复读 ──")
        mock.reset()
        mock.set_script("mock-chat", text="好的。", reject_keys=["temperature"])
        with httpx.Client(timeout=30, headers=H) as c:
            c.patch(f"{base}/api/sessions/{sess.id}", json={"seats": [
                {"seatNo": 1, "lifeId": lives[0].id, "providerId": "pv_mock",
                 "model": "mock-chat", "temperature": 0.7}]})
        br.goto(f"{base}/#/round")
        check(br.wait_for("document.querySelectorAll('.answer').length === 1", 20),
              f"座位改成 1 个了（实际 {br.count('.answer')}）")
        br.type(".composer textarea", "带温度地问一句")
        br.click_text(".composer button", "发给全桌")
        # 先确认这一轮真的发生了——不然"没有黄条"这个结论毫无意义
        ran = br.wait_for("document.body.innerText.includes('好的')", 25)
        check(ran, "这一轮真的跑起来了（否则下面的断言是假通过）")
        check(br.wait_for(
            "(() => {const rs=[...document.querySelectorAll('.round')];"
            "return rs.length && !!rs[rs.length-1].querySelector('.answer-note')})()", 10),
            f"出现了黄条（平台不接受温度）：{br.last_round_text('.answer-note')[:60]}")
        first = br.last_round_count(".answer-note")
        br.wait_for("!document.querySelector('.cursor')", 25)
        rounds_before = br.count(".round")
        br.type(".composer textarea", "再问一次")
        br.click_text(".composer button", "发给全桌")
        check(br.wait_for(f"document.querySelectorAll('.round').length > {rounds_before}", 10),
              "第二轮真的发出去了（不然下面的「没有复读」是假通过）")
        br.wait_for("!document.querySelector('.cursor')", 25)
        second = br.last_round_count(".answer-note")
        check(second < first or (first >= 1 and second == 0),
              f"第二轮没有再说一遍那句话（{first} → {second}）")

        # ── 6. 设置 ──
        print("\n── 设置 ──")
        br.eval("location.hash = '#/settings'; return 1")
        check(br.wait_for("document.querySelector('.category-block')"), "设置页打开了")
        body = br.text(".page")
        check("假上游" in body, "平台列出来了")
        check("sk-mock-secret-1234" not in body, "**密钥没有明文出现在页面上**（后端只回掩码）")
        check(br.count("input[type=password]") >= 1, "密钥框是密码框")
        check("单次回答最长 token" in body, "高级设置在上面")
        check("上限不是目标" in body, "写清了「这是上限不是目标」（免得有人以为调大就答更长）")
        if SHOTS:
            br.shot(root / "shots" / "5-settings.png")

        # ── 7. 新建流程（两步）──
        print("\n── 新建数字人（两步）──")
        br.goto(f"{base}/#/humans", "document.querySelectorAll('.human-card:not(.is-tpl)').length > 0")
        # 点主区那个「＋ 新建数字人」——左侧栏也有个「＋ 新建圆桌」，
        # 按字找会先撞上它（而且它是异步建桌子，看着像没反应）。
        br.click_text(".page button", "＋ 新建数字人")
        check(br.wait_for("document.querySelector('.steps')"), "创建流程打开了")
        steps = br.text(".steps")
        check("1" in steps and "2" in steps, f"两步的编号只在它该在的地方：{steps.replace(chr(10), ' ')}")
        br.eval("const t=[...document.querySelectorAll('.modal .human-card.is-tpl')][1]; t.click(); return 1")
        br.click_text("button", "下一步")
        check(br.wait_for("document.body.innerText.includes('他会在这家平台上活着')"), "第二步：选平台")
        br.click_text(".modal-body button", "假上游")
        check(br.wait_for("document.querySelectorAll('.modal-body select, .modal-body input[type=text]').length > 0", 8),
              "选完平台之后能选模型（列表或手填）")
        br.click_text("button", "创建")
        check(br.wait_for("location.hash.startsWith('#/round')", 15),
              "创建完直接把他放到桌上、跳到对话页（下一步就是能聊）")
        br.goto(f"{base}/#/humans", "document.querySelectorAll('.human-card:not(.is-tpl)').length > 0")
        check(br.count(".human-card:not(.is-tpl)") == 16, f"创建完库里有 16 位了（实际 {br.count('.human-card:not(.is-tpl)')}）")
        if SHOTS:
            br.shot(root / "shots" / "6-created.png")

        # ── 8. 控制台干净 ──
        print("\n── 控制台 ──")
        real = [x for x in br.logs if x.strip() and "favicon" not in x.lower()]
        check(not real, f"浏览器控制台没有报错" + (f"（{len(real)} 条：{real[:2]}）" if real else ""))

    finally:
        br.close()
        srv.stop()

    print(f"\n{'=' * 46}\n通过 {passed}，失败 {failed}")
    if SHOTS:
        print(f"截图在 {root / 'shots'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
