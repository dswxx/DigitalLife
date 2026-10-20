"""使用本机假上游验证 HTTP 接口、并发流与持久化。"""

from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from server.main import API_VERSION, create_app
from server.runtime import Runtime
from tests import mock_upstream as mock
from tests.support import ROOT, fill_history, running_server, seed_data

TOKEN = "http-test-token"


class HttpTests(unittest.TestCase):
    """应用和假上游均走真实本机 socket，覆盖流式并发与重启后的持久化。"""
    def setUp(self):
        # ExitStack 逆序清理：先关客户端和应用，再停上游，最后回收临时数据目录。
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix=".tmp-http-", dir=ROOT / "tests")))
        mock.reset()
        self.upstream = mock.serve(0)
        self.stack.callback(self.upstream.server_close)
        self.stack.callback(self.upstream.shutdown)
        self.lives, self.session = seed_data(self.root, f"http://127.0.0.1:{self.upstream.server_port}/v1")
        self.app = create_app(self.root, token=TOKEN)
        self.url = self.stack.enter_context(running_server(self.app))
        self.client = self.stack.enter_context(httpx.Client(base_url=self.url, headers={"x-digitallife-token": TOKEN}, timeout=15))

    def ask(self, question="测试问题", sid=None):
        response = self.client.post("/api/ask", json={"sessionId": sid or self.session.id, "question": question})
        self.assertEqual(response.status_code, 200)
        return [json.loads(line) for line in response.text.splitlines()]

    def test_token_host_and_secret_masking(self):
        self.assertEqual(httpx.get(self.url + "/api/bootstrap").status_code, 401)
        self.assertEqual(self.client.get("/api/bootstrap", headers={"host": "foreign.example"}).status_code, 403)
        boot = self.client.get("/api/bootstrap").json()
        self.assertEqual(boot["apiVersion"], API_VERSION)
        self.assertNotIn("test-secret", json.dumps(boot))
        self.assertEqual(self.app.state.runtime.messages, {})
        self.assertTrue(all(l._history is None for l in self.app.state.runtime.lives.values()))

    def test_real_stream_parallel_and_restart_records(self):
        # 记录上游实际收发时间；两个请求区间重叠才算发生了网络并发。
        mock.set_script("model-a", text="甲的答案", chunk_delay=0.005)
        mock.set_script("model-b", text="乙的答案", chunk_delay=0.005)
        events = self.ask()
        answers = [e for e in events if e["type"] == "seat_done"]
        self.assertEqual({a["content"] for a in answers}, {"甲的答案", "乙的答案"})
        self.assertEqual(events[-1]["type"], "round_done")
        self.assertEqual(len(mock.REQUESTS), 2)
        starts = [r["started"] for r in mock.REQUESTS]
        ends = [r["ended"] for r in mock.REQUESTS]
        self.assertLess(max(starts), min(ends))
        rt = Runtime(self.root)
        self.assertEqual(len(rt.open_session(self.session.id)), 3)
        for life in self.lives[:2]:
            self.assertEqual(len(rt.lives[life.id].read_history()), 2)
        page = self.client.get(f"/api/sessions/{self.session.id}/messages").json()
        self.assertEqual(len(page["rounds"][0]["answers"]), 2)

    def test_second_request_contains_only_own_history_and_persona(self):
        mock.set_script("model-a", text="甲自己的答案")
        mock.set_script("model-b", text="乙自己的答案")
        self.ask("第一个问题")
        self.ask("第二个问题")
        for model, own, other in [("model-a", "甲自己的答案", "乙自己的答案"), ("model-b", "乙自己的答案", "甲自己的答案")]:
            body = mock.requests_for(model)[1]["body"]
            self.assertEqual([m["role"] for m in body["messages"]], ["system", "user", "assistant", "user"])
            self.assertEqual(body["messages"][0]["content"], "你是测试作者，直接回答问题。")
            self.assertEqual(body["messages"][2]["content"], own)
            self.assertNotIn(other, json.dumps(body, ensure_ascii=False))

    def test_bare_does_not_carry_other_session_history(self):
        bare = self.lives[2]
        first = self.client.post("/api/sessions", json={"seats": [{"lifeId": bare.id}]}).json()
        self.ask("会话一", sid=first["id"])
        second = self.client.post("/api/sessions", json={"seats": [{"lifeId": bare.id}]}).json()
        self.ask("会话二", sid=second["id"])
        self.assertEqual(mock.requests_for("model-a")[-1]["body"]["messages"], [{"role": "user", "content": "会话二"}])

    def test_one_provider_failure_does_not_stop_other(self):
        mock.set_script("model-a", status=401)
        mock.set_script("model-b", text="正常答案")
        answers = [e for e in self.ask() if e["type"] == "seat_done"]
        self.assertEqual(len(answers), 2)
        self.assertTrue(any("API Key" in a["error"] for a in answers))
        self.assertTrue(any(a["content"] == "正常答案" for a in answers))

    def test_busy_session_rejects_edits_and_duplicate_round(self):
        mock.set_script("model-a", text="慢回答", chunk_delay=0.02)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.ask)
            deadline = time.monotonic() + 3
            while not mock.REQUESTS and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(mock.REQUESTS)
            self.assertEqual(self.client.patch(f"/api/sessions/{self.session.id}", json={"title": "不应改动"}).status_code, 409)
            self.assertEqual(self.client.delete(f"/api/sessions/{self.session.id}").status_code, 409)
            self.assertEqual(self.client.delete(f"/api/lives/{self.lives[0].id}").status_code, 409)
            self.assertTrue(any(e["type"] == "error" for e in self.ask("重复问题")))
            future.result(timeout=10)

    def test_crud_templates_lives_sessions_and_models(self):
        tpl = self.client.post("/api/templates", json={"name": "新作者", "system": "人格"}).json()
        life = self.client.post("/api/lives", json={"templateId": tpl["id"], "providerId": "p", "model": "model-a"}).json()
        self.assertIn("id", life)
        revised = self.client.patch(f"/api/templates/{tpl['id']}", json={"system": "修改人格"}).json()
        self.assertEqual(self.app.state.runtime.lives[life["id"]].identity["templateId"], revised["id"])
        self.assertEqual(self.client.delete(f"/api/templates/{revised['id']}").status_code, 400)
        self.assertTrue(self.client.post("/api/providers/p/test").json()["ok"])
        self.assertEqual(len(self.client.post("/api/providers/p/models").json()["models"]), 2)
        self.assertEqual(self.client.delete(f"/api/lives/{life['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/templates/{revised['id']}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/sessions/{self.session.id}").status_code, 200)

    def test_removed_features_have_no_routes(self):
        lid = self.lives[0].id
        for suffix in ("memory", "timeline", "history", f"sessions/{self.session.id}"):
            self.assertEqual(self.client.get(f"/api/lives/{lid}/{suffix}").status_code, 404)
        self.assertIn(self.client.post(f"/api/lives/{lid}/history/compact", json={}).status_code, (404, 405))
        self.assertEqual(self.client.get("/api/settings").status_code, 404)

    def test_auto_compression_then_answer_over_real_http(self):
        runtime = self.app.state.runtime
        runtime.update_settings(advanced={"maxTokens": 256})
        fill_history(runtime, runtime.sessions[self.session.id])
        before = runtime.lives[self.lives[0].id].history_path().read_bytes()
        mock.set_script("model-a", context_chars=1000, text="继续交流的甲答案", summary_text="甲的既往观点与用户意图。")
        mock.set_script("model-b", context_chars=1000, text="继续交流的乙答案", summary_text="乙的既往观点与用户意图。")
        events = self.ask("压缩后的新问题")
        self.assertTrue(any("正在压缩" in e.get("text", "") for e in events))
        self.assertEqual({e["content"] for e in events if e["type"] == "seat_done"}, {"继续交流的甲答案", "继续交流的乙答案"})
        for model in ("model-a", "model-b"):
            requests = mock.requests_for(model)
            self.assertGreater(len(requests), 3)
            self.assertIn("压缩后的新问题", requests[0]["body"]["messages"][-1]["content"])
            self.assertIn("此前对话摘要", requests[-1]["body"]["messages"][0]["content"])
            self.assertEqual(requests[-1]["body"]["messages"][-1]["content"], "压缩后的新问题")
        self.assertTrue(runtime.lives[self.lives[0].id].history_path().read_bytes().startswith(before))

    def test_summary_failure_keeps_history_and_other_model_continues(self):
        runtime = self.app.state.runtime
        runtime.update_settings(advanced={"maxTokens": 256})
        fill_history(runtime, runtime.sessions[self.session.id])
        mock.set_script("model-a", context_chars=1000, summary_text="不完整", summary_finish="length")
        mock.set_script("model-b", text="另一位正常回答")
        answers = [e for e in self.ask() if e["type"] == "seat_done"]
        self.assertTrue(any("压缩未生成完整摘要" in a["error"] for a in answers))
        self.assertTrue(any(a["content"] == "另一位正常回答" for a in answers))
        self.assertFalse((runtime.lives[self.lives[0].id].root / "context.json").exists())
        self.assertGreaterEqual(len(mock.requests_for("model-a")), 3)
        self.assertEqual(len(mock.requests_for("model-b")), 1)


if __name__ == "__main__":
    unittest.main()
