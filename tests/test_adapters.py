"""验证两种协议、UTF-8 流、错误、额度重试及超时。"""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.adapters import AdapterError, list_models, stream_chat
from server.adapters.core import IDLE_MS, Opened, normalize_base, parse_max_tokens_limit
from server.adapters.sse import iter_sse_data


class Transport:
    def __init__(self, steps=None):
        self.steps = list(steps or [{}])
        self.requests = []

    def open_stream(self, **request):
        self.requests.append(request)
        step = self.steps.pop(0)
        if "raise" in step:
            raise step["raise"]
        blob = "".join("data: " + (e if isinstance(e, str) else json.dumps(e, ensure_ascii=False))
                       + "\n\n" for e in step.get("events", ["[DONE]"])).encode("utf-8")
        return Opened(step.get("status", 200), (blob[i:i + 2] for i in range(0, len(blob), 2)), step.get("error", ""))

    def get_json(self, **request):
        self.requests.append(request)
        step = self.steps.pop(0)
        return step.get("status", 200), json.dumps(step.get("data", {}))


class AdapterTests(unittest.TestCase):
    provider = {"kind": "openai", "baseUrl": "https://example.com", "apiKey": "test"}

    def call(self, transport, provider=None, **kw):
        return list(stream_chat(provider or self.provider, model="m", system="人格",
                                messages=[{"role": "user", "content": "问题"}], transport=transport, **kw))

    def test_openai_request_and_stream(self):
        tr = Transport([{"events": [
            {"choices": [{"delta": {"reasoning_content": "思考"}}]},
            {"choices": [{"delta": {"content": "你好"}}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
            {"usage": {"prompt_tokens": 12, "completion_tokens": 5}}, "[DONE]",
        ]}])
        events = self.call(tr)
        req = tr.requests[0]
        self.assertEqual(req["url"], "https://example.com/v1/chat/completions")
        self.assertEqual(req["headers"]["authorization"], "Bearer test")
        self.assertEqual(req["body"]["messages"], [{"role": "system", "content": "人格"}, {"role": "user", "content": "问题"}])
        self.assertNotIn("temperature", req["body"])
        self.assertIn({"type": "text", "text": "你好"}, events)
        self.assertIn({"type": "reasoning", "text": "思考"}, events)
        self.assertIn({"type": "finish", "reason": "length"}, events)
        self.assertIn({"type": "usage", "usage": {"input": 12, "output": 5}}, events)

    def test_anthropic_request_and_stream(self):
        tr = Transport([{"events": [
            {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "回答"}},
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "思考"}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        ]}])
        events = self.call(tr, {**self.provider, "kind": "anthropic"})
        req = tr.requests[0]
        self.assertEqual(req["url"], "https://example.com/v1/messages")
        self.assertEqual(req["headers"]["x-api-key"], "test")
        self.assertEqual(req["body"]["system"], "人格")
        self.assertEqual(req["body"]["messages"], [{"role": "user", "content": "问题"}])
        self.assertIn({"type": "text", "text": "回答"}, events)
        self.assertIn({"type": "finish", "reason": "length"}, events)

    def test_bare_omits_system_for_both_protocols(self):
        for kind in ("openai", "anthropic"):
            tr = Transport()
            list(stream_chat({**self.provider, "kind": kind}, model="m", system=None,
                             messages=[{"role": "user", "content": "问题"}], transport=tr))
            self.assertNotIn("system", tr.requests[0]["body"])
            self.assertFalse(any(m["role"] == "system" for m in tr.requests[0]["body"]["messages"]))

    def test_sse_unicode_split_at_every_byte(self):
        raw = 'data: {"text":"中文🙂"}\r\n\r\ndata: [DONE]'.encode("utf-8")
        self.assertEqual(list(iter_sse_data([bytes([b]) for b in raw])), ['{"text":"中文🙂"}', '[DONE]'])

    def test_output_limit_retry(self):
        tr = Transport([{"status": 400, "error": "valid range of max_tokens is [1, 1024]"}, {}])
        events = self.call(tr, max_tokens=4096)
        self.assertEqual(tr.requests[1]["body"]["max_tokens"], 1024)
        self.assertTrue(any(e["type"] == "limit" and e["max"] == 1024 for e in events))

    def test_errors_and_timeouts(self):
        for status in (401, 402, 404, 429, 500):
            with self.subTest(status=status), self.assertRaises(AdapterError):
                self.call(Transport([{"status": status, "error": "upstream error"}]))
        with self.assertRaisesRegex(AdapterError, "超时"):
            self.call(Transport([{"raise": TimeoutError("timed out")}]))
        tr = Transport()
        self.call(tr, timeout=37)
        self.assertEqual(tr.requests[0]["timeout"], 37)
        tr = Transport()
        self.call(tr)
        self.assertEqual(tr.requests[0]["timeout"], IDLE_MS / 1000)

    def test_models_and_url_validation(self):
        tr = Transport([{"data": {"data": [{"id": "one", "created": 1700000000}, "two"]}}])
        models = list_models(self.provider, transport=tr)
        self.assertEqual([m["id"] for m in models], ["one", "two"])
        self.assertEqual(models[0]["created"], 1700000000000)
        for bad in ("", "hello", "file:///private"):
            with self.assertRaises(AdapterError):
                normalize_base(bad)
        self.assertEqual(parse_max_tokens_limit("max_tokens: 9000 > 8192"), 8192)


if __name__ == "__main__":
    unittest.main()
