"""Explore: every stop and route in the region, for the map's Explore layer.

    GET /api/explore                    -> agencies, routes, stops and transfer hubs
    GET /api/explore/stop?feed=&id=     -> one stop: its agency and the routes serving it

The network comes from the agencies' GTFS feeds already in the database: for each route,
its busiest shape in each direction (drawn from ``gtfs_shapes``), clipped to the region
(Amtrak and the intercity coaches run nationwide) and simplified a little more. It's
built once per data refresh, kept in memory, and sent compactly: routes as encoded
polylines, stops as plain arrays. The browser builds the map layers from it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import threading
from collections import defaultdict

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from transit_mcp.chat.guard import RateLimiter
from transit_mcp.db.database import Database
from transit_mcp.geocode import REGION, in_region
from transit_mcp.ingest.gtfs.shapes import decode, encode, simplify

log = logging.getLogger("transit_mcp.explore")
router = APIRouter()

SIMPLIFY_M = 20.0           # extra simplification: the whole network is drawn at once
# Stop colors, one per agency (route lines use the agency's own route colors).
PALETTE = ["#c62828", "#1b4f9c", "#2e7d32", "#ef6c00", "#6a1b9a", "#00838f", "#ad1457",
           "#5d7d1e", "#8d5524", "#3949ab", "#00695c", "#9e2a2b", "#7b5e00", "#d84315",
           "#283593", "#558b2f", "#6d4c41", "#4527a0", "#0277bd", "#37474f", "#b8860b",
           "#880e4f", "#1565c0", "#4e342e"]
RAIL_MODES = {"rail", "commuter_rail", "intercity_rail", "subway", "light_rail"}


def route_kind(route_type) -> str:
    """rail | ferry | bus, from GTFS route_type (basic or extended)."""
    try:
        t = int(route_type)
    except (TypeError, ValueError):
        return "bus"
    if t in (0, 1, 2, 5, 6, 7, 12) or 100 <= t < 200 or 400 <= t < 500 or 900 <= t < 1000:
        return "rail"
    if t == 4 or 1000 <= t < 1100 or t == 1200:
        return "ferry"
    return "bus"


def _hex(value: str | None) -> str | None:
    v = (value or "").strip().lstrip("#")
    return f"#{v.lower()}" if len(v) == 6 else None


def _inside(lat: float, lon: float) -> bool:
    return in_region(lat, lon)


def clip(points: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    """The parts of a line inside the region, each with one point beyond its ends so the
    line runs to the edge."""
    runs, run, last_out = [], [], None
    for p in points:
        if _inside(*p):
            if not run and last_out is not None:
                run.append(last_out)
            run.append(p)
        else:
            if run:
                run.append(p)
                runs.append(run)
                run = []
            last_out = p
    if run:
        runs.append(run)
    return [r for r in runs if len(r) >= 2]


def build_network(db: Database, agency_names: dict[str, str]) -> dict:
    """The payload for GET /api/explore (see the module docstring)."""
    conn = db.conn
    feeds = [r["feed_id"] for r in conn.execute(
        "SELECT feed_id FROM gtfs_feeds WHERE status IS NULL OR status != 'error' "
        "ORDER BY feed_id")]
    modes = {r["feed_id"]: r["mode"] for r in conn.execute("SELECT feed_id, mode FROM gtfs_feeds")}
    index = {f: i for i, f in enumerate(feeds)}

    # The busiest shape of each route in each direction.
    busiest: dict[tuple, tuple[str, int]] = {}
    for r in conn.execute(
            "SELECT feed_id, route_id, direction_id, shape_id, COUNT(*) AS n FROM gtfs_trips "
            "WHERE shape_id IS NOT NULL AND shape_id != '' "
            "GROUP BY feed_id, route_id, direction_id, shape_id"):
        key = (r["feed_id"], r["route_id"], r["direction_id"])
        if key not in busiest or r["n"] > busiest[key][1]:
            busiest[key] = (r["shape_id"], r["n"])
    shapes_of: dict[tuple[str, str], set[str]] = defaultdict(set)
    for (feed, route, _), (shape, _) in busiest.items():
        shapes_of[(feed, route)].add(shape)

    routes = []
    for r in conn.execute("SELECT feed_id, route_id, short_name, long_name, route_type, color "
                          "FROM gtfs_routes ORDER BY feed_id, route_id"):
        feed = r["feed_id"]
        if feed not in index:
            continue
        lines = []
        for shape_id in sorted(shapes_of.get((feed, r["route_id"]), ())):
            row = conn.execute("SELECT polyline FROM gtfs_shapes WHERE feed_id = ? AND "
                               "shape_id = ?", (feed, shape_id)).fetchone()
            if not row or not row["polyline"]:
                continue
            for part in clip(decode(row["polyline"])):
                lines.append(encode(simplify(part, SIMPLIFY_M)))
        if not lines:
            continue
        routes.append([index[feed], r["route_id"], (r["short_name"] or "").strip(),
                       (r["long_name"] or "").strip(), _hex(r["color"]),
                       route_kind(r["route_type"]), lines])

    # Boardable stops and stations; platforms inside a station show as the station.
    stops = []
    for s in conn.execute(
            "SELECT feed_id, stop_id, name, lat, lon FROM gtfs_stops "
            "WHERE (location_type IS NULL OR location_type IN (0, 1)) "
            "AND (location_type = 1 OR parent_station IS NULL OR parent_station = '') "
            "AND lat IS NOT NULL AND lon IS NOT NULL"):
        if s["feed_id"] in index and _inside(s["lat"], s["lon"]):
            stops.append([index[s["feed_id"]], s["stop_id"], s["name"] or "",
                          round(s["lat"], 5), round(s["lon"], 5)])

    hubs = [[round(h["lat"], 5), round(h["lon"], 5), h["n_stops"], h["n_feeds"]]
            for h in conn.execute("SELECT lat, lon, n_stops, n_feeds FROM stop_clusters "
                                  "WHERE n_feeds >= 2")
            if _inside(h["lat"], h["lon"])]

    # Parking and park & ride (planning/parking.py); lots with no parking stay off the map.
    from transit_mcp.planning import parking

    lots = [{k: lot.get(k) for k in ("id", "operator", "kind", "name", "spaces",
                                     "short_term_spaces", "cost", "address", "notes",
                                     "options", "also_serves", "source", "checked")}
            | {"lat": round(lot["lat"], 5), "lon": round(lot["lon"], 5)}
            for lot in parking.all_lots(db) if lot["available"]]

    agencies = [{"id": f, "name": agency_names.get(f) or f, "mode": modes.get(f) or "",
                 "rail": (modes.get(f) or "") in RAIL_MODES,
                 "color": PALETTE[i % len(PALETTE)]} for i, f in enumerate(feeds)]
    return {"agencies": agencies, "routes": routes, "stops": stops, "hubs": hubs,
            "parking": lots, "region": REGION}


def stop_detail(db: Database, feed: str, stop_id: str, agency_names: dict[str, str]) -> dict | None:
    """A stop (or station), its agency, the routes that serve it and its transfer hub."""
    conn = db.conn
    stop = conn.execute("SELECT * FROM gtfs_stops WHERE feed_id = ? AND stop_id = ?",
                        (feed, stop_id)).fetchone()
    if stop is None:
        return None
    ids = [stop_id] + [r["stop_id"] for r in conn.execute(
        "SELECT stop_id FROM gtfs_stops WHERE feed_id = ? AND parent_station = ?",
        (feed, stop_id))]
    marks = ",".join("?" * len(ids))
    route_ids = [r["route_id"] for r in conn.execute(
        f"SELECT DISTINCT t.route_id FROM gtfs_stop_times st JOIN gtfs_trips t "
        f"ON t.feed_id = st.feed_id AND t.trip_id = st.trip_id "
        f"WHERE st.feed_id = ? AND st.stop_id IN ({marks})", (feed, *ids))]
    routes = []
    for rid in route_ids:
        r = conn.execute("SELECT short_name, long_name, route_type, color, text_color "
                         "FROM gtfs_routes WHERE feed_id = ? AND route_id = ?",
                         (feed, rid)).fetchone()
        if r is not None:
            routes.append({"id": rid, "short": (r["short_name"] or "").strip(),
                           "long": (r["long_name"] or "").strip(), "color": _hex(r["color"]),
                           "text": _hex(r["text_color"]), "kind": route_kind(r["route_type"])})
    routes.sort(key=lambda x: (x["kind"] != "rail", len(x["short"] or x["long"]),
                               x["short"] or x["long"]))
    hub = None
    cluster = conn.execute("SELECT cluster_id FROM cluster_stops WHERE feed_id = ? AND "
                           "stop_id = ?", (feed, stop_id)).fetchone()
    if cluster is not None:
        info = conn.execute("SELECT n_stops, n_feeds FROM stop_clusters WHERE cluster_id = ?",
                            (cluster["cluster_id"],)).fetchone()
        if info is not None and info["n_feeds"] >= 2:
            others = [agency_names.get(r["feed_id"]) or r["feed_id"] for r in conn.execute(
                "SELECT DISTINCT feed_id FROM cluster_stops WHERE cluster_id = ? "
                "AND feed_id != ? ORDER BY feed_id", (cluster["cluster_id"], feed))]
            hub = {"stops": info["n_stops"], "agencies": others}
    wheelchair = {1: True, 2: False}.get(stop["wheelchair_boarding"])
    return {"feed": feed, "id": stop_id, "name": stop["name"] or "", "code": stop["code"],
            "agency": agency_names.get(feed) or feed, "lat": stop["lat"], "lon": stop["lon"],
            "wheelchair": wheelchair, "routes": routes, "hub": hub}


class ExploreCache:
    """The network payload, rebuilt when the timetables change."""

    def __init__(self):
        self._lock = threading.Lock()
        self.key: tuple | None = None
        self.body: bytes = b""
        self.etag: str = ""
        self.limiter = RateLimiter(120, 6000)       # stop look-ups per visitor

    @staticmethod
    def _version(db: Database) -> tuple:
        feeds = db.conn.execute("SELECT MAX(fetched_at), COUNT(*) FROM gtfs_feeds").fetchone()
        try:
            hubs = db.conn.execute("SELECT MAX(computed_at) FROM stop_clusters").fetchone()[0]
        except Exception:  # noqa: BLE001 - an older database without clusters
            hubs = None
        try:
            lots = db.conn.execute("SELECT MAX(retrieved_at) FROM wmata_parking").fetchone()[0]
        except Exception:  # noqa: BLE001 - an older database without parking
            lots = None
        return (feeds[0], feeds[1], hubs, lots)

    def get(self, settings, agency_names: dict[str, str]) -> tuple[bytes, str]:
        with self._lock, Database(settings.db_path) as db:
            version = self._version(db)
            if version != self.key:
                data = build_network(db, agency_names)
                self.body = json.dumps(data, separators=(",", ":")).encode("utf-8")
                self.etag = '"' + hashlib.sha256(self.body).hexdigest()[:20] + '"'
                self.key = version
                log.info("explore: %d routes, %d stops, %d hubs, %d KB", len(data["routes"]),
                         len(data["stops"]), len(data["hubs"]), len(self.body) // 1024)
            return self.body, self.etag


    def warm(self, settings, agency_names: dict[str, str]) -> None:
        try:
            self.get(settings, agency_names)
        except Exception:  # noqa: BLE001 - built on first request instead
            log.exception("explore: couldn't build the network in advance")


def _cache(request: Request) -> ExploreCache:
    st = request.app.state
    if getattr(st, "explore", None) is None:
        st.explore = ExploreCache()
    return st.explore


def _names(request: Request) -> dict[str, str]:
    from transit_mcp.chat.trip_api import _service

    return {slug: info["name"] for slug, info in _service(request).agencies.items()}


@router.get("/api/explore")
async def network(request: Request) -> Response:
    settings = request.app.state.settings
    body, etag = await asyncio.to_thread(_cache(request).get, settings, _names(request))
    headers = {"ETag": etag, "Cache-Control": "public, max-age=3600"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, media_type="application/json", headers=headers)


@router.get("/api/explore/stop")
async def stop(request: Request, feed: str, id: str) -> JSONResponse:  # noqa: A002 - query name
    cache = _cache(request)
    ip = request.client.host if request.client else "unknown"
    if cache.limiter.check(ip):
        return JSONResponse({"message": "That's a lot of look-ups. Please wait a minute."},
                            status_code=429)
    settings = request.app.state.settings
    names = _names(request)

    def look_up():
        with Database(settings.db_path) as db:
            return stop_detail(db, feed[:40], id[:80], names)

    found = await asyncio.to_thread(look_up)
    if found is None:
        return JSONResponse({"message": "That stop isn't in the timetables."}, status_code=404)
    return JSONResponse(found, headers={"Cache-Control": "public, max-age=3600"})
