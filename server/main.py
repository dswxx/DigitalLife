"""HTTP 层：FastAPI 应用。

这一层**故意很薄**——所有判断都在下面的模块里，那些都有测试。
它只做四件事：路由、三层安全、把事件流转成 NDJSON、托管前端。

三层安全（继承老项目，一条都不能省）：

  1. **只绑 127.0.0.1**：不是"默认本地"，是根本不监听外网网卡
  2. **Host 白名单**：只认 127.0.0.1 / localhost / [::1]。
     防的是 DNS rebinding——别的网站的 JS 让浏览器去请求 localhost 时，
     Host 头会是那个域名，一眼就能拦下来。
  3. **每次启动一个随机 token**：前端从注入的 HTML 里拿到它，
     之后每个接口都要带 `x-roundtable-token`。

密钥不出本机：所有返回里的 apiKey 都是掩码（`settings.public_view`）。
"""

from __future__ import annotations

import json
import mimetypes
import secrets
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import chat, memory, presets, sessions, settings as settings_mod, store, templates
from . import __version__
from .adapters import AdapterError, list_models
from .adapters.transport import HttpxTransport
from .life import Life, MEM_TYPES

# 前端要求的接口版本。前端启动时对不上就直接提示"刷新一下"，
# 而不是拿着旧代码去调新接口，然后报一堆看不懂的错。
API_VERSION = 4

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1", "testserver"}

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
WEB_DIR = Path(__file__).resolve().parent.parent / "web" / "dist"
TOKEN = secrets.token_urlsafe(24)

# 前端 index.html 里要放这一串当占位符，**它把兜底值一起包含进去**：
#
#     <script>window.__RT_BOOT__ = /*__RT_BOOT__*/null;</script>
#
# 服务端把它整串换成真正的启动数据（含每次启动随机生成的令牌），结果是
# `window.__RT_BOOT__ = {"apiVersion":…};`。没被注入时那行也是合法 JS（值为 null）。
#
# 三个坑，前两个都踩过：
#   · 不能用光秃秃的 `__RT_BOOT__`——`window.__RT_BOOT__ = ...` 里的属性名会一起被换掉
#   · 占位符不能只包注释（`= /*__RT_BOOT__*/null;` 这种）——只换注释的话会剩一个
#     尾巴 `null`，变成 `= {…}null;`，整段内联脚本语法错误
#   · 占位符更不能写在花括号**里面**（`= {/*__RT_BOOT__*/}`）——替换完是 `={{…}}`
#
# 这两种错的症状一模一样：**前端白屏 + 说连不上后端**，而报错在浏览器控制台里，
# 离真正的原因很远。所以 test_http.py 里专门钉了"不能出现 {{ 或 }null"。
BOOT_PLACEHOLDER = "/*__RT_BOOT__*/null"

# 那份 HTML **永远不许被浏览器缓存**：它带着"这次启动"的令牌。
# 缓存住的话，服务重启一次（令牌变了）浏览器还在用旧的——表现是"突然连不上后端"，
# 而真正的原因和现象离得很远。带哈希的 JS/CSS 相反，可以放心长期缓存。
NO_STORE = {"cache-control": "no-store"}


def _data_dir(request: Request) -> Path:
    return Path(getattr(request.app.state, "data_dir", DATA_DIR))


def _settings(request: Request) -> dict:
    app = request.app
    if not hasattr(app.state, "settings_cache"):
        app.state.settings_cache = settings_mod.load(_data_dir(request))
    return app.state.settings_cache


def _reload_settings(request: Request) -> dict:
    request.app.state.settings_cache = settings_mod.load(_data_dir(request))
    return request.app.state.settings_cache


def _life(request: Request, life_id: str) -> Life:
    life = Life.load(_data_dir(request) / "humans" / life_id)
    if not (life.root / "identity.json").exists():
        raise HTTPException(404, f"没有这位数字人：{life_id}")
    return life


def _summary(data_dir: Path, life: Life) -> dict:
    """数字人库里那张卡要显示的东西：聊过几次、记了几条、上次什么时候。"""
    tpl = templates.load(data_dir, life.identity.get("templateId") or "")
    sids = life.sessions()
    last = 0
    for sid in sids:
        for e in life.read_session(sid, only_tail=True, warn=False):
            last = max(last, int(e.get("t") or 0))
        last = max(last, int(life.compaction(sid).get("compactedThrough") or 0))
    return {
        "id": life.id,
        "name": life.identity.get("name") or "",
        "emoji": life.identity.get("emoji") or "🙂",
        "providerId": life.identity.get("providerId") or "",
        "model": life.identity.get("model") or "",
        "bornAt": life.identity.get("bornAt") or 0,
        "templateId": life.identity.get("templateId") or "",
        "templateName": tpl.name if tpl else "",
        "templateBare": bool(tpl.bare) if tpl else False,
        "sessionCount": len(sids),
        "memoryCount": len(list(life.memory_dir().glob("m_*.md"))) if life.memory_dir().exists() else 0,
        "lastTalkedAt": last,
    }


def create_app(data_dir=None, *, token: str | None = None) -> FastAPI:
    """建应用。`data_dir`/`token` 可以注入——测试要用临时目录和固定 token。

    `data_dir` 默认是**项目里的 data/**（按本文件位置算，不按当前目录算）——
    否则从别处启动、或者被别的东西改了 cwd，数据就会跑到别的地方去。
    """
    app = FastAPI(title="圆桌 Roundtable", docs_url=None, redoc_url=None)
    app.state.token = token or TOKEN
    app.state.data_dir = Path(data_dir) if data_dir else DATA_DIR
    app.state.transport = HttpxTransport()
    app.state.settings_cache = settings_mod.load(app.state.data_dir)

    # ── 三层安全里的第 2、3 层 ──
    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").split(":")[0] or ""
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"error": "只允许本机访问"}, status_code=403)
        if request.url.path.startswith("/api/"):
            if request.headers.get("x-roundtable-token") != request.app.state.token:
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
            "settings": settings_mod.public_view(st),
            "templates": [t.to_json() for t in templates.load_all(data_dir)],
            "lives": [_summary(data_dir, l) for l in Life.list_all(data_dir / "humans")],
            "sessions": [s.to_json() for s in sessions.list_all(data_dir)],
            "memTypes": list(MEM_TYPES),
        }

    # ─────────────────────── 设置与平台 ───────────────────────

    @app.get("/api/settings")
    def get_settings(request: Request):
        return settings_mod.public_view(_settings(request))

    @app.post("/api/settings")
    def post_settings(request: Request, payload: dict = Body(...)):
        """保存设置。**没传的字段一律不动。**

        这里以前是 `payload.get("providers") or []`——前端在设置页只改「高级设置」
        再保存时，整个平台列表（含密钥）就被清空了，之后所有席位都"找不到平台"。
        局部更新是常态（设置页是分区的），所以按字段判断有没有传。
        """
        data_dir = _data_dir(request)
        old = _settings(request)
        providers = old["providers"]
        if "providers" in payload:
            providers = settings_mod.merge_providers(old["providers"], payload.get("providers") or [])
        merged = {
            "providers": providers,
            "advanced": {**old["advanced"], **(payload.get("advanced") or {})},
            "modelInfo": {**old.get("modelInfo", {}), **(payload.get("modelInfo") or {})},
        }
        saved = settings_mod.save(data_dir, merged)
        request.app.state.settings_cache = saved
        return settings_mod.public_view(saved)

    @app.post("/api/providers/{provider_id}/models")
    def fetch_models(request: Request, provider_id: str):
        st = _settings(request)
        prov = settings_mod.provider(st, provider_id)
        if prov is None:
            raise HTTPException(404, f"没有这家平台：{provider_id}")
        got = list_models(prov, transport=request.app.state.transport)
        return {"models": got}

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
        return [t.to_json() for t in templates.load_all(_data_dir(request))]

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
        return tpl.to_json()

    @app.patch("/api/templates/{template_id}")
    def update_template(request: Request, template_id: str, payload: dict = Body(...)):
        data_dir = _data_dir(request)
        old = templates.load(data_dir, template_id)
        if old is None:
            raise HTTPException(404, f"没有这个模板：{template_id}")
        name = (payload.get("name") or old.name).strip()
        system = payload.get("system", old.system)
        new = templates.make(
            name, system,
            emoji=payload.get("emoji", old.emoji),
            tagline=payload.get("tagline", old.tagline),
            tags=list(payload.get("tags", old.tags)),
            bare=bool(payload.get("bare", old.bare)),
            builtin=old.builtin,
            origin=old.origin or "user",
        )
        if new.id != old.id:
            # 改了名字或正文 → id 变了（id 由内容算出来）。**把老文件删掉**，
            # 否则库里会同时留着新旧两份，看起来像多了一个模板。
            templates.template_path(data_dir, old.id).unlink(missing_ok=True)
        templates.save(data_dir, new)
        _retarget_lives(data_dir, old.id, new.id)
        return new.to_json()

    @app.delete("/api/templates/{template_id}")
    def delete_template(request: Request, template_id: str):
        data_dir = _data_dir(request)
        users = [l for l in Life.list_all(data_dir / "humans")
                 if l.identity.get("templateId") == template_id]
        if users:
            names = "、".join(l.identity.get("name") or l.id for l in users[:3])
            raise HTTPException(400, f"还有 {len(users)} 位数字人用着这个模板（{names}），"
                                     f"先把他们的模板换掉或把他们删掉")
        if not templates.template_path(data_dir, template_id).exists():
            raise HTTPException(404, f"没有这个模板：{template_id}")
        templates.template_path(data_dir, template_id).unlink()
        return {"ok": True}

    # ─────────────────────── 数字人（生命）───────────────────────

    @app.get("/api/lives")
    def list_lives(request: Request):
        data_dir = _data_dir(request)
        return [_summary(data_dir, l) for l in Life.list_all(data_dir / "humans")]

    @app.post("/api/lives")
    def create_life(request: Request, payload: dict = Body(...)):
        data_dir = _data_dir(request)
        tpl = templates.load(data_dir, payload.get("templateId") or "")
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
            name=(payload.get("name") or tpl.name).strip(),
            emoji=payload.get("emoji") or tpl.emoji,
        )
        return _summary(data_dir, life)

    @app.get("/api/lives/{life_id}")
    def get_life(request: Request, life_id: str):
        data_dir = _data_dir(request)
        life = _life(request, life_id)
        tpl = templates.load(data_dir, life.identity.get("templateId") or "")
        return {
            "identity": life.identity,
            "template": tpl.to_json() if tpl else None,
            "summary": _summary(data_dir, life),
            "sessions": [{"id": sid, "compaction": life.compaction(sid)}
                         for sid in life.sessions()],
            "memories": [m.__dict__ for m in life.memories()],
        }

    @app.patch("/api/lives/{life_id}")
    def update_life(request: Request, life_id: str, payload: dict = Body(...)):
        data_dir = _data_dir(request)
        life = _life(request, life_id)
        for field in ("name", "emoji", "providerId", "model", "templateId"):
            if field in payload and payload[field] is not None:
                life.identity[field] = payload[field]
        store.write_json(life.root / "identity.json", life.identity)
        return _summary(data_dir, life)

    @app.delete("/api/lives/{life_id}")
    def delete_life(request: Request, life_id: str):
        """删一位数字人。**会把他的记忆一起删掉**——所以前端一定要二次确认，
        别做成"点一下就没了"。"""
        import shutil

        life = _life(request, life_id)
        shutil.rmtree(life.root)
        return {"ok": True}

    # ─────────────────────── 记忆 ───────────────────────

    @app.get("/api/lives/{life_id}/memory")
    def list_memory(request: Request, life_id: str):
        life = _life(request, life_id)
        return {
            "index": life.index_text(),
            "memories": [m.__dict__ for m in life.memories()],
            "types": list(MEM_TYPES),
        }

    @app.post("/api/lives/{life_id}/memory")
    def add_memory(request: Request, life_id: str, payload: dict = Body(...)):
        life = _life(request, life_id)
        name = (payload.get("name") or "").strip()
        text = (payload.get("text") or "").strip()
        if not name or not text:
            raise HTTPException(400, "记忆要有标题和正文")
        m = life.remember(
            name=name, text=text, desc=(payload.get("desc") or "").strip(),
            type=payload.get("type") if payload.get("type") in MEM_TYPES else "user",
            scope=payload.get("scope") if payload.get("scope") in ("persistent", "session") else "persistent",
            source=payload.get("source") or "手动",
        )
        return m.__dict__

    @app.patch("/api/lives/{life_id}/memory/{mid}")
    def edit_memory(request: Request, life_id: str, mid: str, payload: dict = Body(...)):
        life = _life(request, life_id)
        old = next((m for m in life.memories() if m.id == mid), None)
        if old is None:
            raise HTTPException(404, f"没有这条记忆：{mid}")
        m = life.remember(
            name=(payload.get("name") or old.name).strip(),
            text=(payload.get("text") or old.text),
            desc=payload.get("desc", old.desc),
            type=payload.get("type") if payload.get("type") in MEM_TYPES else old.type,
            scope=payload.get("scope") if payload.get("scope") in ("persistent", "session") else old.scope,
            source=old.source, mid=mid,
        )
        return m.__dict__

    @app.delete("/api/lives/{life_id}/memory/{mid}")
    def delete_memory(request: Request, life_id: str, mid: str):
        life = _life(request, life_id)
        if not life.forget(mid):
            raise HTTPException(404, f"没有这条记忆：{mid}")
        return {"ok": True}

    # ─────────────────────── 会话记录与"一生" ───────────────────────

    @app.get("/api/lives/{life_id}/sessions/{session_id}")
    def read_session(request: Request, life_id: str, session_id: str, tail: int = 0):
        life = _life(request, life_id)
        entries = life.read_session(session_id, only_tail=bool(tail))
        return {
            "sessionId": session_id,
            "compaction": life.compaction(session_id),
            "entries": entries,
            "all": 0 if tail else 1,
        }

    @app.get("/api/lives/{life_id}/timeline")
    def timeline(request: Request, life_id: str):
        """「一生」：按天聚合——哪天聊过什么、那天记住了什么。

        只读，但每一条都能跳回那次会话。数据全部从记录和记忆里算出来，
        不额外存一份（**原文是真相，时间线是派生物**）。
        """
        from .text import fmt_day

        life = _life(request, life_id)
        days: dict[str, dict] = {}
        for sid in life.sessions():
            for e in life.read_session(sid, warn=False):
                day = fmt_day(e.get("t") or 0) if e.get("t") else "—"
                slot = days.setdefault(day, {"day": day, "talked": 0, "sessions": {}, "memories": []})
                slot["talked"] += 1
                slot["sessions"][sid] = slot["sessions"].get(sid, 0) + 1
        for m in life.memories():
            day = fmt_day(m.at) if m.at else "—"
            slot = days.setdefault(day, {"day": day, "talked": 0, "sessions": {}, "memories": []})
            slot["memories"].append({"id": m.id, "name": m.name, "desc": m.desc, "type": m.type})
        for slot in days.values():
            slot["sessions"] = [{"id": k, "entries": v} for k, v in slot["sessions"].items()]
        return {"days": [days[k] for k in sorted(days, reverse=True)]}

    # ─────────────────────── 桌子 ───────────────────────

    @app.get("/api/sessions")
    def list_sessions(request: Request):
        return [s.to_json() for s in sessions.list_all(_data_dir(request))]

    @app.post("/api/sessions")
    def create_session(request: Request, payload: dict = Body(...)):
        data_dir = _data_dir(request)
        sid = payload.get("id")
        if sid and sessions.load(data_dir, sid):
            raise HTTPException(400, f"这个会话 id 已经存在：{sid}")
        s = sessions.create(
            data_dir,
            title=payload.get("title") or "新的圆桌",
            mode=payload.get("mode") or "parallel",
            seats=list(payload.get("seats") or []),
            session_id=sid,
        )
        return s.to_json()

    @app.patch("/api/sessions/{session_id}")
    def update_session(request: Request, session_id: str, payload: dict = Body(...)):
        data_dir = _data_dir(request)
        s = sessions.load(data_dir, session_id)
        if s is None:
            raise HTTPException(404, f"没有这张桌子：{session_id}")
        if payload.get("title") is not None:
            s.title = str(payload["title"])
        if payload.get("mode") in sessions.MODES:
            s.mode = payload["mode"]
        if payload.get("seats") is not None:
            s.seats = sessions.normalize_seats(list(payload["seats"]))
        sessions.save(data_dir, s)
        return s.to_json()

    @app.delete("/api/sessions/{session_id}")
    def delete_session(request: Request, session_id: str):
        if not sessions.delete(_data_dir(request), session_id):
            raise HTTPException(404, f"没有这张桌子：{session_id}")
        return {"ok": True}

    # ─────────────────────── 问一句（核心）───────────────────────

    @app.post("/api/ask")
    def ask(request: Request, payload: dict = Body(...)):
        """流式回答。返回 NDJSON：一行一个事件，前端读一行贴一行。

        用 NDJSON 而不是 WebSocket：单向、能穿代理、断了就是断了，没有心跳要维护。
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
                    only_seat=payload.get("seatNo"),
                ):
                    yield (json.dumps(evt, ensure_ascii=False) + "\n").encode("utf-8")
            except AdapterError as e:
                yield (json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False) + "\n").encode("utf-8")
            except Exception as e:  # noqa: BLE001
                yield (json.dumps({"type": "error", "message": f"服务端出错：{type(e).__name__}：{e}"},
                                  ensure_ascii=False) + "\n").encode("utf-8")

        return StreamingResponse(stream(), media_type="application/x-ndjson")

    # ─────────────────────── 手动整理（压缩 + 提取记忆）───────────────────────

    @app.post("/api/lives/{life_id}/sessions/{session_id}/compact")
    def compact(request: Request, life_id: str, session_id: str, payload: dict | None = Body(default=None)):
        """手动让某位生命整理一次。

        平时这件事在后台自动跑（两轮之间）。这个接口是给"我想现在就看到"
        和"上一次没压好、想重压"用的。
        """
        life = _life(request, life_id)
        st = _settings(request)
        provider = settings_mod.provider(st, life.identity.get("providerId"))
        if provider is None:
            raise HTTPException(400, "他绑的那家平台不在了，先给他换一家")
        model = life.identity.get("model") or ""
        window = settings_mod.resolve_window(st, model, provider.get("id", ""))
        _, cap = settings_mod.resolve_output_cap(st, model, window)

        from .adapters import make_caller

        caller = make_caller(provider, model=model, transport=request.app.state.transport,
                             max_tokens=min(cap, 8192))
        budget = max(0, window.tokens - cap - max(1024, round(window.tokens * 0.1)))
        meta = memory.compress(life, session_id, caller=caller, model=model, budget=budget)
        res = memory.extract(life, session_id, caller=caller, model=model)
        return {
            "compaction": meta,
            "extracted": {"written": [m.__dict__ for m in res.written],
                          "skipped": res.skipped_duplicate, "rejected": res.rejected},
            "lines": res.lines(),
        }

    # ─────────────────────── 前端 ───────────────────────

    @app.get("/")
    def index(request: Request):
        return HTMLResponse(_serve_index(request.app.state.token), headers=NO_STORE)

    @app.get("/{path:path}")
    def static_files(request: Request, path: str):
        target = (WEB_DIR / path).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())):
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
        cache = "public, max-age=31536000, immutable" if "/assets/" in str(target) else "no-store"
        return StreamingResponse(iter([target.read_bytes()]), media_type=ctype,
                                 headers={"cache-control": cache})

    return app


def _serve_index(token: str) -> str:
    index = WEB_DIR / "index.html"
    if not index.exists():
        return (
            "<!doctype html><meta charset='utf-8'><title>圆桌</title>"
            "<body style='font-family:system-ui;padding:2rem;line-height:1.7'>"
            "<h1>圆桌后端已经起来了</h1>"
            "<p>前端还没构建（<code>web/dist/index.html</code> 不存在）。</p>"
            "<p>可以先试：<code>/api/health</code>、<code>/api/bootstrap</code>。</p>"
            f"<script>window.__RT_BOOT__={{apiVersion:{API_VERSION},token:{json.dumps(token)}}}</script>"
        )
    html = index.read_text(encoding="utf-8")
    boot = json.dumps({"apiVersion": API_VERSION, "token": token, "dataVersion": presets.DATA_VERSION})
    # 令牌只能在服务端注入——它每次启动都不一样，不可能打进前端产物里
    return html.replace(BOOT_PLACEHOLDER, boot)


def _retarget_lives(data_dir: Path, old_tid: str, new_tid: str) -> int:
    """模板改了内容之后 id 会变，用着它的生命要跟着指过去。返回改了几位。"""
    n = 0
    for life in Life.list_all(data_dir / "humans"):
        if life.identity.get("templateId") == old_tid:
            life.identity["templateId"] = new_tid
            store.write_json(life.root / "identity.json", life.identity)
            n += 1
    return n


app = create_app()
