"""并发调用模型、推送流式回答并保存原文。"""

from __future__ import annotations

import queue
import threading
import time

from . import context, settings as settings_mod, store, templates
from .adapters import AdapterError, stream_chat
from .life import new_id, now_ms
from .runtime import Runtime
from .text import estimate_tokens

def ask(data_dir, *, session_id: str, question: str, settings: dict, transport,
        runtime: Runtime | None = None):
    """并发生成回答，保存完成后推送完成事件。"""
    runtime = runtime or Runtime(data_dir)
    question = (question or "").strip()
    if not question:
        raise AdapterError(400, "先写点什么再问")
    with runtime._guard:
        session = runtime.sessions.get(session_id)
        if session is None:
            raise AdapterError(404, "这次对话已经删除")
        seats = session.ordered()
        if not seats:
            raise AdapterError(400, "还没选要问的人")
        lock = runtime.round_lock(session_id)
        if not lock.acquire(blocking=False):
            raise AdapterError(409, "这一轮仍在后台回答，请等它结束")
    round_id, at = new_id("r"), now_ms()
    lives = {}
    try:
        runtime.open_session(session_id)
        lives = {s["seatNo"]: runtime.lives[s["lifeId"]] for s in seats if s["lifeId"] in runtime.lives}
        if len(lives) != len(seats):
            raise AdapterError(400, "有提问对象已被删除，请先移除再提问")
        for life in lives.values():
            life.active_rounds.add(round_id)
        participants = []
        for s in seats:
            life = lives.get(s["seatNo"])
            identity = life.identity if life else {}
            participants.append({**s, "name": identity.get("name") or s["lifeId"],
                                 "emoji": identity.get("emoji") or "🙂",
                                 "providerId": s.get("providerId") or identity.get("providerId") or "",
                                 "model": s.get("model") or identity.get("model") or ""})
        runtime.write_message(session_id, {
            "kind": "you", "text": question, "t": at, "roundId": round_id,
            "messageId": round_id + "_you", "participants": participants,
        })
    except BaseException:
        for life in lives.values():
            life.active_rounds.discard(round_id)
        lock.release()
        raise
    events = queue.Queue()

    def work():
        try:
            saved = True
            for event in _run_parallel(session, participants, lives, question,
                                       settings, transport, runtime, round_id):
                saved = saved and not event.get("saveFailed")
                events.put(event)
            if saved:
                events.put({"type": "round_done", "roundId": round_id, "at": at})
        except Exception as e:
            events.put({"type": "error", "message": f"本轮未完整保存：{e}"})
        finally:
            for life in lives.values():
                life.active_rounds.discard(round_id)
            lock.release()
            events.put(None)

    # 协调线程持有本轮直到写入结束，浏览器断开不会释放锁或丢掉已经生成的回答。
    threading.Thread(target=work, daemon=True, name="digitallife-round").start()
    yield {"type": "round_start", "roundId": round_id, "at": at}
    while (event := events.get()) is not None:
        yield event


def _run_parallel(session, seats, lives, question, settings, transport, runtime, round_id):
    """用队列汇合各模型的并发事件，单个模型失败不影响其他回答。"""
    q: "queue.Queue[dict]" = queue.Queue()
    threads: list[threading.Thread] = []
    for seat in seats:
        life = lives.get(seat["seatNo"])
        def work(seat=seat, life=life):
            try:
                if life is None:
                    raise AdapterError(404, f"选中的那位（{seat['lifeId']}）已经不在了")
                for evt in _run_seat(session, seat, life, question, settings,
                                     transport, runtime, round_id):
                    q.put(evt)
            except OSError as e:
                q.put({"type": "error", "seatNo": seat["seatNo"], "saveFailed": True,
                       "message": f"保存失败，请检查磁盘与备份：{e}"})
            except Exception as e:
                failure = f"{type(e).__name__}：{e}"
                answer = {**seat, "content": "", "error": failure}
                try:
                    _record_answer(runtime, session.id, round_id, answer)
                    q.put({"type": "seat_done", **answer})
                except OSError as saved:
                    q.put({"type": "error", "seatNo": seat["seatNo"], "saveFailed": True,
                           "message": f"{failure}；保存失败：{saved}"})
            finally:
                q.put({"type": "_seat_end"})

        t = threading.Thread(target=work, daemon=True)
        t.start()
        threads.append(t)

    done = 0
    while done < len(threads):
        evt = q.get()
        if evt.get("type") == "_seat_end":
            done += 1
            continue
        yield evt


def _run_seat(session, seat, life, question, settings: dict,
              transport, runtime, round_id):
    """生成一位的回答，保存成功后发出完成事件。问题已由主入口先行保存。"""
    seat_no = seat["seatNo"]
    name = seat["name"]
    yield {"type": "seat_start", "seatNo": seat_no, "lifeId": life.id, "name": name}

    provider = settings_mod.provider(settings, seat.get("providerId") or life.identity.get("providerId"))
    if provider is None:
        raise AdapterError(400, f"{name} 绑定的平台不在了，请重新配置平台")
    model = seat.get("model") or life.identity.get("model") or ""
    tpl = runtime.templates.get(life.identity.get("templateId")) or templates.Template(
        id="", name=name, system="",
    )
    window = settings_mod.resolve_window(settings, model)
    cap = settings_mod.resolve_output_cap(settings, model, window)

    # 当前问题已保存；请求快照中排除它，再在末尾添加一次。
    with store.lock_for(life.id):
        history = [e for e in life.read_history() if e.get("roundId") != round_id]
        built = context.build_context(
            life, session_id=session.id, question=question, window=window,
            output_reserve=cap, persona=tpl.system, bare=tpl.bare, history=history,
        )
    notices: list[str] = []

    started = time.time()
    content: list[str] = []
    reasoning: list[str] = []
    usage: dict = {}
    finish = ""
    failure = ""

    def consume(chunks):
        """把一条流读完，产出事件。返回时 content/reasoning/usage/finish 都已更新。"""
        nonlocal finish
        for chunk in chunks:
            kind = chunk["type"]
            if kind == "text":
                content.append(chunk["text"])
            elif kind == "reasoning":
                reasoning.append(chunk["text"])
            elif kind == "usage":
                usage.update(chunk["usage"])
            elif kind == "finish":
                finish = chunk["reason"]
            elif kind == "notice":
                for text in [chunk["text"]]:
                    notices.append(text)
                    yield {"type": "notice", "seatNo": seat_no, "text": text}
                continue
            elif kind == "limit":
                for text in [chunk["text"]]:
                    notices.append(text)
                    yield {**chunk, "seatNo": seat_no, "text": text}
                    # 平台说了它的上限 → 记下来，下次不再白挨一次 400
                    if chunk.get("max") and model:
                        runtime.update_settings(model_info={model: {"maxOutput": int(chunk["max"])}})
                continue
            if kind in ("text", "reasoning"):
                yield {**chunk, "seatNo": seat_no}

    try:
        yield from consume(stream_chat(
            provider, model=model, system=built.system, messages=built.messages,
            max_tokens=cap, transport=transport,
        ))
    except AdapterError as e:
        failure = str(e)
    except Exception as e:  # noqa: BLE001
        failure = f"调用失败：{type(e).__name__}：{e}"

    text = "".join(content)
    think = "".join(reasoning)

    if failure and not text:
        yield {"type": "error", "seatNo": seat_no, "message": failure}
    elif finish == "length" and text.strip():
        # 正文有，但答到一半被额度截断了：这个必须说，否则用户以为他说完了
        note = (f"正文被 {cap} token 的输出额度截断了，答到一半就没了"
                f"——可在设置文件中提高 maxTokens 或选择其他模型")
        if note not in notices:
            notices.append(note)
            yield {"type": "notice", "seatNo": seat_no, "text": note}

    answer = {
        "seatNo": seat_no,
        "lifeId": life.id,
        "name": name,
        "emoji": seat.get("emoji") or "🙂",
        "model": model,
        "providerId": provider.get("id") or "",
        "content": text,
        "reasoning": think,
        "usage": usage,
        "tokens": int(usage.get("output") or estimate_tokens(text)),
        "finishReason": finish,
        "ms": int((time.time() - started) * 1000),
        "error": failure,
        "notices": notices,
    }
    # **一说完就落盘**：用户中途关页面，已经答出来的这些也必须留下
    _record_answer(runtime, session.id, round_id, answer)
    yield {"type": "seat_done", "seatNo": seat_no, **answer}


def _record_answer(runtime, session_id, round_id, answer):
    runtime.write_message(session_id, {
        **{k: v for k, v in answer.items() if k != "content"},
        "kind": "self", "text": answer.get("content", ""), "t": now_ms(),
        "roundId": round_id, "messageId": f"{round_id}_{answer['lifeId']}",
    })
