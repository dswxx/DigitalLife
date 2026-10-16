"""一次提问：**请求路径的全过程**。

这一步把前面所有层串起来，而且是整个后端唯一"用户盯着屏幕等"的地方，
所以三条规矩在这里必须同时成立：

  1. **请求路径永远不调用模型做压缩。** 只做确定性裁剪（`context.build_context`）。
     压缩在后台、在上一轮结束之后（`memory.compress`）。
  2. **失败要可见。** 一个席位挂了不能让其他席位消失，也不能让整轮没有输出；
     每块都带 `seatNo`，前端照着往卡片上贴。
  3. **不静默改用户的设置。** 温度被摘、输出上限被改，都通过 notice 说出来。

两种规矩（继承老项目）：
  · parallel 并排：所有人同时听到同一句问题，互相听不到这一轮的回答 → **并发跑**
  · council  会议：按座位顺序一个一个说，后面的人听得到前面的 → **顺序跑**，
    而且前面说过的话要作为「本轮此前已发言的人」进他的这次请求

**记录是每个席位一说完就落盘的**，不是等整轮结束再统一写：
用户看到一半关掉页面时，他刚才问的那句和已经答出来的那些必须留下。
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field

from . import context, sessions, settings as settings_mod, store, templates
from .adapters import AdapterError, stream_chat
from .adapters.core import IDLE_MS
from .life import Life, new_id, now_ms
from .text import estimate_tokens

# 思考吃光额度后重试时，输出上限最多提到这个数。
# max_tokens 是**上限不是目标**，提高它不会让正常回答变长，
# 只会让"想很久"的模型有机会说出正文。
MAX_RETRY_CAP = 32768


@dataclass
class Round:
    """一轮提问。事件流推给前端，answers 落进记录。"""

    id: str
    at: int
    session_id: str
    answers: list[dict] = field(default_factory=list)

    def answer_of(self, seat_no: int) -> dict | None:
        return next((a for a in self.answers if a["seatNo"] == seat_no), None)


# ─────────────────────────── 黄条不复读 ───────────────────────────


def _past_notices(life: Life, session_id: str) -> set[str]:
    """这个会话里已经说过的提示。

    去重的依据是**记录本身**（上一轮把 notices 存进了他的回答里），
    所以不需要额外的状态文件；换一次会话仍然会重新说一次——新会话里它是新信息。
    """
    seen: set[str] = set()
    for e in life.read_session(session_id, only_tail=True, warn=False):
        for n in e.get("notices") or []:
            seen.add(n)
    return seen


def _notices(seen: set[str], texts) -> list[str]:
    """同一句话在这个会话里只说一次。第一次是信息，第二次是噪音。"""
    out = []
    for t in texts:
        t = (t or "").strip()
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ─────────────────────────── 主入口 ───────────────────────────


def ask(data_dir, *, session_id: str, question: str, settings: dict, transport,
        only_seat: int | None = None):
    """问一句。**这是一个生成器**：边算边把事件吐出去。

    用生成器而不是"先算完再返回"，是因为用户要边看边读——七个席位同时出字才算圆桌。
    """
    session = sessions.load(data_dir, session_id)
    if session is None:
        raise AdapterError(404, f"找不到这张桌子：{session_id}")
    question = (question or "").strip()
    if not question:
        raise AdapterError(400, "先写点什么再问")
    seats = [s for s in session.ordered() if only_seat is None or s["seatNo"] == only_seat]
    if not seats:
        raise AdapterError(400, "这张桌子上还没有数字人")

    rnd = Round(id=new_id("r"), at=now_ms(), session_id=session_id)

    lives: dict[int, Life] = {}
    for seat in seats:
        life = Life.load(data_dir / "humans" / seat["lifeId"])
        if (life.root / "identity.json").exists():
            lives[seat["seatNo"]] = life
        else:
            yield {"type": "error", "seatNo": seat["seatNo"],
                   "message": f"座位上那位（{seat['lifeId']}）已经不在了"}

    if session.mode == "council":
        spoken: list[dict] = []
        for seat in seats:  # 顺序：一个一个说，后面的听得到前面的
            life = lives.get(seat["seatNo"])
            if life is None:
                continue
            yield from _run_seat(data_dir, session, seat, life, question, rnd, settings,
                                 transport, list(spoken))
            got = rnd.answer_of(seat["seatNo"])
            if got and got.get("content"):
                spoken.append({"who": got.get("name") or "某位", "text": got["content"]})
    else:
        yield from _run_parallel(data_dir, session, seats, lives, question, rnd, settings, transport)

    sessions.touch(data_dir, session_id)
    yield {"type": "round_done", "roundId": rnd.id, "at": rnd.at}


def _run_parallel(data_dir, session, seats, lives, question, rnd, settings, transport):
    """并排：所有人同时听到同一句问题。**真的并发跑**，靠队列把字交错吐出来。

    并发是有必要的：七个席位串行跑，用户要等七倍时间；而且"同时回答"本来就是
    这个产品的观感。代价是要处理交错——所以每块都带 seatNo。
    """
    q: "queue.Queue[dict]" = queue.Queue()
    threads: list[threading.Thread] = []
    for seat in seats:
        life = lives.get(seat["seatNo"])
        if life is None:
            continue

        def work(seat=seat, life=life):
            try:
                for evt in _run_seat(data_dir, session, seat, life, question, rnd, settings,
                                     transport, []):
                    q.put(evt)
            except Exception as e:  # noqa: BLE001 —— 一个席位炸了不能拖垮整轮
                q.put({"type": "error", "seatNo": seat["seatNo"], "message": f"{type(e).__name__}：{e}"})
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


def _run_seat(data_dir, session, seat, life, question, rnd: Round, settings: dict,
              transport, heard: list[dict]):
    """一个席位的一整次回答。产出事件，**结束时把这一轮记进他的记录**。"""
    seat_no = seat["seatNo"]
    name = life.identity.get("name") or seat["lifeId"]
    yield {"type": "seat_start", "seatNo": seat_no, "lifeId": life.id, "name": name}

    provider = settings_mod.provider(settings, seat.get("providerId") or life.identity.get("providerId"))
    if provider is None:
        yield {"type": "error", "seatNo": seat_no,
               "message": f"{name} 绑的那家平台不在了——去数字人库里给他换一家"}
        return
    model = seat.get("model") or life.identity.get("model") or ""
    tpl = _template_for(data_dir, life)
    window = settings_mod.resolve_window(settings, model, provider.get("id", ""))
    wanted, cap = settings_mod.resolve_output_cap(settings, model, window)

    built = context.build_context(
        life,
        session_id=session.id,
        question=question,
        window=window.tokens,
        output_reserve=cap,
        heard=heard or None,
        persona=tpl.system,
        bare=tpl.bare,
    )

    seen = _past_notices(life, session.id)
    notices: list[str] = []
    for text in _notices(seen, built.notices):
        notices.append(text)
        yield {"type": "notice", "seatNo": seat_no, "text": text}

    started = time.time()
    content: list[str] = []
    reasoning: list[str] = []
    usage: dict = {}
    finish = ""
    failure = ""

    def call(max_tokens: int):
        return stream_chat(
            provider, model=model,
            system=built.system,
            messages=built.messages,
            temperature=seat.get("temperature"),
            max_tokens=max_tokens,
            timeout=IDLE_MS / 1000,
            transport=transport,
        )

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
                for text in _notices(seen, [chunk["text"]]):
                    notices.append(text)
                    yield {"type": "notice", "seatNo": seat_no, "text": text}
                continue
            elif kind == "limit":
                for text in _notices(seen, [chunk["text"]]):
                    notices.append(text)
                    yield {**chunk, "seatNo": seat_no, "text": text}
                    # 平台说了它的上限 → 记下来，下次不再白挨一次 400
                    if chunk.get("max") and model:
                        settings_mod.learn_model_limit(data_dir, settings, model, int(chunk["max"]))
                continue
            if kind in ("text", "reasoning"):
                yield {**chunk, "seatNo": seat_no}

    try:
        yield from consume(call(cap))

        # ── 思考把额度吃光、正文一个字都没轮到 → 提一档再试一次 ──
        # 老项目那个"卡在思考中"就是这么修的。
        if (not "".join(content).strip()) and "".join(reasoning).strip() and finish == "length":
            bigger = min(max(cap * 2, 16384), MAX_RETRY_CAP, window.tokens)
            if bigger > cap:
                waste = usage.get("output") or estimate_tokens("".join(reasoning))
                note = (f"这个模型把 {cap} token 的输出额度全用在思考上了，正文一个字都没轮到"
                        f"——已自动提到 {bigger} 再试一次。（想省掉这次浪费约 {waste} token 的输出，"
                        f"把「单次回答最长 token」调到 {bigger} 以上；你现在设的是 {wanted}）")
                notices.append(note)
                yield {"type": "notice", "seatNo": seat_no, "text": note}
                content.clear()
                reasoning.clear()
                yield from consume(call(bigger))
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
                f"——把「单次回答最长 token」调大（这个模型最多到 {window.tokens}）")
        if note not in notices:
            notices.append(note)
            yield {"type": "notice", "seatNo": seat_no, "text": note}

    answer = {
        "seatNo": seat_no,
        "lifeId": life.id,
        "name": name,
        "model": model,
        "content": text,
        "reasoning": think,
        "usage": usage,
        "tokens": int(usage.get("output") or estimate_tokens(text)),
        "finishReason": finish,
        "ms": int((time.time() - started) * 1000),
        "error": failure,
        "notices": notices,
        "window": window.tokens,
        "windowSource": window.source,
        "outputCap": cap,
        "keptEntries": built.session_entries,
        "droppedEntries": built.dropped,
    }
    rnd.answers.append(answer)
    # **一说完就落盘**：用户中途关页面，已经答出来的这些也必须留下
    _record_round(life, session, seat, question, rnd.at, answer, heard or [])
    yield {"type": "seat_done", "seatNo": seat_no, **answer}


def _template_for(data_dir, life) -> templates.Template:
    tpl = templates.load(data_dir, life.identity.get("templateId") or "")
    if tpl is None:
        # 模板被删了：不能因此让他变成哑巴，用一个空人格（等于"没写人格"）
        return templates.Template(id="", name=life.identity.get("name") or "他", system="")
    return tpl


def _record_round(life: Life, session, seat: dict, question: str, at: int,
                  answer: dict, heard: list[dict]) -> None:
    """把这一轮记进他的记录：**你说了什么、他听到了什么、他说了什么**。

    时间是**原始时间戳**：提问用本轮开始的时间，他的回答用开始 + 他花了多久。
    这样"记忆的年龄"才是真的。
    """
    entries = [{"t": at, "kind": "you", "text": question}]
    for h in heard:
        entries.append({"t": at, "kind": "heard", "who": h.get("who") or "某位", "text": h.get("text", "")})
    if answer.get("content"):
        entries.append({
            "t": at + int(answer.get("ms") or 0),
            "kind": "self",
            "text": answer["content"],
            "reasoning": answer.get("reasoning") or "",
            "tokens": answer.get("tokens", 0),
            "model": answer.get("model", ""),
            "finishReason": answer.get("finishReason", ""),
            "notices": list(answer.get("notices") or []),
        })
    with store.lock_for(life.id):
        for e in entries:
            store.append_jsonl(life.session_path(session.id), e)
