"""FastAPI app serving the transit chat UI and streaming Claude's answers.

Safe to expose publicly when ``APP_ENV=production``: per-visitor rate limits, a
site-wide daily cap, bounded conversations, generic error messages, and a minimal
health endpoint (see :mod:`transit_mcp.chat.guard`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from anthropic import AsyncAnthropic
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from transit_mcp.chat.agent import run_chat
from transit_mcp.chat.guard import (
    MESSAGES,
    DailyCap,
    RateLimiter,
    SessionStore,
    trim_history,
)
from transit_mcp.chat.mcp_bridge import McpBridge
from transit_mcp.config import get_settings

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY_BYTES = 8 * 1024
log = logging.getLogger("transit_mcp.chat")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.settings = settings
    app.state.sessions = SessionStore(
        ttl_seconds=settings.chat_session_ttl_minutes * 60,
        max_sessions=settings.chat_max_sessions,
    )
    app.state.limiter = RateLimiter(settings.chat_rate_per_minute, settings.chat_rate_per_day)
    app.state.daily_cap = DailyCap(settings.chat_daily_limit, settings.timezone)

    app.state.bridge = McpBridge()
    try:
        await app.state.bridge.start()
        app.state.mcp_error = None
    except Exception as exc:  # noqa: BLE001
        log.exception("MCP server failed to start")
        app.state.mcp_error = f"{type(exc).__name__}: {exc}"

    app.state.anthropic = (
        AsyncAnthropic(api_key=settings.anthropic_api_key)
        if settings.anthropic_api_key
        else None
    )
    try:
        yield
    finally:
        await app.state.bridge.stop()
        if app.state.anthropic is not None:
            await app.state.anthropic.close()


app = FastAPI(title="DMV Regional Transportation Assistant", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
async def health(request: Request) -> JSONResponse:
    st = request.app.state
    ok = st.anthropic is not None and st.mcp_error is None
    if st.settings.is_production:
        return JSONResponse({"ok": ok}, status_code=200 if ok else 503)
    return JSONResponse(
        {
            "ok": ok,
            "model": st.settings.anthropic_model,
            "anthropic_key_present": st.anthropic is not None,
            "mcp_ready": st.mcp_error is None,
            "mcp_error": st.mcp_error,
            "tools": [t["name"] for t in st.bridge.anthropic_tools],
            "planner_engine": st.settings.planner_engine,
        }
    )


async def _read_json(request: Request) -> dict | None:
    try:
        if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
            return None
        body = await request.body()
        if len(body) > MAX_BODY_BYTES:
            return None
        data = json.loads(body or b"{}")
        return data if isinstance(data, dict) else None
    except (ValueError, TypeError):
        return None


@app.post("/api/reset")
async def reset(request: Request) -> JSONResponse:
    body = await _read_json(request) or {}
    request.app.state.sessions.drop(str(body.get("session_id") or ""))
    return JSONResponse({"ok": True})


def _sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"


def _stream(events: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        events,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _message_only(text: str) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        yield _sse({"type": "error", "message": text})
        yield _sse({"type": "done"})

    return _stream(gen())


@app.post("/api/chat")
async def chat(request: Request) -> StreamingResponse:
    st = request.app.state
    settings = st.settings
    prod = settings.is_production

    body = await _read_json(request)
    if body is None:
        return _message_only(
            MESSAGES["too_long"].format(limit=settings.chat_max_message_chars)
        )
    session_id = str(body.get("session_id") or "default")[:64]
    user_message = str(body.get("message") or "").strip()

    if st.anthropic is None:
        return _message_only(MESSAGES["unavailable"] if prod else
                             "ANTHROPIC_API_KEY is not set. Add it to .env and restart.")
    if st.mcp_error is not None:
        return _message_only(MESSAGES["unavailable"] if prod else
                             f"MCP server did not start: {st.mcp_error}. "
                             "Run `transit-mcp ingest all` first.")
    if not user_message:
        return _message_only(MESSAGES["empty"])
    if len(user_message) > settings.chat_max_message_chars:
        return _message_only(
            MESSAGES["too_long"].format(limit=settings.chat_max_message_chars)
        )

    client_ip = request.client.host if request.client else "unknown"
    limited = st.limiter.check(client_ip)
    if limited:
        log.info("rate limit (%s) for %s", limited, client_ip)
        return _message_only(MESSAGES[limited])

    session = st.sessions.get(session_id)
    if session.lock.locked():
        return _message_only(MESSAGES["busy_session"])
    if settings.chat_max_turns and session.turns >= settings.chat_max_turns:
        return _message_only(MESSAGES["max_turns"])
    if not st.daily_cap.try_take():
        log.warning("site-wide daily chat limit reached (%s)", st.daily_cap.limit)
        return _message_only(MESSAGES["site_busy"])

    async def event_stream() -> AsyncIterator[str]:
        async with session.lock:
            snapshot = list(session.messages)
            session.messages.append({"role": "user", "content": user_message})
            session.messages[:] = trim_history(session.messages, settings.chat_history_turns)
            failed = False
            try:
                async for event in run_chat(
                    client=st.anthropic,
                    bridge=st.bridge,
                    model=settings.anthropic_model,
                    messages=session.messages,
                    max_tokens=settings.chat_max_tokens,
                    max_iterations=settings.chat_max_tool_iterations,
                ):
                    if event.get("type") == "error":
                        failed = True
                        log.error("chat turn failed: %s", event.get("message"))
                        if prod:
                            event = {"type": "error", "message": MESSAGES["failed"]}
                    yield _sse(event)
            except Exception as exc:  # noqa: BLE001
                failed = True
                log.exception("chat turn crashed")
                yield _sse({"type": "error", "message": MESSAGES["failed"] if prod
                            else f"{type(exc).__name__}: {exc}"})
            finally:
                if failed:
                    # Never leave a half-finished turn (e.g. a tool call without its
                    # result) in the history — it would break every later turn.
                    session.messages[:] = snapshot
                else:
                    session.turns += 1
            yield _sse({"type": "done"})

    return _stream(event_stream())


def _open_when_ready(url: str, timeout_s: float = 60.0) -> None:
    """Open the browser once the server answers /api/health (background thread)."""
    import threading
    import time
    import webbrowser

    import httpx

    def wait_then_open():
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{url}/api/health", timeout=2).status_code < 500:
                    webbrowser.open(url)
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.5)

    threading.Thread(target=wait_then_open, daemon=True).start()


def serve(open_browser: bool = False) -> None:
    import uvicorn

    settings = get_settings()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    url = f"http://{settings.chat_host}:{settings.chat_port}"
    if open_browser:
        _open_when_ready(url)
    uvicorn.run(
        "transit_mcp.chat.app:app",
        host=settings.chat_host,
        port=settings.chat_port,
        log_level="info" if settings.is_production else "warning",
        proxy_headers=settings.trust_proxy,
        forwarded_allow_ips="*" if settings.trust_proxy else None,
    )


if __name__ == "__main__":
    serve()
