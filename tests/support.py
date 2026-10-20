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


@contextmanager
def running_server(app):
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
