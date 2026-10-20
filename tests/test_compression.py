"""验证真实超限触发、二分摘要、有限重试、原文保留与并发隔离。"""

from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import compression, store
from server.adapters import AdapterError
from server.runtime import Runtime
from tests.support import ROOT, seed_data


class CompressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".tmp-compression-", dir=ROOT / "tests")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        lives, self.session = seed_data(self.root)
        self.life = Runtime(self.root).lives[lives[0].id]
        self.requests = []

    def history(self, count=6, size=150, sid=None, prefix="r"):
        for i in range(count):
            rid = f"{prefix}{i}"
            for kind, text in (("you", f"{prefix}问题{i}：" + "问" * size), ("self", f"{prefix}回答{i}：" + "答" * size)):
                self.life.append_history({"kind": kind, "text": text, "reasoning": "历史思考不能发送",
                                          "roundId": rid, "messageId": f"{rid}_{kind}",
                                          "sessionId": sid or self.session.id, "t": i})

    def answer(self, **changes):
        args = dict(session_id=self.session.id, round_id="current", question="当前问题",
                    persona="人格原文", bare=False, output_cap=256,
                    provider={"id": "p"}, model="model-a", transport=None)
        args.update(changes)
        return list(compression.stream_answer(self.life, **args))

    def upstream(self, provider, **kw):
        self.requests.append(kw)
        is_summary = kw["system"] == compression.SUMMARY_SYSTEM
        if not is_summary and sum(len(m["content"]) for m in kw["messages"]) > 1000:
            raise AdapterError(400, "context_length_exceeded", kind="context_limit")
        yield {"type": "text", "text": "既往事实与观点。" if is_summary else "正式回答"}
        yield {"type": "finish", "reason": "stop"}

    def test_large_context_goes_directly_to_provider_without_summary(self):
        self.history(size=50000)
        def accept(*a, **kw):
            self.requests.append(kw)
            yield {"type": "text", "text": "接受完整历史"}
        with patch.object(compression, "stream_chat", side_effect=accept):
            events = self.answer()
        self.assertEqual(len(self.requests), 1)
        self.assertIn("r问题0", str(self.requests[0]["messages"]))
        self.assertNotIn("历史思考不能发送", str(self.requests))
        self.assertFalse(any(e["type"] == "notice" for e in events))
        self.assertFalse((self.life.root / "context.json").exists())

    def test_overflow_then_summary_then_retry_keeps_recent_and_raw(self):
        self.history()
        original = self.life.history_path().read_bytes()
        with patch.object(compression, "stream_chat", side_effect=self.upstream):
            events = self.answer()
        self.assertEqual(len(self.requests), 3)
        self.assertIn("r问题0", str(self.requests[0]["messages"]))
        self.assertIn("r问题3", str(self.requests[1]["messages"]))
        self.assertNotIn("r问题4", str(self.requests[1]["messages"]))
        self.assertNotIn("r问题0", str(self.requests[2]["messages"]))
        self.assertIn("r问题4", str(self.requests[2]["messages"]))
        self.assertIn("r问题5", str(self.requests[2]["messages"]))
        self.assertIn("此前对话摘要", self.requests[2]["system"])
        self.assertNotIn("历史思考不能发送", str(self.requests))
        self.assertEqual(self.life.history_path().read_bytes(), original)
        self.assertTrue(any("压缩完成" in e.get("text", "") for e in events))
        state = store.read_json(self.life.root / "context.json", {})["life"]
        self.assertEqual(state["throughRoundId"], "r3")

    def test_summary_reused_after_restart(self):
        self.history()
        with patch.object(compression, "stream_chat", side_effect=self.upstream):
            self.answer()
        self.life = Runtime(self.root).lives[self.life.id]
        self.requests.clear()
        with patch.object(compression, "stream_chat", side_effect=self.upstream):
            self.answer()
        self.assertEqual(len(self.requests), 1)
        self.assertIn("既往事实与观点", self.requests[0]["system"])

    def test_next_compression_integrates_prior_summary(self):
        self.history()
        with patch.object(compression, "stream_chat", side_effect=self.upstream):
            self.answer()
            self.requests.clear()
            self.history(count=4, prefix="new")
            self.answer()
        material = self.requests[1]["messages"][0]["content"]
        self.assertIn("既往事实与观点", material)
        self.assertIn("r问题4", material)
        self.assertNotIn("r问题0", material)

    def test_bare_summary_is_isolated_from_other_sessions(self):
        self.history(sid="one")
        with patch.object(compression, "stream_chat", side_effect=self.upstream):
            self.answer(session_id="one", bare=True)
            self.answer(session_id="two", bare=True)
        self.assertIsNone(self.requests[-1]["system"])
        self.assertEqual(self.requests[-1]["messages"], [{"role": "user", "content": "当前问题"}])
        self.assertNotIn("人格原文", self.requests[-2]["system"])
        self.assertEqual(set(store.read_json(self.life.root / "context.json", {})), {"session:one"})

    def test_summary_overflow_splits_and_covers_all_text(self):
        self.history(count=6, size=400)
        successful_pieces = []
        def limited(*a, **kw):
            if kw["system"] == compression.SUMMARY_SYSTEM:
                material = kw["messages"][0]["content"].split("【新增对话资料】\n", 1)[1]
                if len(material) > 600:
                    raise AdapterError(400, "prompt too long", kind="context_limit")
                successful_pieces.append(material)
                yield {"type": "text", "text": "摘要。"}
                yield {"type": "finish", "reason": "stop"}
            else:
                yield from self.upstream(*a, **kw)
        with patch.object(compression, "stream_chat", side_effect=limited):
            events = self.answer()
        joined = "".join(successful_pieces)
        for i in range(6):
            self.assertIn(f"r问题{i}：" + "问" * 400, joined)
            self.assertIn(f"r回答{i}：" + "答" * 400, joined)
        self.assertTrue(any("拆分" in e.get("text", "") for e in events))
        self.assertEqual(store.read_json(self.life.root / "context.json", {})["life"]["throughRoundId"], "r5")

    def test_non_context_errors_never_compress_or_retry(self):
        self.history()
        for status, message in ((401, "bad key"), (429, "quota"), (504, "timeout"), (400, "invalid max_tokens"), (413, "body too large")):
            with patch.object(compression, "stream_chat", side_effect=AdapterError(status, message)) as call:
                with self.assertRaisesRegex(AdapterError, message):
                    self.answer()
            self.assertEqual(call.call_count, 1)
        self.assertFalse((self.life.root / "context.json").exists())

    def test_partial_text_or_reasoning_never_restarts(self):
        self.history()
        for kind in ("text", "reasoning"):
            def partial(*a, **kw):
                yield {"type": kind, "text": "已经发给页面的内容"}
                raise AdapterError(502, "context", kind="context_limit")
            with patch.object(compression, "stream_chat", side_effect=partial) as call:
                with self.assertRaises(AdapterError):
                    self.answer()
            self.assertEqual(call.call_count, 1)
        self.assertFalse((self.life.root / "context.json").exists())

    def test_no_history_reports_uncompressible_question(self):
        with patch.object(compression, "stream_chat", side_effect=AdapterError(400, "too long", kind="context_limit")) as call:
            with self.assertRaisesRegex(AdapterError, "没有可压缩"):
                self.answer(question="问" * 300000)
        self.assertEqual(call.call_count, 1)

    def test_retries_are_bounded_even_when_provider_always_rejects(self):
        self.history(count=8)
        normal_calls = []
        def rejecting(*a, **kw):
            if kw["system"] != compression.SUMMARY_SYSTEM:
                normal_calls.append(kw)
                raise AdapterError(400, "too long", kind="context_limit")
            yield {"type": "text", "text": "摘" * (8 - len(normal_calls))}
            yield {"type": "finish", "reason": "stop"}
        with patch.object(compression, "stream_chat", side_effect=rejecting):
            with self.assertRaisesRegex(AdapterError, "压缩重试后"):
                self.answer()
        self.assertEqual(len(normal_calls), compression.MAX_COMPRESSION_RETRIES + 1)

    def test_summary_requests_are_bounded_when_all_batches_overflow(self):
        self.history(size=100000)
        with patch.object(compression, "MAX_SUMMARY_CALLS", 3), \
             patch.object(compression, "stream_chat", side_effect=AdapterError(400, "too long", kind="context_limit")) as call:
            with self.assertRaisesRegex(AdapterError, "分批调用次数上限"):
                self.answer()
        self.assertEqual(call.call_count, 4)
        self.assertFalse((self.life.root / "context.json").exists())

    def test_failed_summary_batch_keeps_previous_file_and_all_raw(self):
        self.history()
        with patch.object(compression, "stream_chat", side_effect=self.upstream):
            self.answer()
        path = self.life.root / "context.json"
        previous = path.read_bytes()
        self.history(count=5, size=400, prefix="new")
        original = self.life.history_path().read_bytes()
        completed = []
        def bad_batch(*a, **kw):
            if kw["system"] != compression.SUMMARY_SYSTEM:
                yield from self.upstream(*a, **kw)
                return
            material = kw["messages"][0]["content"].split("【新增对话资料】\n", 1)[1]
            if len(material) > 600:
                raise AdapterError(400, "too long", kind="context_limit")
            completed.append(material)
            yield {"type": "text", "text": "临时摘要"}
            yield {"type": "finish", "reason": "stop" if len(completed) == 1 else "length"}
        with patch.object(compression, "stream_chat", side_effect=bad_batch):
            with self.assertRaisesRegex(AdapterError, "未生成完整摘要"):
                self.answer()
        self.assertGreater(len(completed), 1)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(self.life.history_path().read_bytes(), original)

    def test_empty_missing_finish_or_longer_summary_is_rejected(self):
        self.history()
        for chunks in ([{"type": "text", "text": "没有收尾"}],
                       [{"type": "finish", "reason": "stop"}],
                       [{"type": "text", "text": "摘" * 10000}, {"type": "finish", "reason": "stop"}]):
            def broken(*a, **kw):
                if kw["system"] == compression.SUMMARY_SYSTEM:
                    yield from chunks
                else:
                    yield from self.upstream(*a, **kw)
            with patch.object(compression, "stream_chat", side_effect=broken):
                with self.assertRaises(AdapterError):
                    self.answer()
            self.assertFalse((self.life.root / "context.json").exists())

    def test_summary_storage_failure_surfaces(self):
        self.history()
        original = self.life.history_path().read_bytes()
        with patch.object(compression, "stream_chat", side_effect=self.upstream), \
             patch.object(store, "write_json", side_effect=OSError("保存失败")):
            with self.assertRaises(OSError):
                self.answer()
        self.assertFalse((self.life.root / "context.json").exists())
        self.assertEqual(self.life.history_path().read_bytes(), original)

    def test_active_older_round_blocks_watermark(self):
        self.history()
        self.life.active_rounds.add("r0")
        with patch.object(compression, "stream_chat", side_effect=self.upstream):
            with self.assertRaisesRegex(AdapterError, "其他会话"):
                self.answer()
        self.assertEqual(len(self.requests), 1)
        self.assertFalse((self.life.root / "context.json").exists())

    def test_current_question_not_summarized_or_duplicated(self):
        self.history()
        self.life.append_history({"kind": "you", "text": "当前问题", "roundId": "current",
                                  "messageId": "current_you", "sessionId": self.session.id, "t": 10})
        self.life.active_rounds.add("current")
        with patch.object(compression, "stream_chat", side_effect=self.upstream):
            self.answer()
        self.assertNotIn("当前问题", str(self.requests[1]["messages"]))
        self.assertEqual(sum(m["content"].count("当前问题") for m in self.requests[-1]["messages"]), 1)

    def test_concurrent_overflows_reuse_committed_summary(self):
        self.history()
        barrier = threading.Barrier(2)
        results, errors = [], []
        def stream(*a, **kw):
            if kw["system"] != compression.SUMMARY_SYSTEM and len(kw["messages"]) > 10:
                barrier.wait(timeout=5)
            yield from self.upstream(*a, **kw)
        def work():
            try:
                results.append(self.answer())
            except Exception as error:
                errors.append(error)
        with patch.object(compression, "stream_chat", side_effect=stream):
            threads = [threading.Thread(target=work) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(sum(r["system"] == compression.SUMMARY_SYSTEM for r in self.requests), 1)

    def test_unfinished_question_before_completed_rounds_cannot_be_covered(self):
        self.life.append_history({"kind": "you", "text": "还在其他会话回答的问题", "roundId": "pending",
                                  "messageId": "pending_you", "sessionId": self.session.id, "t": 0})
        self.life.active_rounds.add("pending")
        self.history()
        with patch.object(compression, "stream_chat", side_effect=self.upstream):
            with self.assertRaisesRegex(AdapterError, "其他会话"):
                self.answer()
        self.assertFalse((self.life.root / "context.json").exists())

    def test_existing_summary_can_be_shortened_without_moving_watermark(self):
        self.history()
        store.write_json(self.life.root / "context.json", {
            "life": {"summary": "旧摘要" * 1000, "throughRoundId": "r5", "model": "old-model", "updatedAt": 1}})
        def limited(*a, **kw):
            if kw["system"] != compression.SUMMARY_SYSTEM and len(kw["system"]) > 1000:
                raise AdapterError(400, "too long", kind="context_limit")
            yield {"type": "text", "text": "更短的摘要。" if kw["system"] == compression.SUMMARY_SYSTEM else "正式回答"}
            yield {"type": "finish", "reason": "stop"}
        with patch.object(compression, "stream_chat", side_effect=limited):
            self.answer()
        state = store.read_json(self.life.root / "context.json", {})["life"]
        self.assertEqual(state["throughRoundId"], "r5")
        self.assertEqual(state["summary"], "更短的摘要。")

    def test_learned_output_cap_is_reused_during_compression_retry(self):
        self.history()
        def limited(*a, **kw):
            if kw["system"] != compression.SUMMARY_SYSTEM and len(kw["messages"]) > 10:
                yield {"type": "limit", "max": 128, "text": "按平台降低输出上限"}
            yield from self.upstream(*a, **kw)
        with patch.object(compression, "stream_chat", side_effect=limited):
            self.answer()
        self.assertEqual(self.requests[0]["max_tokens"], 256)
        self.assertTrue(all(r["max_tokens"] == 128 for r in self.requests[1:]))


if __name__ == "__main__":
    unittest.main()
