"""使用无界面浏览器验证导航、选人、一问多答和历史重载。"""

from contextlib import ExitStack
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from websockets.sync.client import connect
from server.main import create_app
from tests import mock_upstream as mock
from tests.support import ROOT, free_port, running_server, seed_data

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable", "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]


class Browser:
    """最小 CDP 客户端，可启动浏览器或连接 --cdp-port 指定的浏览器。"""

    def __init__(self, port: int, proc: subprocess.Popen | None = None):
        self.port = port
        self.proc = proc
        self.ws = None
        self.n = 0
        self.session = ""
        self.logs: list[str] = []
        self.connection = ExitStack()

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
                self.ws = self.connection.enter_context(connect(info["webSocketDebuggerUrl"], max_size=64 * 1024 * 1024))
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

    def goto(self, url: str, wait: str = "") -> None:
        self.send("Page.navigate", self.session, url=self._bust(url))
        if wait:
            self.wait_for(wait)

    def close(self):
        """关闭本测试创建的浏览器；外部浏览器只断开连接。"""
        try:
            if self.ws:
                self.connection.close()
        except Exception:
            pass
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def main():
    with ExitStack() as stack:
        root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=".tmp-ui-", dir=ROOT / "tests")))
        mock.reset()
        upstream = mock.serve(0)
        stack.callback(upstream.server_close)
        stack.callback(upstream.shutdown)
        lives, session = seed_data(root, f"http://127.0.0.1:{upstream.server_port}/v1")
        mock.set_script("model-a", text="浏览器甲回答 <script>危险脚本</script>", reasoning="先想清楚问题")
        mock.set_script("model-b", text="浏览器乙回答")
        url = stack.enter_context(running_server(create_app(root, token="ui-test-token")))
        if "--cdp-port" in sys.argv:
            browser = Browser.attach(int(sys.argv[sys.argv.index("--cdp-port") + 1]))
        else:
            browser = Browser.launch(root / "browser", free_port())
        stack.callback(browser.close)
        assert browser.wait_ready(), "浏览器未启动"
        browser.new_page(url)
        assert browser.wait_for("document.querySelectorAll('.seat-chip:not(.add)').length === 2"), "提问对象未加载"
        assert browser.type('.composer textarea', '浏览器测试问题')
        assert browser.click_text('.composer button', '发给这几位')
        assert browser.wait_for("document.querySelectorAll('.answer-body').length === 2 && !document.querySelector('.answer .pill')"), "回答未完成"
        assert '浏览器甲回答' in browser.text('.answers')
        assert '浏览器乙回答' in browser.text('.answers')
        assert browser.count('.answer-body script') == 0, "回答没有转义 HTML"
        assert browser.count('.think') == 1
        assert len(mock.REQUESTS) == 2, "出现额外模型调用"

        browser.goto(url, "document.querySelectorAll('.answer-body').length === 2")
        assert '浏览器甲回答' in browser.text('.answers'), "刷新后历史未恢复"
        assert browser.click_text('.side-nav button', '数字人库')
        assert browser.wait_for("location.hash === '#/humans' && document.querySelectorAll('.human-card').length >= 3")
        labels = browser.eval("return [...document.querySelectorAll('.pc-actions button')].map(e=>e.innerText.trim())")
        assert '记忆' not in labels and '一生' not in labels
        assert browser.click_text('.side-nav button', '模型与密钥')
        assert browser.wait_for("location.hash === '#/settings' && document.querySelector('.provider-card')")
        assert 'test-secret' not in browser.text('body'), "界面泄露密钥"

        assert browser.click_text('.side-section button', '新建对话')
        assert browser.wait_for("location.hash === '#/round' && document.querySelector('.seat-chip.add') && document.querySelectorAll('.seat-chip:not(.add)').length === 0")
        assert browser.count('.round') == 0, "新对话混入旧历史"
        assert browser.click_text('.seat-chip.add', '加一位')
        assert browser.wait_for("document.querySelectorAll('.pick-cell').length === 3")
        assert browser.click_text('.pick-cell', '普通 AI')
        assert browser.click_text('.modal-foot .primary', '加进来')
        assert browser.wait_for("!document.querySelector('.modal') && document.querySelectorAll('.seat-chip:not(.add)').length === 1")
        assert browser.type('.composer textarea', '普通模型的新问题')
        assert browser.click_text('.composer button', '发给这几位')
        assert browser.wait_for("document.querySelectorAll('.answer-body').length === 1 && !document.querySelector('.answer .pill')")
        assert mock.requests_for('model-a')[-1]['body']['messages'] == [{"role": "user", "content": "普通模型的新问题"}]
        assert not browser.logs, f"浏览器控制台报错：{browser.logs}"
        print("浏览器通过：流式回答、历史重载、页面导航、选人、普通模型隔离与 HTML 转义")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
