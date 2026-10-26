"""JSON endpoints behind the map: address search, leave-now trip planning, live refresh.

    GET  /api/geocode?q=...          places matching a search (DMV region)
    GET  /api/reverse?lat=..&lon=..  a readable name for a point (e.g. "My location")
    POST /api/trip                   {"from": {lat, lon, label}, "to": {...}} -> options
    POST /api/trip/live              {"rides": [{agency, stop_id, trip_id}]} -> live times

Geocoding and live departures run server-side, so API keys never reach the browser.
Each visitor has its own rate limits; Transitland calls are capped by the monthly
budget (``api_budget.py``).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from transit_mcp.agencies import load_agencies
from transit_mcp.api_budget import ApiBudget
from transit_mcp.chat.guard import RateLimiter
from transit_mcp.db.database import Database
from transit_mcp.geocode import Geocoder, in_region
from transit_mcp.ingest.http import HttpClient
from transit_mcp.ingest.transitland import _secret, atlas_file_for, feed_record
from transit_mcp.planning.live import LivePlanner, LiveSettings, Point
from transit_mcp.planning.realtime import (
    CombinedRealtime,
    DirectTripUpdates,
    TransitlandRealtime,
)

log = logging.getLogger("transit_mcp.trip")
router = APIRouter()

PLEASE_WAIT = "You've searched several times very quickly. Please wait a moment and try again."


class Place(BaseModel):
    lat: float
    lon: float
    label: str = Field(default="", max_length=200)


class TripRequestBody(BaseModel):
    from_: Place = Field(alias="from")
    to: Place


class LiveRide(BaseModel):
    agency: str = Field(max_length=64)
    stop_id: str = Field(max_length=128)
    trip_id: str = Field(max_length=256)
    scheduled: int | None = None      # scheduled departure, epoch seconds


class LiveBody(BaseModel):
    rides: list[LiveRide] = Field(max_length=4)


def agency_info(settings) -> dict[str, dict]:
    """slug -> display name, whether it has realtime, its Transitland feed, base fare."""
    out = {}
    for a in load_agencies(settings):
        src = (a.sources_of("gtfs") or [None])[0]
        opts = src.options() if src else {}
        out[a.slug] = {
            "name": a.profile.display_name or a.name,
            "has_realtime": bool(opts.get("transitland_rt_id")),
            "transitland_id": opts.get("transitland_id"),
            "transitland_rt_id": opts.get("transitland_rt_id"),
            "transitland_atlas": opts.get("transitland_atlas"),
            "url": opts.get("url"),
            "auth_env": opts.get("auth_env"),
            "base_fare": a.fares.base_fare,
        }
    return out


def build_realtime(settings, agencies: dict[str, dict],
                   http: httpx.Client | None = None) -> CombinedRealtime:
    """Live times: each agency's own TripUpdates feed (listed in Transitland's registry)
    where we can read it, Transitland's departures API otherwise."""
    http = http or httpx.Client(timeout=8.0)
    direct: dict[str, tuple[str, dict]] = {}
    budget = ApiBudget.transitland(None, settings, shared=True)
    registry = HttpClient(user_agent=settings.http_user_agent, timeout=20.0)
    try:
        records = {
            slug: feed_record(a["transitland_rt_id"], settings=settings, http=registry,
                              budget=budget, atlas_file=a.get("transitland_atlas")
                              or atlas_file_for(a.get("url")))
            for slug, a in agencies.items() if a.get("transitland_rt_id")}
    finally:
        registry.close()
    for slug, rec in records.items():
        a = agencies[slug]
        if not rec or not rec.realtime_trip_updates:
            continue
        headers = {}
        if rec.auth_type == "header" and rec.auth_param:
            secret = _secret(settings, a.get("auth_env"))
            if not secret:
                continue          # e.g. Swiftly-hosted feeds: use Transitland instead
            headers = {rec.auth_param: secret}
        elif rec.auth_type:
            continue
        direct[slug] = (rec.realtime_trip_updates, headers)
    return CombinedRealtime(
        DirectTripUpdates(direct, tz=settings.timezone, http=http),
        TransitlandRealtime(
            api_key=settings.transitland_api_key,
            budget=budget,
            feeds={s: a["transitland_id"] for s, a in agencies.items() if a["transitland_id"]},
            cache_s=settings.trip_live_cache_s, tz=settings.timezone, http=http))


class TripService:
    """Long-lived pieces shared by requests (caches, limiters, HTTP clients)."""

    def __init__(self, settings):
        self.settings = settings
        self.agencies = agency_info(settings)
        self.http = httpx.Client(timeout=8.0)
        self.geocoder = Geocoder(settings.geocode_api_key, http=self.http)
        self.trip_limiter = RateLimiter(settings.trip_rate_per_minute, settings.trip_rate_per_day)
        self.geo_limiter = RateLimiter(40, 1000)
        self.realtime = build_realtime(settings, self.agencies, self.http)

    def close(self) -> None:
        self.http.close()

    def plan(self, origin: Point, destination: Point) -> dict:
        with Database(self.settings.db_path) as db:
            planner = LivePlanner(db, cfg=LiveSettings.from_settings(self.settings),
                                  realtime=self.realtime, agencies=self.agencies,
                                  tz=self.settings.timezone)
            return planner.plan(origin, destination)


def _service(request: Request) -> TripService:
    st = request.app.state
    if getattr(st, "trips", None) is None:
        st.trips = TripService(st.settings)
    return st.trips


def _ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.get("/api/geocode")
def geocode(request: Request, q: str = "") -> JSONResponse:
    svc = _service(request)
    if svc.geo_limiter.check(_ip(request)):
        return JSONResponse({"results": [], "message": PLEASE_WAIT}, status_code=429)
    if not svc.geocoder.available:
        return JSONResponse({"results": [], "message": "Address search isn't set up yet."},
                            status_code=503)
    try:
        return JSONResponse({"results": svc.geocoder.search(q)})
    except httpx.HTTPError as exc:
        log.warning("geocode failed: %s", exc)
        return JSONResponse({"results": [], "message": "Address search is busy. "
                             "Please try again in a moment."}, status_code=502)


@router.get("/api/reverse")
def reverse(request: Request, lat: float, lon: float) -> JSONResponse:
    svc = _service(request)
    if svc.geo_limiter.check(_ip(request)):
        return JSONResponse({"label": None}, status_code=429)
    if not in_region(lat, lon):
        return JSONResponse({"label": None, "outside_region": True})
    try:
        return JSONResponse({"label": svc.geocoder.reverse(lat, lon)})
    except httpx.HTTPError:
        return JSONResponse({"label": None})


@router.post("/api/trip")
def trip(request: Request, body: TripRequestBody) -> JSONResponse:
    svc = _service(request)
    if svc.trip_limiter.check(_ip(request)):
        return JSONResponse({"options": [], "notice": PLEASE_WAIT}, status_code=429)
    for p in (body.from_, body.to):
        if not in_region(p.lat, p.lon):
            return JSONResponse({"options": [], "notice": "This planner covers the DC, "
                                 "Maryland and Northern Virginia region."}, status_code=422)
    started = time.monotonic()
    try:
        result = svc.plan(Point(body.from_.lat, body.from_.lon, body.from_.label),
                          Point(body.to.lat, body.to.lon, body.to.label))
    except Exception:  # noqa: BLE001
        log.exception("trip planning failed")
        return JSONResponse({"options": [], "notice": "Sorry — something went wrong "
                             "planning that trip. Please try again."}, status_code=500)
    latency = int((time.monotonic() - started) * 1000)
    chatlog = getattr(request.app.state, "chatlog", None)
    if chatlog is not None:
        chatlog.safe_record_trip(
            visitor_id=chatlog.visitor_id(_ip(request)),
            origin=(body.from_.lat, body.from_.lon), destination=(body.to.lat, body.to.lon),
            n_options=len(result["options"]), live_checks=result.get("live_checks", 0),
            latency_ms=latency, notice=result.get("notice"))
    return JSONResponse(result)


@router.post("/api/trip/live")
def trip_live(request: Request, body: LiveBody) -> JSONResponse:
    """Fresh live times for the rides of the option a rider is looking at."""
    svc = _service(request)
    if svc.trip_limiter.check(_ip(request)) == "day":
        return JSONResponse({"rides": []}, status_code=429)
    tz = ZoneInfo(svc.settings.timezone)
    now = datetime.now(tz)
    midnight = int(datetime.combine(now.date(), datetime.min.time(), tz).timestamp())
    out = []
    for r in body.rides:
        if not svc.agencies.get(r.agency, {}).get("has_realtime"):
            continue
        sched = r.scheduled - midnight if r.scheduled else None
        hit, _ = svc.realtime.lookup(r.agency, r.stop_id, r.trip_id, None, sched, now.date())
        if hit:
            delay = hit["delay"]
            if delay is None and hit["dep_s"] is not None and sched is not None:
                delay = hit["dep_s"] - sched       # feeds that give times, not delays
            out.append({"trip_id": r.trip_id, "live": hit["live"],
                        "cancelled": hit["cancelled"], "delay_s": delay,
                        "expected": midnight + hit["dep_s"] if hit["dep_s"] is not None
                        else None})
    return JSONResponse({"rides": out, "live_available": svc.realtime.available()})
