"""测试用本机 HTTP 上游，支持脚本化响应、SSE 分块与请求记录。"""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 每个模型的行为由这个表决定，测试要什么就配什么
SCRIPT: dict[str, dict] = {}
REQUESTS: list[dict] = []
LOCK = threading.Lock()


def set_script(model: str, **kw) -> None:
    SCRIPT[model] = kw


def requests_for(model: str) -> list[dict]:
    return [r for r in REQUESTS if r["body"].get("model") == model]


def reset() -> None:
    SCRIPT.clear()
    REQUESTS.clear()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 别把每个请求都打到测试输出里
        pass

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/").endswith("/models"):
            self._json(200, {"data": [
                {"id": "mock-chat", "created": 1700000000},
                {"id": "mock-reasoner", "created": 1700000000000},
            ]})
            return
        self._json(404, {"error": {"message": "no such path"}})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}
        with LOCK:
            record = {"path": self.path, "headers": dict(self.headers), "body": body,
                      "started": time.time(), "ended": 0.0}
            REQUESTS.append(record)  # 记住**这个对象本身**，别记下标——
                                     # 测试中途 mock.reset() 会清空列表，下标就失效了

        model = body.get("model") or ""
        script = SCRIPT.get(model, {})

        # ── 按剧本先失败：验"能自愈的报错" ──
        reject = script.get("reject_keys") or []
        if "max_tokens" in reject and (body.get("max_tokens") or 0) > int(script.get("max_ok") or 8192):
            self._json(400, {"error": {"message":
                f"Invalid max_tokens value, the valid range of max_tokens is [1, {script.get('max_ok', 8192)}]"}})
            return
        if script.get("status"):
            self._json(int(script["status"]), {"error": {"message": script.get("error") or "boom"}})
            return

        text = script.get("text") or f"（{model} 的回答）"
        reasoning = script.get("reasoning") or ""
        finish = script.get("finish") or "stop"

        # 先把**整条流**拼成字节，再按固定块长发出去。
        # 切点于是会落在 JSON 里中文的 UTF-8 字节中间——真实网络里就是这样。
        blob = bytearray()

        def emit(obj) -> None:
            line = "data: " + (obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)) + "\n\n"
            blob.extend(line.encode("utf-8"))

        if reasoning:
            emit({"choices": [{"delta": {"reasoning_content": reasoning}}]})
        for ch in text or "":
            emit({"choices": [{"delta": {"content": ch}}]})
        emit({"choices": [{"delta": {}, "finish_reason": finish}]})
        emit({"usage": {"prompt_tokens": 123, "completion_tokens": 45}})
        emit("[DONE]")

        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        step = max(1, int(script.get("chunk_bytes") or 7))
        delay = float(script.get("chunk_delay") or 0)
        for i in range(0, len(blob), step):
            self.wfile.write(bytes(blob[i:i + step]))
            self.wfile.flush()
            if delay:
                time.sleep(delay)  # 慢一点，好让并发的两个席位真的重叠在一起
        with LOCK:
            record["ended"] = time.time()
        if script.get("stall_after"):
            time.sleep(float(script["stall_after"]))  # 验"连接开着但不再发数据"


def serve(port: int = 8765) -> ThreadingHTTPServer:
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    serve(port)
    print(f"假上游在 http://127.0.0.1:{port}")
    threading.Event().wait()
