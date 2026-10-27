"""FastAPI app serving the transit chat UI and streaming Claude's answers.

Safe to expose publicly when ``APP_ENV=production``: per-visitor rate limits, a
site-wide daily cap, bounded conversations, generic error messages, and a minimal
health endpoint (see :mod:`transit_mcp.chat.guard`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from anthropic import AsyncAnthropic
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from transit_mcp.chat.agent import INTERNAL_EVENTS, run_chat
from transit_mcp.chat.guard import (
    MESSAGES,
    DailyCap,
    RateLimiter,
    SessionStore,
    trim_history,
)
from transit_mcp.chat.mcp_bridge import McpBridge
from transit_mcp.chat.reports_api import process_reports
from transit_mcp.chat.reports_api import router as reports_router
from transit_mcp.chat.trip_api import agency_info
from transit_mcp.chat.trip_api import router as trip_router
from transit_mcp.chatlog import ChatLog, TurnRecord, device_class
from transit_mcp.config import get_settings
from transit_mcp.reports import ReportStore

STATIC_DIR = Path(__file__).parent / "static"
# Browsers only run module scripts served as JavaScript; Windows' registry may not
# know .mjs (MapLibre ships as .mjs).
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/x-protobuf", ".pbf")
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
    app.state.chatlog = None
    app.state.trips = None  # created on first trip/geocode request (chat/trip_api.py)
    if settings.chatlog_enabled:
        try:
            app.state.chatlog = ChatLog.from_settings(settings)
            app.state.chatlog.prune(settings.chatlog_retention_days)
        except Exception:  # noqa: BLE001 - the site works without its log
            log.exception("chat log unavailable")

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

    app.state.reports = None
    app.state.report_limiter = RateLimiter(settings.reports_rate_per_minute,
                                           settings.reports_rate_per_day)
    app.state.report_cap = DailyCap(settings.reports_daily_limit, settings.timezone)
    if settings.reports_enabled:
        try:
            app.state.reports = ReportStore.from_settings(settings)
            app.state.report_agencies = {s: a["name"] for s, a in agency_info(settings).items()}
            asyncio.get_running_loop().create_task(process_reports(app))  # any left waiting
        except Exception:  # noqa: BLE001 - the site works without rider reports
            log.exception("rider reports unavailable")
            app.state.reports = None
    try:
        yield
    finally:
        await app.state.bridge.stop()
        if app.state.anthropic is not None:
            await app.state.anthropic.close()
        if app.state.chatlog is not None:
            app.state.chatlog.close()
        if getattr(app.state, "trips", None) is not None:
            app.state.trips.close()


app = FastAPI(title="DMV Regional Transportation Assistant", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.include_router(trip_router)
app.include_router(reports_router)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    return FileResponse(STATIC_DIR / "brand" / "favicon.ico",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.api_route("/tiles/dmv.pmtiles", methods=["GET", "HEAD"])
async def basemap(request: Request):
    """The self-hosted basemap (one PMTiles file; the browser reads byte ranges)."""
    path = Path(request.app.state.settings.map_tiles_path)
    if not path.is_file():
        return JSONResponse({"error": "basemap not installed"}, status_code=404)
    return FileResponse(path, media_type="application/octet-stream",
                        headers={"Cache-Control": "public, max-age=86400"})


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

    chatlog: ChatLog | None = st.chatlog
    client_ip = request.client.host if request.client else "unknown"
    started = time.monotonic()

    body = await _read_json(request)
    session_id = str((body or {}).get("session_id") or "default")[:64]
    user_message = str((body or {}).get("message") or "").strip()
    rec = TurnRecord(
        conversation_id=session_id,
        visitor_id=chatlog.visitor_id(client_ip) if chatlog else "",
        question=user_message[: settings.chat_max_message_chars],
        device=device_class(request.headers.get("user-agent")),
        model=settings.anthropic_model,
    )

    def refuse(outcome: str, message: str) -> StreamingResponse:
        """Answer with a plain message, and log the question as refused."""
        if chatlog is not None and outcome != "empty":
            rec.outcome = outcome
            rec.latency_ms = int((time.monotonic() - started) * 1000)
            chatlog.safe_record(rec)
        return _message_only(message)

    too_long = MESSAGES["too_long"].format(limit=settings.chat_max_message_chars)
    if body is None:
        return refuse("too_long", too_long)
    if st.anthropic is None:
        return refuse("unavailable", MESSAGES["unavailable"] if prod else
                      "ANTHROPIC_API_KEY is not set. Add it to .env and restart.")
    if st.mcp_error is not None:
        return refuse("unavailable", MESSAGES["unavailable"] if prod else
                      f"MCP server did not start: {st.mcp_error}. "
                      "Run `transit-mcp ingest all` first.")
    if not user_message:
        return refuse("empty", MESSAGES["empty"])
    if len(user_message) > settings.chat_max_message_chars:
        return refuse("too_long", too_long)

    limited = st.limiter.check(client_ip)
    if limited:
        log.info("rate limit (%s) for visitor %s", limited, rec.visitor_id or client_ip)
        return refuse(limited, MESSAGES[limited])

    session = st.sessions.get(session_id)
    if session.lock.locked():
        return refuse("busy_session", MESSAGES["busy_session"])
    if settings.chat_max_turns and session.turns >= settings.chat_max_turns:
        return refuse("max_turns", MESSAGES["max_turns"])
    if not st.daily_cap.try_take():
        log.warning("site-wide daily chat limit reached (%s)", st.daily_cap.limit)
        return refuse("site_busy", MESSAGES["site_busy"])

    def note(event: dict) -> None:
        """Collect what the log needs from each event of the answer."""
        kind = event.get("type")
        if kind == "text":
            if rec.first_text_ms is None:
                rec.first_text_ms = int((time.monotonic() - started) * 1000)
            rec.answer += event.get("text", "")
        elif kind == "tool_call":
            rec.tools.append({"name": event.get("name"), "input": event.get("input")})
        elif kind == "tool_result" and rec.tools:
            rec.tools[-1].update(is_error=bool(event.get("is_error")),
                                 duration_ms=event.get("duration_ms"))
        elif kind == "usage":
            rec.add_usage(event)
            rec.model = event.get("model") or rec.model
        elif kind == "nudged":
            rec.nudged = True
        elif kind == "error":
            rec.outcome, rec.error = "error", str(event.get("message"))[:2000]

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
                    note(event)
                    if event.get("type") in INTERNAL_EVENTS:
                        continue
                    if event.get("type") == "error":
                        failed = True
                        log.error("chat turn failed: %s", event.get("message"))
                        if prod:
                            event = {"type": "error", "message": MESSAGES["failed"]}
                    yield _sse(event)
            except Exception as exc:  # noqa: BLE001
                failed = True
                log.exception("chat turn crashed")
                rec.outcome, rec.error = "error", f"{type(exc).__name__}: {exc}"[:2000]
                yield _sse({"type": "error", "message": MESSAGES["failed"] if prod
                            else f"{type(exc).__name__}: {exc}"})
            finally:
                if failed:
                    # Never leave a half-finished turn (e.g. a tool call without its
                    # result) in the history — it would break every later turn.
                    session.messages[:] = snapshot
                else:
                    session.turns += 1
                if chatlog is not None:
                    rec.latency_ms = int((time.monotonic() - started) * 1000)
                    chatlog.safe_record(rec)
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
        # Per-request lines would record visitors' IP addresses; Caddy's access log
        # (with addresses masked) covers requests instead.
        access_log=False,
        proxy_headers=settings.trust_proxy,
        forwarded_allow_ips="*" if settings.trust_proxy else None,
    )


if __name__ == "__main__":
    serve()
