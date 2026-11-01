"""HTTP/WebSocket server: operator + speaker UI, command API, audio ingest."""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from subtalk.config import Config, audio_token
from subtalk.engine import Engine
from subtalk.server.auth import ControlLease, LoginLimiter, PasscodeStore, SessionManager
from subtalk.state import Role

log = logging.getLogger("subtalk.server")
WEB = Path(__file__).resolve().parent.parent / "web"


def _origin_ok(headers, allowed: list[str] | tuple = ()) -> bool:
    """Same-origin check for state-changing requests (CSRF / cross-site WebSocket)."""
    origin = headers.get("origin")
    if not origin:
        return True
    netloc = urlsplit(origin).netloc
    hosts = {headers.get("host"), headers.get("x-forwarded-host")}
    return netloc in hosts or netloc in allowed or urlsplit(origin).hostname in allowed


def _is_https(request_or_ws) -> bool:
    proto = request_or_ws.headers.get("x-forwarded-proto") or request_or_ws.url.scheme
    return proto in ("https", "wss")


def create_app(cfg: Config, engine: Engine | None = None, *, start_engine: bool = True) -> FastAPI:
    engine = engine or Engine(cfg)
    sessions = SessionManager(cfg.server.session_hours)
    passcodes = PasscodeStore(cfg.path(cfg.paths.secrets_dir) / "auth.json", allow_dev_defaults=cfg.mode == "mock")
    lease = ControlLease(sessions, cfg.server.lease_grace_s)
    limiter = LoginLimiter()

    @asynccontextmanager
    async def lifespan(app):
        if start_engine:
            try:
                await engine.start()
            except Exception:  # noqa: BLE001 - keep serving the dashboard so the operator sees the fault
                log.exception("engine start failed")
        yield
        await engine.stop()

    app = FastAPI(title="SubTalk", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine, app.state.sessions, app.state.lease, app.state.passcodes = engine, sessions, lease, passcodes
    if engine.mock_zoom:
        app.include_router(engine.mock_zoom.router())
    app.mount("/static", StaticFiles(directory=WEB), name="static")

    def session_of(conn):
        return sessions.verify(conn.cookies.get(SessionManager.COOKIE))

    def client_snapshot(session) -> dict:
        snap = engine.snapshot(session.role)
        snap["boot_id"] = sessions.boot_id
        info = lease.info()
        snap["you"] = {"role": session.role.value, "name": session.name,
                       "has_control": info["holder_sid"] == session.sid}
        snap["lease"] = {"holder": info["holder"], "since": info["since"]}
        snap["dev_passcodes"] = passcodes.dev_defaults
        return snap

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if request.method == "POST" and not _origin_ok(request.headers, cfg.server.allowed_origins):
            return JSONResponse({"error": "bad origin"}, status_code=403)
        resp = await call_next(request)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.get("/")
    async def root():
        return RedirectResponse("/operator")

    @app.get("/operator")
    async def operator_page():
        return FileResponse(WEB / "operator.html")

    @app.get("/speaker")
    async def speaker_page():
        return FileResponse(WEB / "speaker.html")

    @app.get("/healthz")
    async def healthz():
        return {"ok": engine.state.value in ("READY", "RUNNING", "DEGRADED"), "engine": engine.state.value,
                "output": engine.output.state.value, "boot_id": sessions.boot_id}

    @app.post("/api/login")
    async def login(request: Request):
        ip = request.client.host if request.client else "?"
        if not limiter.allow(ip):
            return JSONResponse({"error": "Too many attempts - wait a few minutes"}, status_code=429)
        body = await request.json()
        try:
            role = Role(body.get("role", "operator"))
        except ValueError:
            return JSONResponse({"error": "Unknown role"}, status_code=400)
        if not passcodes.configured(role.value):
            return JSONResponse({"error": f"No passcode set for {role.value} (run: subtalk set-passcode --role {role.value})"},
                                status_code=503)
        if not passcodes.check(role.value, str(body.get("passcode", ""))):
            limiter.fail(ip)
            await asyncio.sleep(0.5)
            return JSONResponse({"error": "Wrong passcode"}, status_code=401)
        s, token = sessions.create(role, str(body.get("name") or role.value))
        resp = JSONResponse({"role": role.value, "name": s.name, "boot_id": sessions.boot_id})
        resp.set_cookie(SessionManager.COOKIE, token, httponly=True, samesite="strict", secure=_is_https(request),
                        max_age=int(cfg.server.session_hours * 3600), path="/")
        log.info("login", extra={"role": role.value, "who": s.name, "ip": ip})
        return resp

    @app.post("/api/logout")
    async def logout(request: Request):
        s = session_of(request)
        if s:
            sessions.revoke(s.sid)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SessionManager.COOKIE, path="/")
        return resp

    @app.get("/api/session")
    async def session_info(request: Request):
        s = session_of(request)
        if not s:
            return JSONResponse({"error": "not signed in", "boot_id": sessions.boot_id,
                                 "dev_passcodes": passcodes.dev_defaults}, status_code=401)
        return {"role": s.role.value, "name": s.name, "boot_id": sessions.boot_id}

    @app.get("/api/state")
    async def state(request: Request):
        s = session_of(request)
        if not s:
            return JSONResponse({"error": "not signed in"}, status_code=401)
        return client_snapshot(s)

    @app.post("/api/command")
    async def command(request: Request):
        s = session_of(request)
        if not s:
            return JSONResponse({"error": "not signed in", "accepted": False}, status_code=401)
        body = await request.json()
        result = await engine.execute(s, lease, body)
        return {"command_id": result.command_id, "accepted": result.accepted, "error": result.error,
                "version": result.version, "data": result.data}

    @app.websocket("/ws/ui")
    async def ws_ui(ws: WebSocket):
        if not _origin_ok(ws.headers, cfg.server.allowed_origins):
            await ws.close(code=4403)
            return
        s = session_of(ws)
        if not s:
            await ws.close(code=4401)
            return
        await ws.accept()
        s.connections += 1
        last_body, last_sent = None, 0.0

        async def reader():
            while True:
                await ws.receive_text()  # client pings; disconnect raises

        rtask = asyncio.create_task(reader())
        try:
            while not rtask.done():
                snap = client_snapshot(s)
                body = json.dumps({k: v for k, v in snap.items() if k != "server_time"}, default=str, sort_keys=True)
                now = time.monotonic()
                if body != last_body or now - last_sent > 2.0:
                    await ws.send_text(json.dumps(snap, default=str))
                    last_body, last_sent = body, now
                await asyncio.sleep(0.25)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            rtask.cancel()
            s.connections = max(0, s.connections - 1)
            s.disconnected_at = time.monotonic()

    @app.websocket("/ws/audio")
    async def ws_audio(ws: WebSocket):
        expected = audio_token()
        given = ws.headers.get("x-subtalk-token", "")
        local = ws.client is not None and ws.client.host in ("127.0.0.1", "::1")
        if expected:
            if not hmac.compare_digest(given, expected):
                await ws.close(code=4401)
                return
        elif not (cfg.mode == "mock" or local):
            engine.alert("critical", "audio_no_token", "Audio sender rejected: set SUBTALK_AUDIO_TOKEN on both computers.")
            await ws.close(code=4401)
            return
        await ws.accept()
        if engine.ingest.connected:
            engine.alert("warn", "audio_second_sender", "A second audio sender connected; it replaced the first.")
        engine.ingest.on_connect({"device": ws.headers.get("x-subtalk-device", "?"),
                                  "ip": ws.client.host if ws.client else "?"})
        engine.mark_dirty()
        try:
            while True:
                data = await ws.receive_bytes()
                engine.ingest.handle(data)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            engine.ingest.on_disconnect()
            engine.alert("warn", "audio_disconnected", "Audio sender disconnected.")

    return app
