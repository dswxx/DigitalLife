"""端到端：**真起两个服务**，走真 socket、真流式。

    py -3 dev/test_http.py

    假上游（真的 HTTP 服务，说 OpenAI 兼容的 SSE）
        ↑ httpx（真连接）
    数字生命后端（FastAPI + uvicorn，真的监听端口）
        ↑ httpx（真连接）
    这个测试（扮演浏览器）

这是整个项目里最接近真实使用的一次验证。它专门验那些**离线测不到**的东西：

  · `HttpxTransport`——唯一没法离线测的一层
  · 中文在真网络里被切成半个字（假上游按 7 字节切）
  · NDJSON 边收边读（不是等全部算完）
  · 服务真的能起来、真的只绑本机、真的拦没有令牌的请求
  · 记录真的落盘（用户看到一半关页面也不丢）

前面那些套件验的是"每一层算得对"，这一套验的是"它们接起来真的能用"。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# 依赖装在项目外（沙箱里 pip 装不进去），跑测试时手动指过去
DEPS = Path(os.environ.get("DL_DEPS") or "D:/新建文件夹/rt-deps")
if DEPS.exists():
    sys.path.insert(0, str(DEPS))

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from dev import mock_upstream as mock  # noqa: E402
from server import chat, main as appmod  # noqa: E402
from server import sessions, settings as settings_mod, store, templates  # noqa: E402
from server.adapters import core  # noqa: E402
from server.life import Life  # noqa: E402

passed = failed = 0
TOKEN = "test-token-123"


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {msg}")
    else:
        failed += 1
        print(f"  ❌ {msg}")


class SlowTransport:
    """一个很慢的假传输：用来量"派活给后台是不是立刻返回"。"""

    def __init__(self, delay: float = 1.5):
        self.delay = delay
        self.calls = 0

    def open_stream(self, *, url, headers, body, timeout):
        self.calls += 1
        time.sleep(self.delay)
        return core.Opened(status=200, chunks=iter([b"data: [DONE]\n\n"]))

    def get_json(self, *, url, headers, timeout):
        return 200, "{}"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Upstream:
    """uvicorn 跑我们的后端，线程里。"""

    def __init__(self, app, port: int):
        self.config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return True
            time.sleep(0.05)
        return False

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=5)


def read_ndjson(resp) -> list[dict]:
    """一行一行读——**边收边解析**，不是等全部结束。"""
    out = []
    for line in resp.iter_lines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def ask_stream(client, base: str, payload: dict) -> list[dict]:
    """问一句并把整条流读完。**必须用 with 管住响应**，否则连接会被提前关掉，
    服务端写到一个已经关掉的 socket 上（真出过 WinError 10054）。"""
    with client.stream("POST", f"{base}/api/ask", json=payload) as resp:
        if resp.status_code != 200:
            raise AssertionError(f"ask 返回 {resp.status_code}：{resp.read()[:200]!r}")
        return read_ndjson(resp)


def overlap(a: dict, b: dict) -> bool:
    """两个请求在时间上有没有重叠——这才是"同时回答"的证据。"""
    if not (a.get("ended") and b.get("ended")):
        return False
    return a["started"] < b["ended"] and b["started"] < a["ended"]


def main() -> int:
    root = Path(os.environ.get("DL_HTTP_DIR") or Path(__file__).resolve().parent / ".tmp-http")
    if root.exists():
        shutil.rmtree(root, ignore_errors=True)
    data_dir = root / "data"
    data_dir.mkdir(parents=True)

    up_port, app_port = free_port(), free_port()
    mock.reset()
    up = mock.serve(up_port)
    print(f"假上游 http://127.0.0.1:{up_port}\n")

    # ── 造数据：一家平台 + 一份人格 + 两位生命 ──
    templates.seed(data_dir)  # 内置模板（苏格拉底/芒格/…/裸模型）
    st = settings_mod.load(data_dir)
    st["providers"] = [{
        "id": "pv_mock", "name": "假上游", "label": "假上游", "kind": "openai",
        "baseUrl": f"http://127.0.0.1:{up_port}/v1", "apiKey": "sk-mock-secret-1234",
    }]
    st["advanced"]["maxTokens"] = 2048
    settings_mod.save(data_dir, st)

    tpl = next(t for t in templates.load_all(data_dir) if t.name == "苏格拉底")
    life_a = Life.create(data_dir / "humans", template_id=tpl.id, provider_id="pv_mock",
                         model="mock-chat", name="甲", emoji="🅰️")
    life_b = Life.create(data_dir / "humans", template_id=tpl.id, provider_id="pv_mock",
                         model="mock-chat", name="乙", emoji="🅱️")

    app = appmod.create_app(data_dir, token=TOKEN)
    srv = Upstream(app, app_port)
    if not srv.start():
        print("❌ 服务没起来")
        return 1
    base = f"http://127.0.0.1:{app_port}"
    H = {"x-digitallife-token": TOKEN}
    print(f"数字生命后端 http://127.0.0.1:{app_port}\n")

    try:
        # ── 1. 三层安全 ──
        print("── 三层安全 ──")
        with httpx.Client(timeout=20) as c:
            r = c.get(f"{base}/api/health")
            check(r.status_code == 401, f"没有令牌 → 401（实际 {r.status_code}）")
            check("刷新" in r.json().get("error", ""), "而且告诉他刷新一下页面就好，不是一句「未授权」")
            r = c.get(f"{base}/api/health", headers={"x-digitallife-token": "wrong"})
            check(r.status_code == 401, "令牌不对 → 401")
            r = c.get(f"{base}/api/health", headers={"Host": "evil.example.com", **H})
            check(r.status_code == 403, f"Host 不是本机 → 403（防 DNS rebinding，实际 {r.status_code}）")
            r = c.get(f"{base}/api/health", headers=H)
            check(r.status_code == 200, "带对令牌 → 通")
            body = r.json()
            check(body["apiVersion"] == appmod.API_VERSION, f"报了接口版本 {body['apiVersion']}")
            check(body["dataVersion"] == 9, "报了数据版本 9")
            r = c.get(f"{base}/api/nothing-here", headers=H)
            check(r.status_code == 404 and "接口" in r.json().get("error", ""),
                  f"不存在的接口是 404 JSON，不是 HTML（实际 {r.status_code}）")
            r = c.get(f"{base}/")
            check(r.status_code == 200 and TOKEN in r.text,
                  "根路径把令牌注进了 HTML（前端就是从这里拿到它的）")

        # 前端还没构建时：给一张**说清楚**的提示页，而不是白屏
        print("\n── 前端还没构建时的兜底页 ──")
        real_web = appmod.WEB_DIR
        appmod.WEB_DIR = root / "no-such-web"  # 只影响这个测试进程
        with httpx.Client(timeout=20) as c:
            r = c.get(f"{base}/")
            check(r.status_code == 200 and "还没构建" in r.text,
                  "明确说了前端还没构建——不是给一个白屏让人猜")
            check(TOKEN in r.text, "兜底页里也注了令牌（想直接调接口也能用）")
        appmod.WEB_DIR = real_web

        # 前端构建产物存在时，占位符必须被换成真正的启动数据
        print("\n── 构建产物里的启动数据注入 ──")
        web = root / "webdist"
        web.mkdir(parents=True, exist_ok=True)
        (web / "index.html").write_text(
            "<!doctype html><title>数字生命</title>"
            "<script>window.__DL_BOOT__ = /*__DL_BOOT__*/null;</script>"
            "<div id=app></div>",
            encoding="utf-8")
        appmod.WEB_DIR = web  # 只影响这个测试进程
        with httpx.Client(timeout=20) as c:
            r = c.get(f"{base}/")
            check(appmod.BOOT_PLACEHOLDER not in r.text, "占位符被整串替换掉了（连兜底值一起）")
            check('"apiVersion"' in r.text and "= {" in r.text, "注入的是一个合法的对象字面量")
            check("{{" not in r.text, "**没有变成双层花括号**（那会让整段内联脚本语法错误）")
            check("}null" not in r.text, "**也没有留下兜底的尾巴**（同上，症状都是白屏）")
            check(TOKEN in r.text, "启动数据里有令牌")
            check("<div id=app>" in r.text, "页面其余部分原样保留")
            check(c.get(f"{base}/不存在的路由").text.find("id=app") > 0,
                  "单页应用：认不出的路径也交给前端自己路由")
        appmod.WEB_DIR = Path("web/dist")

        # ── 2. 密钥不出本机 ──
        print("\n── 密钥不出本机 ──")
        with httpx.Client(timeout=20, headers=H) as c:
            got = c.get(f"{base}/api/settings").json()
            prov = got["providers"][0]
            check("sk-mock-secret-1234" not in json.dumps(got, ensure_ascii=False),
                  f"返回里没有真密钥（是掩码 {prov['apiKey']}）")
            check(prov["hasKey"] is True, "但明确告诉前端「填过了」，界面上才分得清没填和打了码")
            # 前端把掩码原样回传，不能覆盖真密钥
            c.post(f"{base}/api/settings", json={"providers": [prov]})
            again = c.get(f"{base}/api/settings").json()["providers"][0]
            check(again["hasKey"] is True, "把掩码原样回传之后，密钥还在（没有被自己的掩码覆盖）")

        # ── 3. 拉模型列表（真连接）──
        print("\n── 真去连一次上游 ──")
        with httpx.Client(timeout=20, headers=H) as c:
            r = c.post(f"{base}/api/providers/pv_mock/test").json()
            check(r["ok"] and len(r["models"]) == 2, f"平台测试通过：{r['message']}")
            models = c.post(f"{base}/api/providers/pv_mock/models").json()["models"]
            check([m["id"] for m in models] == ["mock-chat", "mock-reasoner"], "模型列表拿到了")
            check(models[0]["created"] == 1700000000000, "秒级时间戳在真链路上也归一成毫秒了")

        # ── 4. 建桌子 + 第一轮对话（真流式）──
        print("\n── 第一轮：真流式对话 ──")
        # 老会话文件（0.8.0 之前）里还留着 `mode` 字段：读的时候忽略它，
        # 保存一次之后就自然没了——**这就是 v9 不需要迁移函数的理由**，钉住它。
        legacy = data_dir / "sessions" / "th_legacy.json"
        store.write_json(legacy, {
            "id": "th_legacy", "title": "老的", "mode": "council",
            "seats": [], "createdAt": 1, "updatedAt": 1,
        })
        with httpx.Client(timeout=20, headers=H) as c:
            got = c.get(f"{base}/api/sessions").json()
            check(any(s["id"] == "th_legacy" for s in got), "带 mode 的老会话文件读得出来（不报错）")
            c.patch(f"{base}/api/sessions/th_legacy", json={"title": "老的（改过）"})
        after = json.loads(legacy.read_text(encoding="utf-8"))
        check("mode" not in after, f"保存一次之后 `mode` 就没了（{sorted(after)}）")
        legacy.unlink()

        mock.set_script("mock-chat", text="行动 > 空想。第一，先算成本；第二，别自我感动。")
        with httpx.Client(timeout=60, headers=H) as c:
            seats = [{"seatNo": 1, "lifeId": life_a.id, "providerId": "pv_mock", "model": "mock-chat"},
                     {"seatNo": 2, "lifeId": life_b.id, "providerId": "pv_mock", "model": "mock-chat"}]
            sid = c.post(f"{base}/api/sessions",
                         json={"title": "第一次测试", "seats": seats}).json()["id"]
            t0 = time.time()
            first_chunk_at = None
            events = []
            with c.stream("POST", f"{base}/api/ask",
                          json={"sessionId": sid, "question": "我该不该辞职？"}) as resp:
                check(resp.status_code == 200, "ask 返回 200")
                check(resp.headers.get("content-type", "").startswith("application/x-ndjson"),
                      f"是 NDJSON 流（{resp.headers.get('content-type')}）")
                for line in resp.iter_lines():
                    if not line.strip():
                        continue
                    if first_chunk_at is None:
                        first_chunk_at = time.time() - t0
                    events.append(json.loads(line))
            total = time.time() - t0

        kinds = [e["type"] for e in events]
        check("text" in kinds, "收到了正文块")
        check(kinds[-1] == "round_done", f"最后是 round_done（实际 {kinds[-1]}）")
        check(events[-1].get("roundId", "").startswith("r_"), "带了一轮的 id")
        check(first_chunk_at is not None and first_chunk_at < total,
              f"**是边算边吐**：第一块 {first_chunk_at:.2f}s 就到了，整轮 {total:.2f}s")
        texts = "".join(e["text"] for e in events if e["type"] == "text")
        expect = "行动 > 空想。第一，先算成本；第二，别自我感动。"
        # 并排模式下两个席位的字是**交错**来的，所以不能拿拼起来的整串去找两遍——
        # 得按 seatNo 分组。这也顺带证明了交错确实发生了（这正是"同时回答"的观感）。
        per_seat = {}
        for e in events:
            if e["type"] == "text":
                per_seat[e["seatNo"]] = per_seat.get(e["seatNo"], "") + e["text"]
        check(sorted(per_seat) == [1, 2], f"两个字都出了字（{sorted(per_seat)}）")
        check(all(v == expect for v in per_seat.values()),
              f"每位都完整、而且**中文没被切坏**（假上游按 7 字节切；实收 {len(texts)} 字）")
        # **并发**不靠"字交错"来判断：本地假上游太快，第一个席位可能整段先跑完。
        # 真正的证据是两个请求在时间上重叠（假上游记了各自的开窗和关窗时刻）。
        calls = mock.requests_for("mock-chat")
        check(len(calls) == 2 and overlap(calls[0], calls[1]),
              f"两个席位是**同时**在跑的（{calls[0]['started']:.3f}–{calls[0]['ended']:.3f} "
              f"vs {calls[1]['started']:.3f}–{calls[1]['ended']:.3f}）")
        seat_done = [e for e in events if e["type"] == "seat_done"]
        check(len(seat_done) == 2, f"两个席位各有一张完成卡（{len(seat_done)}）")
        check(all(e["tokens"] == 45 for e in seat_done), "用量从流里收上来了")
        check(all(e["finishReason"] == "stop" for e in seat_done), "结束原因也收上来了")
        check(all(e["windowSource"] == "guess" for e in seat_done),
              "平台没报窗口 → 用了保守的猜测值（宁可保守）")
        check(all(e["outputCap"] == 2048 for e in seat_done), "输出上限用了设置里的值")
        starts = [e["seatNo"] for e in events if e["type"] == "seat_start"]
        check(sorted(starts) == [1, 2], "两个席位都开了场")

        # ── 5. 记录真的落盘了 ──
        print("\n── 记录落盘（用户中途关页面也不丢）──")
        for life, name in ((life_a, "甲"), (life_b, "乙")):
            entries = life.read_session(sid)
            kinds2 = [e["kind"] for e in entries]
            check(kinds2 == ["you", "self"], f"{name} 的记录是「你说了什么 + 他说了什么」（{kinds2}）")
            check(entries[0]["text"] == "我该不该辞职？", f"{name} 记住了原问题")
            check("行动 > 空想" in entries[1]["text"], f"{name} 记住了自己说的原话")
            check(entries[1]["t"] > entries[0]["t"], f"{name} 的回答时间晚于提问时间")
        check(mock.requests_for("mock-chat")[0]["body"]["messages"][0]["role"] == "system",
              "发给上游的第一条是人格（system）")
        sent = mock.requests_for("mock-chat")[0]["body"]
        check("temperature" not in sent, "没设温度就不发这个字段")

        # ── 6. 第二轮：历史真的进了请求 ──
        print("\n── 第二轮：他记得上一轮 ──")
        with httpx.Client(timeout=60, headers=H) as c:
            c.post(f"{base}/api/ask", json={"sessionId": sid, "question": "那第一步做什么？",
                                            "seatNo": 1})
        last = mock.requests_for("mock-chat")[-1]["body"]
        contents = [m["content"] for m in last["messages"]]
        check(any("我该不该辞职？" in x for x in contents), "第二轮带上了上一轮的问题")
        check(any("行动 > 空想" in x for x in contents), "也带上了他自己上一轮的回答")
        check(last["messages"][-1]["content"] == "那第一步做什么？", "这一轮的问题在最后")
        roles = [m["role"] for m in last["messages"]]
        check(roles[0] == "system", "人格仍然作为 system 放在最前面")
        check(all(roles[i] != roles[i + 1] for i in range(len(roles) - 1)), f"user/assistant 严格交替（{roles}）")

        # ── 7. 一问多答：各自独立，听不到别人这一轮说了什么 ──
        # （0.8.0 把"会议"取消了：没有顺序发言，也没有"听到前面那位"这回事）
        print("\n── 一问多答：各自独立 ──")
        mock.reset()
        mock.set_script("mock-chat", text="我答我的。")
        with httpx.Client(timeout=60, headers=H) as c:
            sid2 = c.post(f"{base}/api/sessions",
                          json={"title": "另外问一次", "seats": seats}).json()["id"]
            c.post(f"{base}/api/ask", json={"sessionId": sid2, "question": "怎么看这件事？"})
        calls = mock.requests_for("mock-chat")
        check(len(calls) == 2, f"两位都问了（{len(calls)} 次调用）")
        for i, call in enumerate(calls):
            texts = [m["content"] for m in call["body"]["messages"]]
            check(not any("本轮此前已发言的人" in x for x in texts),
                  f"第 {i + 1} 位的请求里没有别人的话（各自独立）")
            check(not any("我答我的" in x for x in texts),
                  f"第 {i + 1} 位也没听到同轮别人的回答")
        kinds = [e["kind"] for e in life_b.read_session(sid2)]
        check("heard" not in kinds, f"记录里不再产生「听到的」（{kinds}）")
        check(sorted(set(kinds)) == ["self", "you"], f"只有「你说的」和「他说的」两种（{kinds}）")

        # ── 8. 能自愈的报错：温度被摘、max_tokens 按平台说的改 ──
        print("\n── 上游不接受我们发的参数 ──")
        mock.reset()
        mock.set_script("mock-chat", text="好", reject_keys=["temperature"])
        with httpx.Client(timeout=60, headers=H) as c:
            c.patch(f"{base}/api/sessions/{sid}", json={"seats": [
                {"seatNo": 1, "lifeId": life_a.id, "providerId": "pv_mock",
                 "model": "mock-chat", "temperature": 0.7}]})
            events = ask_stream(c, base, {"sessionId": sid, "question": "试试温度"})
        calls = mock.requests_for("mock-chat")
        check(len(calls) == 2, f"重发了一次（{len(calls)}）")
        check(len(calls) >= 2 and "temperature" in calls[0]["body"]
              and "temperature" not in calls[1]["body"],
              "第一次带温度、第二次摘掉了")
        notices = [e["text"] for e in events if e["type"] == "notice"]
        check(any("不接受自定义温度" in t for t in notices), f"黄条告诉他了：{notices}")

        print("\n── max_tokens 超出平台范围 ──")
        mock.reset()
        mock.set_script("mock-chat", text="好", reject_keys=["max_tokens"], max_ok=1024)
        with httpx.Client(timeout=60, headers=H) as c:
            c.post(f"{base}/api/settings", json={"advanced": {"maxTokens": 8192}})
            events = ask_stream(c, base, {"sessionId": sid, "question": "再来"})
        calls = mock.requests_for("mock-chat")
        got_cap = calls[-1]["body"].get("max_tokens") if calls else None
        check(got_cap == 1024, f"按平台说的 1024 重发（实际 {got_cap}）")
        learned = settings_mod.load(data_dir).get("modelInfo", {}).get("mock-chat", {})
        check(learned.get("maxOutput") == 1024, f"学到上限并落盘了（{learned}）")
        with httpx.Client(timeout=20, headers=H) as c:
            events2 = ask_stream(c, base, {"sessionId": sid, "question": "第三次"})
        tail_calls = mock.requests_for("mock-chat")
        check(bool(tail_calls) and tail_calls[-1]["body"].get("max_tokens") == 1024,
              "**下一次直接用学到的值**，不再白挨一个 400")
        notes2 = [e["text"] for e in events2 if e["type"] == "notice"]
        check(not any("超出这个模型允许的范围" in t for t in notes2),
              "而且不再重复说那句话（黄条不复读）")

        print("\n── 思考把额度吃光（老项目那个「卡在思考中」）──")
        mock.reset()
        # 第一次只给思考、正文一个字都没有，结束原因是 length —— 这正是当时的现场
        mock.set_script("mock-chat", text="这次有正文了", reasoning="想了很久很久",
                        reasoning_first=True)
        with httpx.Client(timeout=60, headers=H) as c:
            events = ask_stream(c, base, {"sessionId": sid, "question": "想久一点"})
        calls = mock.requests_for("mock-chat")
        check(len(calls) == 2, f"自动重试了一次（{len(calls)}）")
        first_cap = calls[0]["body"].get("max_tokens") if calls else 0
        second_cap = calls[1]["body"].get("max_tokens") if len(calls) > 1 else 0
        check(second_cap > first_cap,
              f"第二次把输出上限提上去了（{first_cap} → {second_cap}）")
        done = [e for e in events if e["type"] == "seat_done"]
        check(done and done[-1]["content"] == "这次有正文了", "重试之后拿到正文了")
        check(any("全用在思考上" in (e.get("text") or "") for e in events if e["type"] == "notice"),
              "并且说明白了为什么重试（不是静默重发）")
        check(done and done[-1]["reasoning"] == "想了很久很久", "思考过程也留着，没丢")

        # ── 9. 上游挂了也不能整轮消失 ──
        print("\n── 上游报错 ──")
        mock.reset()
        mock.set_script("mock-chat", status=500, error="上游炸了")
        with httpx.Client(timeout=60, headers=H) as c:
            events = ask_stream(c, base, {"sessionId": sid, "question": "挂一个看看"})
        errs = [e for e in events if e["type"] == "error"]
        check(errs and "500" in errs[0]["message"], f"错误事件带着人话：{errs[0]['message'][:60] if errs else '没有'}")
        check(events[-1]["type"] == "round_done", "整轮仍然正常收尾（不让前端永远转圈）")

        mock.reset()
        mock.set_script("mock-chat", status=401, error="invalid api key")
        with httpx.Client(timeout=60, headers=H) as c:
            events = ask_stream(c, base, {"sessionId": sid, "question": "钥匙错了"})
        msg = next(e["message"] for e in events if e["type"] == "error")
        check("API Key" in msg, f"401 翻成人话：{msg[:56]}")

        # ── 10. 记忆：手动加、召回、删 ──
        print("\n── 记忆（一辈子的事）──")
        with httpx.Client(timeout=20, headers=H) as c:
            got = c.post(f"{base}/api/lives/{life_a.id}/memory",
                         json={"name": "想辞职", "desc": "他在考虑辞职", "type": "user",
                               "text": "他在考虑辞职，主要原因是导师和课题组。"}).json()
            check(got["id"].startswith("m_"), "加了一条记忆")
            lst = c.get(f"{base}/api/lives/{life_a.id}/memory").json()
            check(len(lst["memories"]) == 1 and "想辞职" in lst["index"], "目录里有它")
            check(lst["memories"][0]["at"] > 0, "带时间戳")
            # 手动加的记忆也要能被召回
            before = mock.requests_for("mock-chat")
            c.post(f"{base}/api/ask", json={"sessionId": sid, "question": "导师那件事我该怎么办？",
                                            "seatNo": 1})
            system = mock.requests_for("mock-chat")[-1]["body"]["messages"][0]["content"]
            check("想辞职" in system and "他在考虑辞职" in system,
                  "问到导师的事时，这条记忆被召回了（目录 + 正文都进了 system）")
            r = c.patch(f"{base}/api/lives/{life_a.id}/memory/{got['id']}",
                        json={"text": "改成：他不想辞职了。"})
            check("不想辞职" in r.json()["text"], "改记忆能改")
            check(c.delete(f"{base}/api/lives/{life_a.id}/memory/{got['id']}").json()["ok"], "删记忆能删")
            check(len(c.get(f"{base}/api/lives/{life_a.id}/memory").json()["memories"]) == 0, "删完就空了")

        # ── 11. 后台整理：聊着聊着就自己长出记忆了 ──
        print("\n── 自动整理（两轮之间后台跑，用户无感）──")
        mock.reset()
        mock.set_script(
            "mock-chat",
            text="【摘要】这次在谈辞职和导师，他说过想清楚再动。",
            extract_text=json.dumps([
                {"name": "考虑辞职", "desc": "他在考虑辞掉课题组", "type": "user",
                 "scope": "persistent", "text": "他在考虑辞职，和导师关系紧张。"},
                {"name": "不想听空话", "desc": "他要求回答别绕弯子", "type": "feedback",
                 "scope": "persistent", "text": "他明确说过别绕弯子，直接说结论。"},
            ], ensure_ascii=False),
        )
        with httpx.Client(timeout=90, headers=H) as c:
            # 把窗口调到小模型那种规模，再聊厚一点，才到得了水位线。
            # 128k 的窗口下那点原文根本不需要压——这是对的，不是没生效。
            c.post(f"{base}/api/settings", json={"advanced": {"contextWindow": 12000}})
            for i in range(8):
                c.post(f"{base}/api/ask", json={"sessionId": sid, "seatNo": 1,
                                                "question": f"第 {i} 个问题：" + "背景" * 400})

        # 后台是异步的：等它落地（最多 30 秒）
        settled = False
        for _ in range(60):
            if life_a.summary_text(sid) and life_a.memories():
                settled = True
                break
            time.sleep(0.5)
        check(settled, "**没按任何按钮**，他自己压出了摘要、也记住了事")
        check(life_a.summary_text(sid) != "", f"摘要：{life_a.summary_text(sid)[:30]}")
        check(len(life_a.memories()) >= 2, f"记住了 {len(life_a.memories())} 条长期记忆")
        check("考虑辞职" in life_a.index_text(), "进了他的记忆目录（下次对话就能被召回）")
        check(len(life_a.read_session(sid)) > len(life_a.read_session(sid, only_tail=True)),
              "原文一条没少（只追加），但请求路径从此只读尾巴")
        sent = [r for r in mock.requests_for("mock-chat")
                if "数字生命" in json.dumps(r["body"].get("messages") or [], ensure_ascii=False)]
        check(bool(sent), "后台那次调用确实发了（用压缩/提取的提示词，不是人格）")
        check(life_a.compaction(sid).get("usage", {}).get("promptTokens", 0) > 0,
              "整理花的 token 记进账了（越老越贵，要看得见）")

        # 刚整理过就别再花一次钱
        with httpx.Client(timeout=90, headers=H) as c:
            again = c.post(f"{base}/api/lives/{life_a.id}/sessions/{sid}/compact", json={}).json()
        check(again["compaction"] is None, "刚整理过再按「现在整理一次」→ 什么都不做（不白花一次调用）")
        check(len(life_a.memories()) == 2, f"记忆条数没有翻倍（{len(life_a.memories())}）")

        print("\n── 「不阻塞」要能量出来 ──")
        slow = SlowTransport(delay=1.5)
        t0 = time.time()
        chat.schedule_housekeeping(data_dir, {1: life_a}, sid, settings_mod.load(data_dir), slow)
        took = time.time() - t0
        check(took < 0.3, f"派活给后台是**立刻返回**的（{took * 1000:.0f}ms，模型那边要 1.5 秒）")
        time.sleep(2.0)  # 让它跑完，别影响后面的断言

        print("\n── 裸模型不积累记忆 ──")
        bare_tpl = next((t for t in templates.load_all(data_dir) if t.bare), None)
        bare_life = Life.create(data_dir / "humans", template_id=bare_tpl.id, provider_id="pv_mock",
                                model="mock-chat", name="裸的")
        mock.set_script("mock-chat", text="裸模型的一句话回答。")
        with httpx.Client(timeout=60, headers=H) as c:
            made = c.post(f"{base}/api/sessions",
                          json={"title": "只有裸模型",
                                "seats": [{"seatNo": 1, "lifeId": bare_life.id,
                                           "providerId": "pv_mock", "model": "mock-chat"}]}).json()
            bare_sid = made["id"]  # **用它给的那个 id**，别自己编一个
            for i in range(4):
                c.post(f"{base}/api/ask", json={"sessionId": bare_sid,
                                                "question": f"问题 {i}：" + "字" * 300})
        time.sleep(1.5)  # 给后台一点时间（如果有的话）
        check(bare_life.summary_text(bare_sid) == "" and not bare_life.memories(),
              "裸模型不压缩也不记长期记忆——它是对打的探针，不是陪伴（设计文档里的承诺）")
        check(len(bare_life.read_session(bare_sid)) >= 8,
              f"但对话记录照旧留着（原文是真相，实际 {len(bare_life.read_session(bare_sid))} 条）")
        # 收拾干净，别影响后面数数
        with httpx.Client(timeout=20, headers=H) as c:
            c.delete(f"{base}/api/sessions/{bare_sid}")
            c.delete(f"{base}/api/lives/{bare_life.id}")
        check(not (bare_life.root / "identity.json").exists(), "裸的那位删掉了（连同他的记录）")

        # ── 12. 一生（时间线）──
        print("\n── 一生 ──")
        with httpx.Client(timeout=20, headers=H) as c:
            tl = c.get(f"{base}/api/lives/{life_a.id}/timeline").json()
            check(len(tl["days"]) >= 1, f"时间线有 {len(tl['days'])} 天")
            check(any(d["talked"] > 0 for d in tl["days"]), "哪天聊过都算得出来")
            check(any(d["sessions"] for d in tl["days"]), "而且能跳回那次会话")

        # ── 13. 模板与生命的管理 ──
        print("\n── 数字人库 ──")
        with httpx.Client(timeout=20, headers=H) as c:
            lives = c.get(f"{base}/api/lives").json()
            check(len(lives) == 2, f"两位生命（{len(lives)}）")
            check(all(x["sessionCount"] >= 1 and x["name"] for x in lives), "每张卡都有聊过几次和名字")
            boot = c.get(f"{base}/api/bootstrap").json()
            check(len(boot["templates"]) >= 7 and len(boot["lives"]) == 2, "bootstrap 一次给全（模板 + 生命 + 桌子）")
            check("memTypes" in boot, "连记忆类型一起给了，前端不用写死")
            # 改模板会让 id 变，用着它的生命要跟着指过去
            old_tid = tpl.id
            newt = c.patch(f"{base}/api/templates/{old_tid}",
                           json={"system": tpl.system + "\n补充一句。"}).json()
            check(newt["id"] != old_tid, f"改了正文 → id 变了（内容算出来的）")
            refetched = c.get(f"{base}/api/lives/{life_a.id}").json()
            check(refetched["identity"]["templateId"] == newt["id"],
                  "用着它的生命自动指到了新模板（不然他会突然「没有模板」）")
            check(not (data_dir / "templates" / f"{old_tid}.json").exists(), "老模板文件删掉了，不留两份")
            # 还有人在用就不许删
            r = c.delete(f"{base}/api/templates/{newt['id']}")
            check(r.status_code == 400 and "用着" in r.json()["error"],
                  f"有人用着就不让删，并说清是谁：{r.json().get('error','')[:40]}")
            r = c.post(f"{base}/api/lives", json={"templateId": "tpl_nope", "providerId": "pv_mock"})
            check(r.status_code == 400, "选一个不存在的模板 → 400")
            r = c.post(f"{base}/api/lives", json={"templateId": newt["id"], "providerId": "pv_nope"})
            check(r.status_code == 400 and "平台" in r.json()["error"], "选一家不存在的平台 → 400")

    finally:
        srv.stop()
        up.shutdown()

    print(f"\n{'=' * 46}\n通过 {passed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
