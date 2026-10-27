"""Rider reports from the map page (see :mod:`transit_mcp.reports`).

    POST /api/reports         a rider's report -> saved, then checked in the background
    GET  /api/reports/recent  the latest published reports about today

Each visitor may send a few reports a minute / a day, and the site as a whole a set
number a day (each report costs one small Claude call).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from transit_mcp.reports import ReportInput, process_pending

log = logging.getLogger("transit_mcp.reports")
router = APIRouter()
_parse_lock = asyncio.Lock()


class ReportBody(BaseModel):
    station: str | None = Field(default=None, max_length=120)
    line: str | None = Field(default=None, max_length=120)
    origin: str = Field(min_length=2, max_length=120)
    destination: str = Field(min_length=2, max_length=120)
    travel_date: date
    text: str = Field(min_length=10, max_length=2000)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def process_reports(app: FastAPI) -> None:
    """Check waiting reports with Claude (one batch at a time)."""
    st = app.state
    if getattr(st, "reports", None) is None or st.anthropic is None:
        return
    async with _parse_lock:
        try:
            await process_pending(st.reports, st.anthropic, st.settings.reports_model,
                                  st.report_agencies)
        except Exception:  # noqa: BLE001 - reports stay pending and are retried
            log.exception("checking rider reports failed")


@router.post("/api/reports")
async def submit_report(request: Request, body: ReportBody,
                        background: BackgroundTasks) -> JSONResponse:
    st = request.app.state
    if getattr(st, "reports", None) is None:
        return JSONResponse({"ok": False, "message": "Rider reports aren't available right "
                             "now."}, status_code=503)
    if st.report_limiter.check(_ip(request)):
        return JSONResponse({"ok": False, "message": "Thanks! You've sent several reports "
                             "already. Please try again a little later."}, status_code=429)
    today = datetime.now(ZoneInfo(st.settings.timezone)).date()
    if body.travel_date > today or body.travel_date < today - timedelta(days=60):
        return JSONResponse({"ok": False, "message": "Please pick a travel date from the "
                             "last two months."}, status_code=422)
    if not st.report_cap.try_take():
        return JSONResponse({"ok": False, "message": "We've received a lot of reports "
                             "today. Please try again tomorrow."}, status_code=429)
    chatlog = getattr(st, "chatlog", None)
    visitor = chatlog.visitor_id(_ip(request)) if chatlog is not None else "anonymous"
    try:
        report_id = await asyncio.to_thread(st.reports.add, visitor, ReportInput(
            travel_date=body.travel_date, station=body.station, line=body.line,
            origin=body.origin, destination=body.destination, text=body.text))
    except Exception:  # noqa: BLE001 - e.g. the database is busy with a data load
        log.exception("saving a rider report failed")
        return JSONResponse({"ok": False, "message": "Sorry, your report couldn't be saved "
                             "just now. Please try again in a minute."}, status_code=503)
    background.add_task(process_reports, request.app)
    return JSONResponse({"ok": True, "id": report_id,
                         "message": "Thank you! Your report will help other riders."},
                        status_code=202)


@router.get("/api/reports/recent")
async def recent_reports(request: Request) -> JSONResponse:
    st = request.app.state
    if getattr(st, "reports", None) is None:
        return JSONResponse({"reports": [], "hours": 0})
    hours = st.settings.reports_show_hours
    rows = await asyncio.to_thread(st.reports.recent, hours, 20)
    keep = ("id", "minutes_ago", "category", "severity", "summary", "station", "line")
    return JSONResponse({"reports": [{k: r[k] for k in keep} for r in rows], "hours": hours})
