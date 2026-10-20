"""验证上下文、两处记录、按需加载、并发与保存失败行为。"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import chat, compression, context, sessions, settings, store, templates
from server.adapters import AdapterError
from server.life import Life
from server.runtime import Runtime
from tests.support import ROOT, seed_data


class CoreTests(unittest.TestCase):
    """使用临时文件与替代模型流检验业务约束，不调用网络或读取个人数据。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix=".tmp-core-", dir=ROOT / "tests")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        lives, self.session = seed_data(self.root)
        self.rt = Runtime(self.root)
        self.a, self.b, self.bare = [self.rt.lives[l.id] for l in lives]

    def message(self, kind, text, rid="r1", sid=None, life=None):
        """经由真实保存入口构造历史，避免测试绕过两处记录与内存追加逻辑。"""
        life = life or self.a
        entry = {"kind": kind, "text": text, "roundId": rid, "t": 1,
                 "messageId": rid + ("_you" if kind == "you" else "_" + life.id)}
        if kind == "you":
            entry["participants"] = [{"lifeId": l.id, "seatNo": i + 1, "name": l.identity["name"]}
                                     for i, l in enumerate((self.a, self.b))]
        else:
            entry.update(lifeId=life.id, seatNo=1, name=life.identity["name"])
        self.rt.write_message(sid or self.session.id, entry)

    def ask(self, question="现在的问题", sid=None):
        return list(chat.ask(self.root, session_id=sid or self.session.id, question=question,
                             settings=self.rt.settings, transport=None, runtime=self.rt))

    def test_startup_is_lazy_and_reuses_open_session(self):
        # 首次读取后让读盘函数直接失败，从外部行为验证确实复用了运行时记录。
        self.assertEqual(self.rt.messages, {})
        self.assertTrue(all(l._history is None for l in self.rt.lives.values()))
        self.rt.open_session(self.session.id)
        with patch.object(sessions, "read_messages", side_effect=AssertionError("重复读盘")):
            self.assertEqual(self.rt.open_session(self.session.id), [])
        self.a.read_history()
        with patch.object(store, "read_jsonl", side_effect=AssertionError("重复读盘")):
            self.assertEqual(self.a.read_history(), [])

    def test_records_and_personal_history_are_separate(self):
        self.message("you", "问题")
        self.message("self", "甲的回答")
        self.message("self", "乙的回答", life=self.b)
        self.assertEqual(len(self.rt.open_session(self.session.id)), 3)
        self.assertEqual([e["text"] for e in self.a.read_history()], ["问题", "甲的回答"])
        self.assertEqual([e["text"] for e in self.b.read_history()], ["问题", "乙的回答"])
        self.assertEqual(self.bare.read_history(), [])
        self.assertEqual({p.name for p in self.a.root.iterdir()}, {"identity.json", "history.jsonl"})
        restarted = Runtime(self.root)
        self.assertEqual(restarted.open_session(self.session.id), self.rt.open_session(self.session.id))
        self.assertEqual(restarted.lives[self.a.id].read_history(), self.a.read_history())

    def test_context_is_exact_persona_history_question(self):
        self.message("you", "之前的问题")
        self.message("self", "之前的回答")
        built = context.build_context(self.a, session_id=self.session.id, question="当前问题",
                                      persona="人格原文")
        self.assertEqual(built.system, "人格原文")
        self.assertEqual(built.messages, [
            {"role": "user", "content": "之前的问题"},
            {"role": "assistant", "content": "之前的回答"},
            {"role": "user", "content": "当前问题"},
        ])

    def test_cross_session_history_and_bare_isolation(self):
        self.message("you", "其他会话的问题")
        self.message("self", "其他会话的回答")
        digital = context.build_context(self.a, session_id="other", question="新问题")
        bare = context.build_context(self.a, session_id="other", question="新问题",
                                     persona="不该发送", bare=True)
        self.assertEqual(len(digital.messages), 3)
        self.assertIsNone(bare.system)
        self.assertEqual(bare.messages, [{"role": "user", "content": "新问题"}])

    def test_context_sends_all_original_text_without_reasoning_or_disk_changes(self):
        for i in range(6):
            self.message("you", f"{i}：" + "问" * 150, rid=f"r{i}")
            self.message("self", f"{i}：" + "答" * 150, rid=f"r{i}")
        original = self.a.history_path().read_bytes()
        self.a._history[-1]["reasoning"] = "不能发给模型的历史思考"
        built = context.build_context(self.a, session_id=self.session.id, question="当前问题")
        self.assertIn("5：", str(built.messages))
        self.assertIn("0：", str(built.messages))
        self.assertNotIn("不能发给模型的历史思考", str(built.messages))
        self.assertEqual(original, self.a.history_path().read_bytes())
        long = context.build_context(self.a, session_id=self.session.id, question="问" * 300000)
        self.assertEqual(long.messages[-1]["content"], "问" * 300000)

    def test_interleaved_rounds_are_paired(self):
        self.message("you", "甲问", rid="r1")
        self.message("you", "乙问", rid="r2")
        self.a.active_rounds.add("r2")
        self.message("self", "甲答", rid="r1")
        built = context.build_context(self.a, session_id=self.session.id, question="现在")
        self.assertEqual([m["content"] for m in built.messages], ["甲问", "甲答", "现在"])

    def test_pagination_and_historical_participants(self):
        for i in range(4):
            self.message("you", str(i), rid=f"r{i}")
            self.message("self", f"答{i}", rid=f"r{i}")
        self.session.seats = []
        self.rt.save_session(self.session)
        self.rt.delete_life(self.a.id)
        rows = self.rt.open_session(self.session.id)
        latest = sessions.round_views(rows, limit=2)
        older = sessions.round_views(rows, limit=2, before=latest["before"])
        self.assertEqual([r["question"] for r in latest["rounds"]], ["2", "3"])
        self.assertEqual([r["question"] for r in older["rounds"]], ["0", "1"])
        self.assertEqual(latest["rounds"][0]["answers"][0]["content"], "答2")
        self.assertIn("未收到完整回答", latest["rounds"][0]["answers"][1]["error"])

    def test_delete_session_preserves_personal_history(self):
        self.message("you", "保留在个人历史")
        self.rt.delete_session(self.session.id)
        self.assertEqual(self.a.read_history()[0]["text"], "保留在个人历史")
        self.assertFalse(sessions.session_path(self.root, self.session.id).exists())

    def test_corrupt_and_missing_history_are_visible(self):
        self.a.history_path().write_bytes(b'{"incomplete":')
        with self.assertRaises(OSError):
            self.a.read_history()
        with self.assertRaises(OSError):
            self.message("you", "不能拼接到坏行")
        self.b.history_path().unlink()
        with self.assertRaises(OSError):
            self.b.read_history()

    def test_stream_only_calls_each_model_once(self):
        calls, guard = [], threading.Lock()
        def stream(provider, **kw):
            with guard:
                calls.append(kw)
            yield {"type": "text", "text": kw["model"] + "回答"}
            yield {"type": "finish", "reason": "stop"}
        with patch.object(compression, "stream_chat", side_effect=stream):
            events = self.ask()
        self.assertEqual(len(calls), 2)
        self.assertEqual(len([e for e in events if e["type"] == "seat_done"]), 2)
        self.assertEqual(events[-1]["type"], "round_done")
        self.assertTrue(all(c["messages"] == [{"role": "user", "content": "现在的问题"}] for c in calls))
        self.assertEqual(len(self.rt.open_session(self.session.id)), 3)

    def test_reasoning_limit_does_not_start_another_call(self):
        def stream(*a, **kw):
            yield {"type": "reasoning", "text": "思考"}
            yield {"type": "finish", "reason": "length"}
        with patch.object(compression, "stream_chat", side_effect=stream) as called:
            events = self.ask()
        self.assertEqual(called.call_count, 2)
        self.assertTrue(all(e["finishReason"] == "length" for e in events if e["type"] == "seat_done"))

    def test_save_failure_is_not_reported_as_completed(self):
        # 模拟会话已写、个人历史写入失败，验证不重复追加，也不报告虚假的完成。
        append = Life.append_history
        def save(life, entry):
            if entry["kind"] == "self":
                raise OSError("磁盘写入失败")
            return append(life, entry)
        with patch.object(compression, "stream_chat", side_effect=lambda *a, **kw: iter([{"type": "text", "text": "回答"}])), \
             patch.object(Life, "append_history", save):
            events = self.ask()
        self.assertFalse(any(e["type"] in ("seat_done", "round_done") for e in events))
        self.assertTrue(any(e.get("saveFailed") for e in events))
        self.assertEqual(sum(m["kind"] == "self" for m in self.rt.open_session(self.session.id)), 2)
        before = self.a.history_path().read_bytes()
        Runtime(self.root).open_session(self.session.id)
        self.assertEqual(self.a.history_path().read_bytes(), before)

    def test_disconnect_keeps_background_round_locked_until_saved(self):
        # 用事件明确控制“已开始、尚未结束”的时间段，避免依赖机器快慢猜测并发。
        entered, release = threading.Event(), threading.Event()
        def stream(*a, **kw):
            entered.set()
            release.wait(5)
            yield {"type": "text", "text": "完整回答"}
        with patch.object(compression, "stream_chat", side_effect=stream):
            gen = chat.ask(self.root, session_id=self.session.id, question="问题",
                           settings=self.rt.settings, transport=None, runtime=self.rt)
            self.assertEqual(next(gen)["type"], "round_start")
            self.assertTrue(entered.wait(2))
            gen.close()
            with self.assertRaises(AdapterError):
                self.ask()
            with self.assertRaises(AdapterError):
                self.rt.delete_session(self.session.id)
            release.set()
            self.assertTrue(self.rt.round_lock(self.session.id).acquire(timeout=5))
            self.rt.round_lock(self.session.id).release()
        self.assertEqual(len([m for m in self.rt.open_session(self.session.id) if m["kind"] == "self"]), 2)

    def test_settings_merge_masks_and_concurrency(self):
        masked = settings.public_view(self.rt.settings)["providers"]
        self.assertNotEqual(masked[0]["apiKey"], "test-secret")
        settings.update(self.root, providers=masked)
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda i: settings.update(self.root, model_info={str(i): {"maxOutput": 1000}}), range(8)))
        saved = settings.load(self.root)
        self.assertEqual(saved["providers"][0]["apiKey"], "test-secret")
        self.assertEqual(len(saved["modelInfo"]), 8)

    def test_legacy_context_window_no_longer_limits_requests_or_output(self):
        self.rt.update_settings(advanced={"contextWindow": 1, "maxTokens": 8192},
                                model_info={"model-a": {"contextWindow": 1}})
        self.assertEqual(settings.resolve_output_cap(self.rt.settings, "model-a"), 8192)
        with patch.object(compression, "stream_chat", side_effect=lambda *a, **kw: iter([{"type": "text", "text": "正常回答"}])) as call:
            self.ask(question="问" * 200000)
        self.assertEqual(call.call_count, 2)
        self.assertTrue(all(c.kwargs["messages"][-1]["content"] == "问" * 200000 for c in call.call_args_list))

    def test_bad_json_is_reported_without_rewriting_or_moving_it(self):
        path = self.root / "settings.json"
        original = b'{"unfinished":'
        path.write_bytes(original)
        with self.assertRaises(json.JSONDecodeError):
            settings.load(self.root)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(list(self.root.glob("*.corrupt-*")))

    def test_template_save_failure_preserves_old_persona(self):
        original = self.rt.templates[self.a.identity["templateId"]]
        with patch.object(templates, "save", side_effect=OSError("写入失败")):
            with self.assertRaises(OSError):
                templates.update(self.root, original, {"system": "新内容"})
        self.assertEqual(store.read_json(templates.template_path(self.root, original.id), {})["system"], original.system)

    def test_invalid_seats_and_paths(self):
        with self.assertRaises(ValueError):
            sessions.normalize_seats([{"lifeId": "same"}, {"lifeId": "same"}])
        with self.assertRaises(ValueError):
            sessions.session_path(self.root, "../outside")


if __name__ == "__main__":
    unittest.main()
