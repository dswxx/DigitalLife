"""适配器测试：**除了真去发 HTTP，其余全部验完**。

    py -3 dev/test_adapters.py

传输层是注入的（`core.Transport`），所以这里可以按剧本安排上游：
先说"温度不支持"再接受、先说"max_tokens 超范围"再说清上限、
流到一半插一个坏 JSON、把中文切成半个字发过来……

验的都是**老项目踩过坑才写下来的行为**，换栈时不能丢：
  · 400 要看着报错内容分派（max_tokens 还是上下文）
  · 温度不被接受 → 摘掉重试，并且**告诉用户**（不静默改设置）
  · max_tokens 超范围 → 按平台说的上限重试，并把学到的值交回上层
  · finish_reason 要看（推理模型常把额度吃光、正文一个字都没轮到）
  · reasoning_content / reasoning 两种字段名都要认
  · 中文被分块切断不能变成乱码
  · 连接开着但不再发数据 → 明确失败，而不是永远转圈
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.adapters import anthropic, core, list_models, openai, sse  # noqa: E402
from server.adapters import stream_chat as real_stream_chat  # noqa: E402

passed = failed = 0


def check(cond, msg):
    global passed, failed
    if cond:
        passed += 1
        print(f"  ✅ {msg}")
    else:
        failed += 1
        print(f"  ❌ {msg}")


class ScriptedTransport:
    """按剧本返回的假传输。每次开口消耗一条剧本。"""

    def __init__(self, script: list[dict]):
        self.script = list(script)
        self.requests: list[dict] = []

    def open_stream(self, *, url, headers, body, timeout):
        self.requests.append({"url": url, "headers": headers, "body": json.loads(json.dumps(body))})
        step = self.script.pop(0) if self.script else {"status": 200, "chunks": []}
        if "raise" in step:
            raise step["raise"]
        return core.Opened(
            status=step.get("status", 200),
            chunks=iter(step.get("chunks", [])),
            error_text=step.get("error", ""),
        )

    def get_json(self, *, url, headers, timeout):
        self.requests.append({"url": url, "headers": headers})
        step = self.script.pop(0) if self.script else {"status": 200, "text": "{}"}
        return step.get("status", 200), step.get("text", "{}")


def sse_lines(*objects) -> list[bytes]:
    return [(("data: " + (o if isinstance(o, str) else json.dumps(o, ensure_ascii=False))) + "\n\n").encode("utf-8")
            for o in objects]


def collect(provider, transport, **kw):
    return list(real_stream_chat(provider, transport=transport, **kw))


DS = {"id": "pv_1", "name": "DeepSeek", "kind": "openai",
      "baseUrl": "https://api.deepseek.com", "apiKey": "sk-test"}


def main() -> int:
    # ── 1. SSE 解析：中文被分块切断 ──
    print("── SSE 解析（最容易悄悄丢字的一层）──")
    payload = 'data: {"choices":[{"delta":{"content":"你好，世界"}}]}\n\n'.encode("utf-8")
    whole = list(sse.iter_sse_data([payload]))
    check(whole and "你好，世界" in whole[0], "整块发过来时解析正常")
    # 故意切在"世"这个字的中间（UTF-8 三字节）
    for cut in range(1, len(payload)):
        got = list(sse.iter_sse_data([payload[:cut], payload[cut:]]))
        if not got or "你好，世界" not in got[0]:
            check(False, f"在第 {cut} 字节切开就出错了：{got}")
            break
    else:
        check(True, f"在所有 {len(payload) - 1} 个位置切开都能还原（增量解码器在工作）")

    check(list(sse.iter_sse_data([b'data: {"a":1}\n'])) == ['{"a":1}'],
          "最后一块没有换行也要吐出来（否则丢最后一句话）")
    check(list(sse.iter_sse_data([b": heartbeat\n\n", b"event: x\n", b'data: {"a":1}\n\n']))
          == ['{"a":1}'], "心跳、event 行、空行都跳过，只认 data")
    check(list(sse.iter_sse_data([b'data: {"a":1}\r\n\r\n'])) == ['{"a":1}'],
          "CRLF 换行也要认（有些平台用 \\r\\n）")

    # ── 2. OpenAI 兼容：正常流 ──
    print("\n── OpenAI 兼容：正常一次流式回答 ──")
    tr = ScriptedTransport([{"chunks": sse_lines(
        {"choices": [{"delta": {"reasoning_content": "让我想想"}}]},
        {"choices": [{"delta": {"content": "第一段"}}]},
        {"choices": [{"delta": {"content": "第二段"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"usage": {"prompt_tokens": 100, "completion_tokens": 20}},
        "[DONE]",
    )}])
    got = collect(DS, tr, model="deepseek-chat", system="你是苏格拉底。",
                  messages=[{"role": "user", "content": "你好"}])
    types = [g["type"] for g in got]
    check(types[-1] == "done", "最后一块是 done")
    reply = got[-1]["reply"]
    check(reply.text == "第一段第二段", f"正文拼对了（{reply.text}）")
    check(reply.reasoning == "让我想想", "思考过程单独收着，不混进正文")
    check(reply.prompt_tokens == 100 and reply.completion_tokens == 20, "用量收到了")
    check(reply.finish_reason == "stop", "结束原因收到了")
    sent = tr.requests[0]
    check(sent["url"] == "https://api.deepseek.com/v1/chat/completions", f"URL 补上了 /v1（{sent['url']}）")
    check(sent["headers"]["authorization"] == "Bearer sk-test", "认证头对")
    check(sent["body"]["messages"][0] == {"role": "system", "content": "你是苏格拉底。"},
          "人格作为 system 放在最前面")
    check(sent["body"]["stream"] is True, "开了流式")
    check("temperature" not in sent["body"], "**没设温度时不发这个字段**（有些模型只接受固定温度）")
    check(sent["body"]["max_tokens"] == core.DEFAULT_MAX_TOKENS, "带上了输出上限")

    # ── 3. 结束原因：推理把额度吃光 ──
    print("\n── 推理模型把额度吃光（老项目那个「卡在思考中」）──")
    tr = ScriptedTransport([{"chunks": sse_lines(
        {"choices": [{"delta": {"reasoning_content": "想了很久很久"}}]},
        {"choices": [{"delta": {}, "finish_reason": "length"}]},
        "[DONE]",
    )}])
    got = collect(DS, tr, model="m", system=None, messages=[{"role": "user", "content": "1+1"}])
    reply = got[-1]["reply"]
    check(reply.finish_reason == "length",
          "**finish_reason=length 被看到了**——正文是空的，但原因清清楚楚，不用猜")
    check(reply.text == "" and reply.reasoning == "想了很久很久", "思考有内容、正文是空的（这正是要报出来的情况）")

    # ── 4. 温度不被接受 → 摘掉重试 ──
    print("\n── 模型不接受自定义温度 ──")
    tr = ScriptedTransport([
        {"status": 400, "error": '{"error":{"message":"temperature is not supported for this model"}}'},
        {"chunks": sse_lines({"choices": [{"delta": {"content": "好"}}]}, "[DONE]")},
    ])
    got = collect(DS, tr, model="kimi-k2", system=None, temperature=0.7,
                  messages=[{"role": "user", "content": "hi"}])
    check(len(tr.requests) == 2, f"重发了一次（共 {len(tr.requests)} 次请求）")
    check("temperature" in tr.requests[0]["body"], "第一次带了温度")
    check("temperature" not in tr.requests[1]["body"], "**第二次把温度摘掉了**")
    notices = [g for g in got if g["type"] == "notice"]
    check(notices and "不接受自定义温度" in notices[0]["text"],
          f"而且明确告诉用户，不静默改设置（{notices[0]['text'] if notices else '没提示'}）")
    check(got[-1]["reply"].text == "好", "重试之后正常拿到回答")

    # ── 5. max_tokens 超范围 → 按平台说的改 ──
    print("\n── max_tokens 超出这个模型的范围 ──")
    tr = ScriptedTransport([
        {"status": 400, "error": '{"error":{"message":"Invalid max_tokens value, the valid range of max_tokens is [1, 393216]"}}'},
        {"chunks": sse_lines({"choices": [{"delta": {"content": "好了"}}]}, "[DONE]")},
    ])
    got = collect(DS, tr, model="m", system=None, max_tokens=500000,
                  messages=[{"role": "user", "content": "hi"}])
    check(tr.requests[1]["body"]["max_tokens"] == 393216,
          f"按平台说的 393216 重发（实际 {tr.requests[1]['body'].get('max_tokens')}）")
    limits = [g for g in got if g["type"] == "limit"]
    check(limits and limits[0]["max"] == 393216,
          "把学到的上限交回上层（下次不用再白挨一个 400）")
    check("393216" in limits[0]["text"], f"提示里说清了平台允许多少：{limits[0]['text'][:50]}")

    # 平台说不出具体数 → 砍半；再不行 → 干脆不发这个字段
    tr = ScriptedTransport([
        {"status": 400, "error": '{"error":{"message":"max_tokens too large"}}'},   # 首发
        {"status": 400, "error": '{"error":{"message":"max_tokens too large"}}'},   # 砍半之后还是不行
        {"chunks": sse_lines({"choices": [{"delta": {"content": "行"}}]}, "[DONE]")},  # 不发这个字段，行了
    ])
    got = collect(DS, tr, model="m", system=None, max_tokens=4096,
                  messages=[{"role": "user", "content": "hi"}])
    check(tr.requests[1]["body"]["max_tokens"] == 2048, "说不出具体数时先砍半")
    check("max_tokens" not in tr.requests[-1]["body"], "最后退到「干脆不发这个字段」，让平台用默认值")
    check(got[-1]["reply"].text == "行", "最终拿到了回答")
    check([g for g in got if g["type"] == "limit"][0]["max"] == 0,
          "这种情况提示里不说具体数字（因为平台没说）")

    # ── 6. 各种平台报错都要翻成人话 ──
    print("\n── 报错文案（400 要看着内容分派，不能都说「上下文撑爆了」）──")
    cases = [
        (401, '{"error":{"message":"invalid api key"}}', "API Key"),
        (402, '{"error":{"message":"Insufficient Balance"}}', "余额"),
        (429, '{"error":{"message":"rate limit"}}', "限流"),
        (404, '{"error":{"message":"model not found"}}', "模型名"),
        (500, '{"error":{"message":"boom"}}', "平台内部错误"),
    ]
    for status, body, expect in cases:
        tr = ScriptedTransport([{"status": status, "error": body}])
        try:
            collect(DS, tr, model="m", system=None, temperature=0.5,
                    messages=[{"role": "user", "content": "hi"}])
            check(False, f"{status} 应该抛错")
        except core.AdapterError as e:
            check(expect in str(e), f"{status} → {str(e)[:60]}")
    # 400 的两种分派
    tr = ScriptedTransport([{"status": 400, "error": '{"error":{"message":"max_tokens must be <= 8192"}}'},
                            {"status": 400, "error": '{"error":{"message":"max_tokens must be <= 8192"}}'},
                            {"status": 400, "error": '{"error":{"message":"max_tokens must be <= 8192"}}'},
                            {"status": 400, "error": '{"error":{"message":"max_tokens must be <= 8192"}}'}])
    try:
        collect(DS, tr, model="m", system=None, max_tokens=99999, messages=[{"role": "user", "content": "x"}])
        check(False, "应该抛错")
    except core.AdapterError as e:
        check("输出上限" in str(e) and "记忆轮数" not in str(e),
              f"max_tokens 的 400 不该把人往「调小记忆轮数」上引：{str(e)[:70]}")
    tr = ScriptedTransport([{"status": 400, "error": '{"error":{"message":"This model maximum context length is 65536 tokens"}}'}])
    try:
        collect(DS, tr, model="m", system=None, messages=[{"role": "user", "content": "x"}])
        check(False, "应该抛错")
    except core.AdapterError as e:
        check("上下文" in str(e), f"上下文超了的 400 才说调小记忆轮数：{str(e)[:70]}")

    # ── 7. 网络异常 ──
    print("\n── 网络异常也要说人话 ──")
    for exc, expect in [
        (OSError("[Errno -2] getaddrinfo failed"), "域名解析失败"),
        (ConnectionRefusedError("connection refused"), "连接被拒绝"),
        (core.AdapterError(504, "已经 90 秒没有收到任何输出了——连接还开着"), "没有收到任何输出"),
    ]:
        tr = ScriptedTransport([{"raise": exc}])
        try:
            collect(DS, tr, model="m", system=None, messages=[{"role": "user", "content": "x"}])
            check(False, f"{expect} 应该抛错")
        except core.AdapterError as e:
            check(expect in str(e), f"{type(exc).__name__} → {str(e)[:56]}")

    # ── 8. 流里插坏数据不能中断整段回答 ──
    print("\n── 上游偶尔插半截数据 ──")
    tr = ScriptedTransport([{"chunks": [
        'data: {"choices":[{"delta":{"content":"前半"}}]}\n\n'.encode("utf-8"),
        b'data: {"choices":[{"delta":\n\n',          # 半截 JSON
        b'data: {oops not json}\n\n',
        'data: {"choices":[{"delta":{"content":"后半"}}]}\n\n'.encode("utf-8"),
        b'data: [DONE]\n\n',
    ]}])
    got = collect(DS, tr, model="m", system=None, messages=[{"role": "user", "content": "x"}])
    check(got[-1]["reply"].text == "前半后半",
          f"坏数据跳过，好数据照样收到（{got[-1]['reply'].text}）")
    check(not [g for g in got if g["type"] == "error"], "而且不把整段标成失败")

    # 但上游明确报错要抛出来
    tr = ScriptedTransport([{"chunks": sse_lines({"error": {"message": "quota exhausted"}}, "[DONE]")}])
    try:
        collect(DS, tr, model="m", system=None, messages=[{"role": "user", "content": "x"}])
        check(False, "流里的 error 应该抛出来")
    except core.AdapterError as e:
        check("quota exhausted" in str(e), f"流里的 error 抛出来了：{str(e)[:50]}")

    # ── 9. Anthropic ──
    print("\n── Anthropic：人格在顶层、认证头不同、事件更细 ──")
    cl = {"id": "pv_a", "name": "Claude", "kind": "anthropic",
          "baseUrl": "https://api.anthropic.com", "apiKey": "sk-ant"}
    tr = ScriptedTransport([{"chunks": sse_lines(
        {"type": "message_start", "message": {"usage": {"input_tokens": 50}}},
        {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "琢磨中"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "我认为"}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "是这样"}},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    )}])
    got = collect(cl, tr, model="claude-sonnet-4", system="你是苏格拉底。",
                  messages=[{"role": "user", "content": "hi"}], temperature=0.9, max_tokens=1024)
    sent = tr.requests[0]
    check(sent["url"] == "https://api.anthropic.com/v1/messages", f"URL 对（{sent['url']}）")
    check(sent["headers"].get("x-api-key") == "sk-ant" and "authorization" not in sent["headers"],
          "认证头是 x-api-key，不是 Bearer")
    check(sent["headers"].get("anthropic-version") == anthropic.ANTHROPIC_VERSION, "带上了版本头")
    check(sent["body"]["system"] == "你是苏格拉底。", "人格在顶层 system 字段，不在 messages 里")
    check(all(m["role"] != "system" for m in sent["body"]["messages"]),
          "messages 里没有 system（Anthropic 不接受）")
    check(sent["body"]["temperature"] == 0.9, "温度原样给（没超上限）")
    reply = got[-1]["reply"]
    check(reply.text == "我认为是这样", f"正文拼对（{reply.text}）")
    check(reply.reasoning == "琢磨中", "thinking_delta 当成思考过程")
    check(reply.finish_reason == "length", "stop_reason=max_tokens 映射成统一的 length")
    check(reply.prompt_tokens == 50 and reply.completion_tokens == 7, "两处用量都收齐了")

    tr = ScriptedTransport([{"chunks": sse_lines({"type": "message_stop"})}])
    collect(cl, tr, model="m", system=None, messages=[{"role": "user", "content": "x"}], temperature=3.0)
    check(tr.requests[0]["body"]["temperature"] == 1, "温度超过 1 会被夹到 1（Anthropic 的硬上限）")

    # ── 10. 参数校验要早报错 ──
    print("\n── 参数不对就早点说，别等发出去 ──")
    for bad, expect in [
        ({**DS, "apiKey": ""}, "API Key"),
        ({**DS, "baseUrl": ""}, "Base URL"),
        ({**DS, "baseUrl": "不是网址"}, "Base URL 格式"),
    ]:
        try:
            collect(bad, ScriptedTransport([]), model="m", system=None, messages=[{"role": "user", "content": "x"}])
            check(False, f"{expect} 应该报错")
        except core.AdapterError as e:
            check(expect in str(e), f"{expect} → {str(e)[:50]}")
    try:
        collect(DS, ScriptedTransport([]), model="", system=None, messages=[{"role": "user", "content": "x"}])
        check(False, "没选模型应该报错")
    except core.AdapterError as e:
        check("还没选模型" in str(e), "没选模型 → 提示去选模型")
    check(core.normalize_base("https://api.deepseek.com/") == "https://api.deepseek.com/v1",
          "尾斜杠和缺 /v1 都能正确处理")
    check(core.normalize_base("https://api.deepseek.com/v1/chat") == "https://api.deepseek.com/v1/chat",
          "已经写了路径就不要乱补")

    # ── 11. max_tokens 上限解析（三种文案）──
    print("\n── 从报错里读平台的上限（三种文案）──")
    samples = [
        ("the valid range of max_tokens is [1, 393216]", 393216),
        ("max_tokens: 100000 > 64000, which is the maximum allowed", 64000),
        ("maximum allowed number of output tokens is 8192", 8192),
        ("something unrelated 12345", 0),
    ]
    for text, expect in samples:
        got = core.parse_max_tokens_limit(text)
        check(got == expect, f"{text[:42]}… → {got}（期望 {expect}）")

    # ── 12. 模型列表 ──
    print("\n── 模型列表 ──")
    tr = ScriptedTransport([{"text": json.dumps({"data": [
        {"id": "deepseek-chat", "created": 1700000000},
        {"id": "deepseek-reasoner", "created": 1700000000000},
        "plain-string-model",
    ]})}])
    got = list_models(DS, transport=tr)
    check(len(got) == 3, f"三种形状的条目都认（{len(got)} 条）")
    check(got[0]["created"] == 1700000000000, "秒级时间戳归一成毫秒")
    check(got[1]["created"] == 1700000000000, "毫秒的原样保留")
    check(got[2]["id"] == "plain-string-model" and got[2]["created"] == 0,
          "只给名字的也认；时间认不出来就是 0（不瞎猜）")

    tr = ScriptedTransport([{"status": 404, "text": "not found"}])
    try:
        list_models(DS, transport=tr)
        check(False, "拉不到模型列表应该报错")
    except core.AdapterError as e:
        check("手动填" in str(e), f"而且告诉用户可以手填：{str(e)[:60]}")

    print(f"\n{'=' * 46}\n通过 {passed}，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
