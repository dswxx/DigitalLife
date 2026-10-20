"""HTTP 边界：访问校验、管理接口、NDJSON 流和构建后前端的托管。

聊天业务交给 chat，文件与运行时状态交给 Runtime；本模块负责请求和响应形状。
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import chat, presets, sessions, settings as settings_mod, templates
from . import __version__
from .adapters import AdapterError, list_models
from .adapters.transport import HttpxTransport
from .life import Life
from .runtime import Runtime

# 不兼容的接口形状变化增加此版本，前端启动时校验。
API_VERSION = 7

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1", "testserver"}

DATA_DIR = Path(os.environ.get("DL_DATA_DIR") or Path(__file__).resolve().parent.parent / "data").resolve()
WEB_DIR = Path(__file__).resolve().parent.parent / "web" / "dist"
TOKEN = secrets.token_urlsafe(24)

# 替换完整表达式，包含 null，避免残留无效脚本。
BOOT_PLACEHOLDER = "/*__DL_BOOT__*/null"

# 入口 HTML 包含启动令牌，禁止缓存；带哈希的资源另行长期缓存。
NO_STORE = {"cache-control": "no-store"}


def _data_dir(request: Request) -> Path:
    return Path(getattr(request.app.state, "data_dir", DATA_DIR))


def _settings(request: Request) -> dict:
    return request.app.state.runtime.settings


def _life(request: Request, life_id: str) -> Life:
    base = (_data_dir(request) / "humans").resolve()
    root = (base / life_id).resolve()
    if root.parent != base:
        raise HTTPException(400, "数字人 id 不合法")
    life = request.app.state.runtime.lives.get(life_id)
    if life is None:
        raise HTTPException(404, f"没有这位数字人：{life_id}")
    return life


def create_app(data_dir=None, *, token: str | None = None) -> FastAPI:
    """创建应用，数据目录和访问令牌可注入以便测试。"""
    @asynccontextmanager
    async def lifespan(app):
        # 【核心】每个应用实例持有一份 Runtime 和一个可被多线程复用的 HTTP 客户端。
        # 退出生命周期时关闭连接池，测试也使用相同的初始化与清理流程。
        app.state.runtime = Runtime(app.state.data_dir)
        transport = HttpxTransport()
        app.state.transport = transport
        try:
            yield
        finally:
            transport.close()

    app = FastAPI(title="数字生命 DigitalLife", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.state.token = token or TOKEN
    app.state.data_dir = Path(data_dir) if data_dir else DATA_DIR

    # 限制本机访问，并校验页面启动时注入的令牌。
    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = request.url.hostname or ""
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"error": "只允许本机访问"}, status_code=403)
        if request.url.path.startswith("/api/"):
            if request.headers.get("x-digitallife-token") != request.app.state.token:
                return JSONResponse(
                    {"error": "没有令牌或令牌不对。如果你是刷新页面之后变成这样，"
                              "说明服务重启过——刷新一下页面就好。"},
                    status_code=401,
                )
        return await call_next(request)

    @app.exception_handler(AdapterError)
    async def adapter_error(_: Request, exc: AdapterError):
        return JSONResponse({"error": str(exc)}, status_code=exc.status if exc.status >= 400 else 502)

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(ValueError)
    async def value_error(_: Request, exc: ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(OSError)
    async def storage_error(_: Request, exc: OSError):
        return JSONResponse({"error": f"数据读写失败：{exc}"}, status_code=503)

    # ─────────────────────── 基本状态 ───────────────────────

    @app.get("/api/health")
    def health(request: Request):
        return {
            "ok": True,
            "version": __version__,
            "apiVersion": API_VERSION,
            "dataVersion": presets.DATA_VERSION,
            "dataDir": str(_data_dir(request)),
        }

    @app.get("/api/bootstrap")
    def bootstrap(request: Request):
        """前端启动要的一切，一次拿完。省掉五六个来回。"""
        data_dir = _data_dir(request)
        st = _settings(request)
        return {
            "apiVersion": API_VERSION,
            "version": __version__,
            "dataVersion": presets.DATA_VERSION,
            "dataDir": str(data_dir),
            "settings": settings_mod.public_view(st),
            "templates": [t.to_json() for t in request.app.state.runtime.templates.values()],
            "lives": [request.app.state.runtime.life_summary(l) for l in request.app.state.runtime.lives.values()],
            "sessions": list_sessions(request),
            # 设置页那排「一键添加平台模板」。它是**数据**（放 presets.json），
            # 前端只管画——加一家平台不用改前端代码。
            "providerPresets": presets.PROVIDER_PRESETS,
        }

    # ─────────────────────── 设置与平台 ───────────────────────

    @app.post("/api/settings")
    def post_settings(request: Request, payload: dict = Body(...)):
        """局部更新设置；未传入的字段保持原值，密钥掩码不覆盖实际密钥。"""
        # 必须区分“未传 providers”和“传空列表”：前者保留，后者删除全部平台。
        saved = request.app.state.runtime.update_settings(
            providers=(payload.get("providers") or []) if "providers" in payload else None,
            advanced=payload.get("advanced"), model_info=payload.get("modelInfo"),
        )
        return settings_mod.public_view(saved)

    @app.post("/api/providers/{provider_id}/models")
    def fetch_models(request: Request, provider_id: str):
        """获取并保存平台模型列表及拉取时间。"""
        st = _settings(request)
        prov = settings_mod.provider(st, provider_id)
        if prov is None:
            raise HTTPException(404, f"没有这家平台：{provider_id}")
        got = list_models(prov, transport=request.app.state.transport)
        stamp = int(time.time() * 1000)
        # 基于保存时的最新配置修改这一家，避免较慢的网络请求覆盖其他配置更新。
        request.app.state.runtime.update_settings(providers=lambda providers: [
            {**p, "models": got, "modelsFetchedAt": stamp} if p.get("id") == provider_id else p
            for p in providers
        ])
        return {"models": got, "modelsFetchedAt": stamp}

    @app.post("/api/providers/{provider_id}/test")
    def test_provider(request: Request, provider_id: str):
        st = _settings(request)
        prov = settings_mod.provider(st, provider_id)
        if prov is None:
            raise HTTPException(404, f"没有这家平台：{provider_id}")
        try:
            got = list_models(prov, transport=request.app.state.transport)
            return {"ok": True, "message": f"通了，这家看得见 {len(got)} 个模型", "models": got}
        except AdapterError as e:
            return {"ok": False, "message": str(e), "models": []}

    # ─────────────────────── 人格模板 ───────────────────────

    @app.get("/api/templates")
    def list_templates(request: Request):
        return [t.to_json() for t in request.app.state.runtime.templates.values()]

    @app.post("/api/templates")
    def create_template(request: Request, payload: dict = Body(...)):
        name = (payload.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "给这个模板起个名字")
        tpl = templates.make(
            name, payload.get("system") or "",
            emoji=payload.get("emoji") or "🙂",
            tagline=payload.get("tagline") or "",
            tags=list(payload.get("tags") or []),
            bare=bool(payload.get("bare")),
            origin="user",
        )
        templates.save(_data_dir(request), tpl)
        request.app.state.runtime.refresh_templates()
        return tpl.to_json()

    @app.patch("/api/templates/{template_id}")
    def update_template(request: Request, template_id: str, payload: dict = Body(...)):
        data_dir = _data_dir(request)
        old = request.app.state.runtime.templates.get(template_id)
        if old is None:
            raise HTTPException(404, f"没有这个模板：{template_id}")
        result = templates.update(data_dir, old, payload, lives=list(request.app.state.runtime.lives.values())).to_json()
        request.app.state.runtime.refresh_templates()
        return result

    @app.delete("/api/templates/{template_id}")
    def delete_template(request: Request, template_id: str):
        """在仍有实体引用时拒绝删除，避免产生失去人格来源的实体。"""
        data_dir = _data_dir(request)
        users = [l for l in request.app.state.runtime.lives.values()
                 if l.identity.get("templateId") == template_id]
        if users:
            names = "、".join(l.identity.get("name") or l.id for l in users[:3])
            raise HTTPException(400, f"还有 {len(users)} 位数字人用着这个模板（{names}），"
                                     f"先把他们的模板换掉或把他们删掉")
        if not templates.template_path(data_dir, template_id).exists():
            raise HTTPException(404, f"没有这个模板：{template_id}")
        templates.template_path(data_dir, template_id).unlink()
        request.app.state.runtime.refresh_templates()
        return {"ok": True}

    # ─────────────────────── 数字人（生命）───────────────────────

    @app.get("/api/lives")
    def list_lives(request: Request):
        return [request.app.state.runtime.life_summary(l) for l in request.app.state.runtime.lives.values()]

    @app.post("/api/lives")
    def create_life(request: Request, payload: dict = Body(...)):
        """从已有的人格与平台创建实体，只保存绑定和身份，不复制人格正文。"""
        data_dir = _data_dir(request)
        tpl = request.app.state.runtime.templates.get(payload.get("templateId") or "")
        if tpl is None:
            raise HTTPException(400, "先选一个模板——数字人是从某份人格长出来的")
        provider_id = payload.get("providerId") or ""
        if settings_mod.provider(_settings(request), provider_id) is None:
            raise HTTPException(400, "选一家平台，他要在那家平台上活着")
        life = Life.create(
            data_dir / "humans",
            template_id=tpl.id,
            provider_id=provider_id,
            model=payload.get("model") or "",
            name=(payload.get("name") or (payload.get("model") if tpl.bare else tpl.name) or tpl.name).strip(),
            emoji=payload.get("emoji") or tpl.emoji,
        )
        request.app.state.runtime.lives[life.id] = life
        return request.app.state.runtime.life_summary(life)

    @app.delete("/api/lives/{life_id}")
    def delete_life(request: Request, life_id: str):
        rt = request.app.state.runtime
        _life(request, life_id)
        destination = rt.delete_life(life_id)
        return {"ok": True, "movedTo": str(destination)}

    # ─────────────────────── 一次对话（问了谁）───────────────────────

    @app.get("/api/sessions")
    def list_sessions(request: Request):
        return [sessions.summary(s) for s in
                sorted(request.app.state.runtime.sessions.values(), key=lambda s: s.updated_at, reverse=True)]

    @app.post("/api/sessions")
    def create_session(request: Request, payload: dict = Body(...)):
        data_dir = _data_dir(request)
        sid = payload.get("id")
        if sid and sessions.load(data_dir, sid):
            raise HTTPException(400, f"这个会话 id 已经存在：{sid}")
        s = sessions.create(
            data_dir,
            title=payload.get("title") or sessions.DEFAULT_TITLE,
            seats=list(payload.get("seats") or []),
            session_id=sid,
        )
        request.app.state.runtime.sessions[s.id] = s
        return s.to_json()

    @app.patch("/api/sessions/{session_id}")
    def update_session(request: Request, session_id: str, payload: dict = Body(...)):
        rt = request.app.state.runtime
        rt.ensure_idle(session_id)
        original = rt.sessions.get(session_id)
        # 先编辑副本，保存成功后才替换运行时对象，避免失败操作提前污染当前状态。
        s = sessions.Session.from_json(original.to_json()) if original else None
        if s is None:
            raise HTTPException(404, f"没有这次对话：{session_id}")
        if payload.get("title") is not None:
            s.title = str(payload["title"])
        if payload.get("seats") is not None:
            s.seats = sessions.normalize_seats(list(payload["seats"]))
        rt.save_session(s)
        return s.to_json()

    @app.delete("/api/sessions/{session_id}")
    def delete_session(request: Request, session_id: str):
        rt = request.app.state.runtime
        rt.ensure_idle(session_id)
        if not rt.delete_session(session_id):
            raise HTTPException(404, f"没有这次对话：{session_id}")
        return {"ok": True}

    @app.get("/api/sessions/{session_id}/messages")
    def session_messages(request: Request, session_id: str, before: str = "", limit: int = 3):
        """只从会话完整原文分页，展示不依赖当前实体是否仍存在。"""
        rt = request.app.state.runtime
        if session_id not in rt.sessions:
            raise HTTPException(404, "没有这次对话")
        messages = rt.open_session(session_id)
        return {"sessionId": session_id, **sessions.round_views(messages, before=before, limit=limit)}

    # ─────────────────────── 问一句（核心）───────────────────────

    @app.post("/api/ask")
    def ask(request: Request, payload: dict = Body(...)):
        """【核心】将 chat 的统一事件编码成 NDJSON，一行一个 JSON 对象。

        上游模型使用 SSE，本接口使用 NDJSON，两种流的解析边界不同。
        响应开始后，后续错误通过流内 error 事件报告，不能再改变 HTTP 状态码。
        """
        data_dir = _data_dir(request)
        st = _settings(request)

        def stream():
            try:
                for evt in chat.ask(
                    data_dir,
                    session_id=payload.get("sessionId") or "",
                    question=payload.get("question") or "",
                    settings=st,
                    transport=request.app.state.transport,
                    runtime=request.app.state.runtime,
                ):
                    # 换行是事件边界；一次网络读取可能包含半行或多行，前端负责缓冲。
                    yield (json.dumps(evt, ensure_ascii=False) + "\n").encode("utf-8")
            except AdapterError as e:
                yield (json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False) + "\n").encode("utf-8")
            except Exception as e:  # noqa: BLE001
                yield (json.dumps({"type": "error", "message": f"服务端出错：{type(e).__name__}：{e}"},
                                  ensure_ascii=False) + "\n").encode("utf-8")

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    # ─────────────────────── 前端 ───────────────────────

    @app.get("/")
    def index(request: Request):
        return HTMLResponse(_serve_index(request.app.state.token), headers=NO_STORE)

    @app.get("/{path:path}")
    def static_files(request: Request, path: str):
        target = (WEB_DIR / path).resolve()
        if not target.is_relative_to(WEB_DIR.resolve()):
            raise HTTPException(403, "越界了")  # 防目录穿越
        if not target.exists() or target.is_dir():
            # 前端是单页应用：认不出的路径交给它自己路由。
            # 但 `/api/` 开头的**必须是 404**——否则前端把 HTML 当 JSON 解析，
            # 报出来的错会离真正的原因十万八千里。
            if path.startswith("api/"):
                raise HTTPException(404, f"没有这个接口：/{path}")
            return HTMLResponse(_serve_index(request.app.state.token), headers=NO_STORE)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        # 带哈希的产物可以放心缓存；HTML 不行（理由见 NO_STORE）
        cache = "public, max-age=31536000, immutable" if target.is_relative_to((WEB_DIR / "assets").resolve()) else "no-store"
        return StreamingResponse(iter([target.read_bytes()]), media_type=ctype,
                                 headers={"cache-control": cache})

    return app


def _serve_index(token: str) -> str:
    """向构建产物注入当前启动令牌；令牌不能写入版本库或长期缓存的 JS 文件。"""
    index = WEB_DIR / "index.html"
    if not index.exists():
        return (
            "<!doctype html><meta charset='utf-8'><title>数字生命</title>"
            "<body style='font-family:system-ui;padding:2rem;line-height:1.7'>"
            "<h1>数字生命后端已经起来了</h1>"
            "<p>前端还没构建（<code>web/dist/index.html</code> 不存在）。</p>"
            "<p>可以先试：<code>/api/health</code>、<code>/api/bootstrap</code>。</p>"
            f"<script>window.__DL_BOOT__={{apiVersion:{API_VERSION},token:{json.dumps(token)}}}</script>"
        )
    html = index.read_text(encoding="utf-8")
    boot = json.dumps({"apiVersion": API_VERSION, "token": token, "dataVersion": presets.DATA_VERSION})
    # 令牌只能在服务端注入——它每次启动都不一样，不可能打进前端产物里
    return html.replace(BOOT_PLACEHOLDER, boot)


app = create_app()
