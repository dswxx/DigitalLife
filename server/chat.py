"""聊天主流程：先存问题，再并发调用各模型，逐人保存回答。

阅读顺序：ask → _run_parallel → _run_seat → _record_answer。
线程只负责网络等待与事件汇总；记录写入由 Runtime 持锁完成。
"""

from __future__ import annotations

import queue
import threading
import time

from . import compression, settings as settings_mod, templates
from .adapters import AdapterError
from .life import new_id, now_ms
from .runtime import Runtime
from .text import estimate_tokens

def ask(data_dir, *, session_id: str, question: str, settings: dict, transport,
        runtime: Runtime | None = None):
    """【核心】管理一轮问答的生命周期，并向 HTTP 层产出事件。

    HTTP 入口传入应用共享的 runtime；独立调用时才创建临时 Runtime。
    本生成器只转发事件，后台协调线程负责将调用和保存执行到结束。
    """
    runtime = runtime or Runtime(data_dir)
    question = (question or "").strip()
    if not question:
        raise AdapterError(400, "先写点什么再问")
    # 在同一临界区读取参与者并占用本轮，避免删除会话与开始提问同时发生。
    # 会话锁跨整轮持有，拒绝重复提问；不同会话有各自的锁。
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
    # roundId 关联一个问题和多人的回答；messageId 标识其中某条消息。
    # 并发完成顺序不确定，不能用时间戳或相邻行猜测归属。
    round_id, at = new_id("r"), now_ms()
    lives = {}
    try:
        runtime.open_session(session_id)
        lives = {s["seatNo"]: runtime.lives[s["lifeId"]] for s in seats if s["lifeId"] in runtime.lives}
        if len(lives) != len(seats):
            raise AdapterError(400, "有提问对象已被删除，请先移除再提问")
        # 上下文组装会跳过其他仍未完成的轮次，避免只带入半轮问题。
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
        # 保存当时的参与者快照，之后改席位或删除实体也能还原这轮问答。
        # 问题保存成功后才调用模型，写盘失败不会产生已开始的模型请求。
        runtime.write_message(session_id, {
            "kind": "you", "text": question, "t": at, "roundId": round_id,
            "messageId": round_id + "_you", "participants": participants,
        })
    except BaseException:
        # 此时协调线程尚未接管，启动阶段失败必须在这里归还锁和轮次标记。
        for life in lives.values():
            life.active_rounds.discard(round_id)
        lock.release()
        raise
    # 外层队列解耦浏览器消费与后台工作；None 是内部结束标记，不发给前端。
    events = queue.Queue()

    def work():
        try:
            saved = True
            for event in _run_parallel(session, participants, lives, question,
                                       settings, transport, runtime, round_id):
                saved = saved and not event.get("saveFailed")
                events.put(event)
            # 调用失败可以作为错误回答保存；落盘失败则不能宣告整轮保存完成。
            if saved:
                events.put({"type": "round_done", "roundId": round_id, "at": at})
        except Exception as e:
            events.put({"type": "error", "message": f"本轮未完整保存：{e}"})
        finally:
            for life in lives.values():
                life.active_rounds.discard(round_id)
            lock.release()
            events.put(None)

    # 【核心】协调线程持有本轮直到保存结束。
    # 浏览器断开或点击“停下”只结束接收；这条后台线程仍继续调用和保存。
    # daemon=True 不保证进程退出后的工作，因此这里只承诺页面断开时继续执行。
    threading.Thread(target=work, daemon=True, name="digitallife-round").start()
    yield {"type": "round_start", "roundId": round_id, "at": at}
    while (event := events.get()) is not None:
        yield event


def _run_parallel(session, seats, lives, question, settings, transport, runtime, round_id):
    """【核心】每位参与者一个工作线程，用队列汇合交错的流事件。

    for 循环只启动线程，不等待上一位回答。各线程独立执行 _run_seat，
    所有对象启动后才在当前线程消费队列；当前没有应用层并发数量上限。
    """
    q: "queue.Queue[dict]" = queue.Queue()
    threads: list[threading.Thread] = []
    for seat in seats:
        life = lives.get(seat["seatNo"])
        # 默认参数固定本次循环的对象，防止线程稍后运行时拿到最后一位。
        def work(seat=seat, life=life):
            try:
                if life is None:
                    raise AdapterError(404, f"选中的那位（{seat['lifeId']}）已经不在了")
                for evt in _run_seat(session, seat, life, question, settings,
                                     transport, runtime, round_id):
                    q.put(evt)
            except OSError as e:
                # 文件可能已有部分写入；报告保存失败，不自动补写或重试追加。
                q.put({"type": "error", "seatNo": seat["seatNo"], "saveFailed": True,
                       "message": f"保存失败，请检查磁盘与备份：{e}"})
            except Exception as e:
                # 一位失败不终止其他线程；尽量将失败作为本人的回答保存。
                failure = f"{type(e).__name__}：{e}"
                answer = {**seat, "content": "", "error": failure}
                try:
                    _record_answer(runtime, session.id, round_id, answer)
                    q.put({"type": "seat_done", **answer})
                except OSError as saved:
                    q.put({"type": "error", "seatNo": seat["seatNo"], "saveFailed": True,
                           "message": f"{failure}；保存失败：{saved}"})
            finally:
                # 无论成功还是失败，都必须发送内部收尾事件，否则消费方会一直等待。
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
    """为一位参与者组装上下文、读取模型流，并保存最终回答。

    seatNo 用于前端定位卡片，lifeId 用于身份和个人历史；两者不能互换。
    模型调用发生在实体锁之外，其他会话可以同时调用同一实体。
    """
    seat_no = seat["seatNo"]
    name = seat["name"]
    yield {"type": "seat_start", "seatNo": seat_no, "lifeId": life.id, "name": name}

    provider = settings_mod.provider(settings, seat.get("providerId") or life.identity.get("providerId"))
    if provider is None:
        raise AdapterError(400, f"{name} 绑定的平台不在了，请重新配置平台")
    # 本会话的模型覆盖值优先；留空时使用实体的默认模型。
    model = seat.get("model") or life.identity.get("model") or ""
    tpl = runtime.templates.get(life.identity.get("templateId")) or templates.Template(
        id="", name=name, system="",
    )
    cap = settings_mod.resolve_output_cap(settings, model)
    notices: list[str] = []

    started = time.time()
    content: list[str] = []
    reasoning: list[str] = []
    usage: dict = {}
    finish = ""
    failure = ""

    def consume(chunks):
        """协议层事件转为页面事件，同时在内存累积最终可保存的回答。"""
        nonlocal finish
        for chunk in chunks:
            kind = chunk["type"]
            if kind == "text":
                content.append(chunk["text"])
            elif kind == "reasoning":
                reasoning.append(chunk["text"])
            elif kind == "usage":
                # 某些协议分开报告输入、输出用量，合并字段而非覆盖整份统计。
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
                    # 保存平台明确给出的输出上限，后续请求可直接采用。
                    if chunk.get("max") and model:
                        runtime.update_settings(model_info={model: {"maxOutput": int(chunk["max"])}})
                continue
            if kind in ("text", "reasoning"):
                yield {**chunk, "seatNo": seat_no}

    try:
        # 【核心】先完整请求，由保护层在明确超限时压缩重试；正常请求只调用一次。
        yield from consume(compression.stream_answer(
            life, session_id=session.id, round_id=round_id, question=question,
            persona=tpl.system, bare=tpl.bare, output_cap=cap,
            provider=provider, model=model, transport=transport,
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
    # 【核心】先保存再发送 seat_done；看到完成事件意味着本人的保存步骤已成功。
    # 流中途失败时也保留已经收到的正文与错误，便于刷新后查看实际结果。
    _record_answer(runtime, session.id, round_id, answer)
    yield {"type": "seat_done", "seatNo": seat_no, **answer}


def _record_answer(runtime, session_id, round_id, answer):
    """将接口的 content 映射为存储的 text，保留轮次、身份和调用状态。"""
    runtime.write_message(session_id, {
        **{k: v for k, v in answer.items() if k != "content"},
        "kind": "self", "text": answer.get("content", ""), "t": now_ms(),
        "roundId": round_id, "messageId": f"{round_id}_{answer['lifeId']}",
    })
