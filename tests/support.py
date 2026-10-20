"""测试用合成数据与本机服务，不读取真实聊天或密钥。"""

from contextlib import contextmanager
from pathlib import Path
import socket
import threading
import time

import uvicorn

from server import sessions, settings, templates
from server.life import Life

ROOT = Path(__file__).resolve().parent.parent


def seed_data(root, base_url="http://127.0.0.1:1/v1"):
    """创建两位数字人和一位普通 AI；HTTP 测试传入本机假上游地址。"""
    tpl = templates.make("测试作者", "你是测试作者，直接回答问题。")
    bare = templates.make("普通 AI", "", bare=True)
    for item in (tpl, bare):
        templates.save(root, item)
    settings.update(root, providers=[{
        "id": "p", "name": "测试平台", "kind": "openai", "apiKey": "test-secret",
        "baseUrl": base_url, "models": [{"id": "model-a"}, {"id": "model-b"}],
    }])
    lives = [Life.create(root / "humans", template_id=template.id, provider_id="p",
                         model=model, name=name)
             for template, model, name in [(tpl, "model-a", "作者甲"),
                                            (tpl, "model-b", "作者乙"),
                                            (bare, "model-a", "普通 AI")]]
    session = sessions.create(root, seats=[{"lifeId": life.id} for life in lives[:2]])
    return lives, session


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fill_history(runtime, session, rounds=6, size=150):
    """通过真实两处保存入口生成长历史，用于 HTTP 和浏览器压缩场景。"""
    base = len(runtime.open_session(session.id))
    people = [{**seat, "name": runtime.lives[seat["lifeId"]].identity["name"], "emoji": "🙂"}
              for seat in session.ordered()]
    for i in range(rounds):
        rid = f"fixture_{session.id}_{base}_{i}"
        runtime.write_message(session.id, {"kind": "you", "text": f"较早问题{i}：" + "问" * size,
                                          "roundId": rid, "messageId": rid + "_you", "t": i + 1,
                                          "participants": people})
        for person in people:
            runtime.write_message(session.id, {**person, "kind": "self", "text": f"较早回答{i}：" + "答" * size,
                                              "roundId": rid, "messageId": rid + "_" + person["lifeId"], "t": i + 1})


@contextmanager
def running_server(app):
    """在临时本机端口运行真实 ASGI 生命周期，退出测试时通知服务结束并等待回收。"""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        for _ in range(100):
            if server.started:
                break
            if not thread.is_alive():
                raise RuntimeError("测试服务启动失败")
            time.sleep(0.05)
        else:
            raise RuntimeError("测试服务启动超时")
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
