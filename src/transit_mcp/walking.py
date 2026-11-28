"""Walking directions over real streets and paths, from openrouteservice (HeiGIT), which
routes on OpenStreetMap data: the path to draw, the distance, and turn-by-turn steps.

The trip planner still picks stops with a quick straight-line estimate; this module
replaces that estimate for the walks a rider actually looks at. Metro walks end at the
station entrance nearest the other end of the walk (GTFS ``location_type`` 2), and name
the elevator entrance too when it's a different one.

Requests are capped (openrouteservice's free plan allows about 2,000 a day and 40 a
minute, shared by the site and the MCP server, so each gets its own share), cached for a
month, and anything that goes wrong gives ``None``: the caller keeps its straight line.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from transit_mcp.chat.guard import RateLimiter
from transit_mcp.ingest.gtfs.clusters import haversine_m

log = logging.getLogger("transit_mcp.walking")

# HeiGIT moved openrouteservice to api.heigit.org; api.openrouteservice.org answers 410
# Gone since 2026-11-24. Same keys, same requests and answers.
ORS_URL = "https://api.heigit.org/openrouteservice/v2/directions/foot-walking/geojson"
ATTRIBUTION = ("Walking directions © openrouteservice.org by HeiGIT, "
               "map data © OpenStreetMap contributors")
CACHE_DAYS = 30
MAX_WALK_M = 5000          # longer than any walk the planner offers
MIN_WALK_M = 25            # closer than this there's nothing to route
JOG_M = 40                 # unnamed bits shorter than this aren't steps of their own
_TURNS = {0: "left", 1: "right", 2: "sharp left", 3: "sharp right", 4: "slight left",
          5: "slight right", 6: "straight", 9: "u-turn", 12: "keep left", 13: "keep right"}


def _key(a: tuple[float, float], b: tuple[float, float]) -> str:
    # About 10 m: nearby starts share a route (it's re-trimmed to the exact ends anyway).
    return f"{a[0]:.4f},{a[1]:.4f};{b[0]:.4f},{b[1]:.4f}"


class WalkRouter:
    """Walking routes between two points (lat, lon), with a cap and a cache."""

    def __init__(self, api_key: str | None, *, cache_path: str | Path | None = None,
                 per_minute: int = 30, per_day: int = 1500, pace_m_per_min: float = 70.0,
                 http: httpx.Client | None = None, timeout_s: float = 6.0):
        self.api_key = api_key
        self.pace = pace_m_per_min
        self.http = http or httpx.Client(timeout=timeout_s)
        self.limit = RateLimiter(per_minute, per_day)
        self.paused_until = 0.0
        self.cache_path = Path(cache_path) if cache_path else None
        self._lock = threading.Lock()
        if self.cache_path:
            with self._db() as c:
                c.execute("CREATE TABLE IF NOT EXISTS walks (key TEXT PRIMARY KEY, "
                          "body TEXT NOT NULL, at REAL NOT NULL)")

    @classmethod
    def from_settings(cls, settings, *, share: str = "site", http=None) -> WalkRouter:
        per_day = (settings.ors_mcp_daily_limit if share == "mcp"
                   else settings.ors_daily_limit)
        return cls(settings.ors_api_key, per_day=per_day,
                   per_minute=settings.ors_per_minute if share != "mcp"
                   else max(1, settings.ors_per_minute // 3),
                   cache_path=Path(settings.db_path).with_name("walks.db"),
                   pace_m_per_min=settings.trip_walk_m_per_min, http=http)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _db(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.cache_path, timeout=5)
        c.isolation_level = None
        return c

    # -- cache ---------------------------------------------------------------------------
    def _cached(self, key: str) -> dict | None:
        if not self.cache_path:
            return None
        with self._lock, self._db() as c:
            row = c.execute("SELECT body, at FROM walks WHERE key = ?", (key,)).fetchone()
        if row and time.time() - row[1] < CACHE_DAYS * 86400:
            return json.loads(row[0])
        return None

    def _store(self, key: str, body: dict) -> None:
        if not self.cache_path:
            return
        with self._lock, self._db() as c:
            c.execute("INSERT OR REPLACE INTO walks (key, body, at) VALUES (?, ?, ?)",
                      (key, json.dumps(body, separators=(",", ":")), time.time()))
            if hash(key) % 200 == 0:      # now and then, drop old routes
                c.execute("DELETE FROM walks WHERE at < ?",
                          (time.time() - CACHE_DAYS * 86400,))

    # -- one route -----------------------------------------------------------------------
    def route(self, a: tuple[float, float], b: tuple[float, float]) -> dict | None:
        """The walk from ``a`` to ``b``: distance_m, duration_s (at the rider's gentle
        pace), path ([[lon, lat], ...]) and steps ([{text, distance_m}]). None when it
        can't be had (no key, over the cap, an error): keep the straight line."""
        d = haversine_m(a[0], a[1], b[0], b[1])
        if d < MIN_WALK_M or d > MAX_WALK_M:
            return None
        key = _key(a, b)
        hit = self._cached(key)
        if hit is not None:
            return self._finish(hit, a, b)
        if not self.available or time.time() < self.paused_until:
            return None
        if self.limit.check("ors"):
            log.info("walking: over the openrouteservice cap; straight lines for now")
            return None
        try:
            r = self.http.post(ORS_URL, headers={"Authorization": self.api_key,
                                                 "Accept": "application/geo+json"},
                               json={"coordinates": [[a[1], a[0]], [b[1], b[0]]],
                                     "instructions": True, "maneuvers": True, "units": "m",
                                     "language": "en"})
        except httpx.HTTPError as exc:
            log.warning("walking: openrouteservice unreachable: %s", exc)
            self.paused_until = time.time() + 60
            return None
        if r.status_code in (401, 403, 410, 429):   # bad key, moved, or quota: back off
            log.warning("walking: openrouteservice said %s", r.status_code)
            self.paused_until = time.time() + (60 if r.status_code == 429 else 3600)
            return None
        if r.status_code != 200:                 # e.g. 404: no path between the points
            log.info("walking: no route (%s) %s", r.status_code, key)
            return None
        try:
            body = parse(r.json())
        except (KeyError, IndexError, TypeError, ValueError):
            log.warning("walking: unexpected openrouteservice answer")
            return None
        self._store(key, body)
        return self._finish(body, a, b)

    def _finish(self, body: dict, a, b) -> dict:
        path = [[round(x, 6), round(y, 6)] for x, y in body["path"]]
        # Start and end exactly at the rider's points (the route starts on the nearest path).
        if path and haversine_m(a[0], a[1], path[0][1], path[0][0]) > 3:
            path.insert(0, [a[1], a[0]])
        if path and haversine_m(b[0], b[1], path[-1][1], path[-1][0]) > 3:
            path.append([b[1], b[0]])
        meters = body["distance_m"]
        return {"distance_m": round(meters), "path": path, "steps": body["steps"],
                "duration_s": int(math.ceil(meters / self.pace * 60)), "routed": True}

    def routes(self, pairs: list[tuple[tuple[float, float], tuple[float, float]]],
               workers: int = 4) -> list[dict | None]:
        """Several walks at once (a trip's walks), in order."""
        if not pairs:
            return []
        with ThreadPoolExecutor(max_workers=min(workers, len(pairs))) as pool:
            return list(pool.map(lambda p: self.route(*p), pairs))


_COMPASS = ["north", "northeast", "east", "southeast", "south", "southwest", "west",
            "northwest"]
_SIGN = [(r"\bNorthwest\b", "NW"), (r"\bNortheast\b", "NE"), (r"\bSouthwest\b", "SW"),
         (r"\bSoutheast\b", "SE"), (r"\bStreet\b", "St"), (r"\bAvenue\b", "Ave"),
         (r"\bPlace\b", "Pl"), (r"\bRoad\b", "Rd"), (r"\bBoulevard\b", "Blvd"),
         (r"\bDrive\b", "Dr"), (r"\bCourt\b", "Ct"), (r"\bTerrace\b", "Ter")]


def _sign(text: str) -> str:
    """Street names as the signs read: "F Street Northwest" -> "F St NW"."""
    for pattern, short in _SIGN:
        text = re.sub(pattern, short, text)
    return text


def _bearing(step: dict) -> float | None:
    b = (step.get("maneuver") or {}).get("bearing_after")
    return None if b is None else float(b) % 360


def _compass(bearing: float) -> str:
    return _COMPASS[int((bearing + 22.5) // 45) % 8]


def _turned(a: float, b: float) -> float:
    d = abs(a - b) % 360
    return min(d, 360 - d)


def parse(geojson: dict) -> dict:
    """openrouteservice's GeoJSON answer -> {distance_m, path, steps}, tidied for people.

    Named streets keep openrouteservice's words, shortened as the signs show them ("Turn
    right onto G St NW"). Many sidewalks and paths have no name in OpenStreetMap; those
    steps say which way to walk instead ("Walk north"), and keep going while the way
    bends only a little. Crosswalks and other short jogs are folded into the step
    before, and the same street twice in a row is one step."""
    feature = geojson["features"][0]
    props = feature["properties"]
    raw = [s for seg in props.get("segments", []) for s in seg.get("steps", [])]
    steps: list[dict] = []
    for s in raw:
        kind, meters = s.get("type"), float(s.get("distance") or 0)
        text = (s.get("instruction") or "").strip()
        name = (s.get("name") or "").strip()
        named = bool(name) and name != "-"
        bearing = _bearing(s)
        if kind == 10 or not text:              # "Arrive at...": the trip card says where
            continue
        prev = steps[-1] if steps else None
        if prev and not named and meters < JOG_M:
            prev["_m"] += meters                # a crosswalk or a jog: keep going
            continue
        if prev and named and name == prev["_name"] and kind in (4, 5, 6, 12, 13):
            prev["_m"] += meters                # the same street again
            continue
        if prev and not named and bearing is not None and prev["_bearing"] is not None:
            bend = _turned(bearing, prev["_bearing"])
            # Straight on (often a street's sidewalk, mapped on its own with no name), or a
            # path that bends a little: the same step.
            if bend <= 25 or (prev["_name"] is None and bend <= 50 and meters < 120):
                prev["_m"] += meters
                continue
        if named:
            text = _sign(text)
        elif bearing is not None:
            text = f"{'Head' if kind == 11 else 'Walk'} {_compass(bearing)}"
        steps.append({"text": text, "_m": meters, "_name": name if named else None,
                      "_bearing": bearing, "turn": _TURNS.get(kind)})
    return {"distance_m": float(props["summary"]["distance"]),
            "path": feature["geometry"]["coordinates"],
            "steps": [{"text": x["text"], "distance_m": round(x["_m"]), "turn": x["turn"]}
                      for x in steps]}


# -- Metro entrances --------------------------------------------------------------------
def entrances(db, feed: str, stop_id: str) -> list[dict]:
    """The street entrances of the station a stop belongs to (empty for a bus stop)."""
    try:
        row = db.conn.execute("SELECT parent_station, location_type FROM gtfs_stops "
                              "WHERE feed_id = ? AND stop_id = ?", (feed, stop_id)).fetchone()
    except sqlite3.Error:
        return []
    if not row:
        return []
    station = stop_id if row["location_type"] == 1 else row["parent_station"]
    if not station:
        return []
    rows = db.conn.execute(
        "SELECT stop_id, name, lat, lon, wheelchair_boarding FROM gtfs_stops WHERE feed_id = ? "
        "AND parent_station = ? AND location_type = 2 AND lat IS NOT NULL",
        (feed, station)).fetchall()
    return [{"name": _entrance_name(r["name"]), "lat": r["lat"], "lon": r["lon"],
             "elevator": "elevator" in (r["name"] or "").lower()
             or r["wheelchair_boarding"] == 1} for r in rows]


def _entrance_name(name: str | None) -> str:
    # WMATA writes "12Th and G Sts." -- tidy the capital after a number.
    return re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(),
                  (name or "Entrance").strip())


def pick_entrance(options: list[dict], toward: tuple[float, float]) -> tuple[dict | None,
                                                                             dict | None]:
    """The entrance nearest ``toward`` (stairs/escalators or elevator, whichever is
    closer), and the nearest elevator entrance if that's a different one."""
    if not options:
        return None, None
    by = sorted(options, key=lambda e: haversine_m(toward[0], toward[1], e["lat"], e["lon"]))
    lift = next((e for e in by if e["elevator"]), None)
    return by[0], (lift if lift is not by[0] else None)


def walk_ends(db, a: tuple[float, float], b: tuple[float, float],
              from_stop: tuple[str, str] | None = None,
              to_stop: tuple[str, str] | None = None) -> tuple[tuple, tuple, dict]:
    """Where a walk really starts and ends: leaving a station by its best exit, going in
    at its best entrance ((feed, stop_id) of the platform). ``same_station`` when it's a
    change of platforms inside one station (nothing to route)."""
    extra: dict = {}
    into = entrances(db, *to_stop) if to_stop else []
    out_of = entrances(db, *from_stop) if from_stop else []
    if into and into == out_of:
        return a, a, {"same_station": True}
    door, lift = pick_entrance(into, a)
    if door:
        b = (door["lat"], door["lon"])
        extra["entrance"] = door["name"]
        if lift:
            extra["elevator"] = lift["name"]
    door, lift = pick_entrance(out_of, b)
    if door:
        a = (door["lat"], door["lon"])
        extra["exit"] = door["name"]
        if lift:
            extra["exit_elevator"] = lift["name"]
    return a, b, extra


def walk_legs(opt: dict, origin: dict, destination: dict) -> list[tuple[int, tuple, tuple,
                                                                     tuple | None,
                                                                     tuple | None]]:
    """An option's walks to route: (leg index, from, to, from_stop, to_stop). Airport
    walks and shuttles are left out (the airport's own directions cover them)."""
    legs, out = opt["legs"], []
    for i, leg in enumerate(legs):
        if leg["mode"] != "walk" or leg.get("airport"):
            continue
        before = legs[i - 1] if i else None
        after = legs[i + 1] if i + 1 < len(legs) else None
        start = leg.get("from") or (before["alight"] if before and before["mode"] == "ride"
                                    else origin)
        end = leg.get("to") or destination
        if start.get("lat") is None or end.get("lat") is None:
            continue
        from_stop = ((before["agency"], start["stop_id"]) if before and before["mode"] == "ride"
                     and start.get("stop_id") else None)
        to_stop = ((after["agency"], end["stop_id"]) if after and after["mode"] == "ride"
                   and end.get("stop_id") else None)
        out.append((i, (start["lat"], start["lon"]), (end["lat"], end["lon"]),
                    from_stop, to_stop))
    return out


def apply_walk(opt: dict, i: int, walk: dict, now: float) -> None:
    """Put a routed walk on leg ``i`` and move the option's times to match (the same
    as loadWalks/applyWalk in app.js)."""
    leg = opt["legs"][i]
    leg["walk"] = walk
    if not walk.get("routed"):
        return
    delta = walk["duration_s"] - leg["duration_s"]
    leg["duration_s"] = walk["duration_s"]
    opt["walk_s"] += delta
    if i == 0:
        opt["leave_at"] -= delta
        opt["wait_s"] -= delta
    elif leg.get("transfer"):
        leg["wait_s"] -= delta
    else:
        opt["arrive_at"] += delta
    opt["duration_s"] = opt["arrive_at"] - max(opt["leave_at"], now)


def route_option(db, router: WalkRouter, opt: dict, origin: dict, destination: dict,
                 now: float, only_ends: bool = False) -> None:
    """Route an option's walks (``only_ends``: just the first and last) and apply them."""
    todo = walk_legs(opt, origin, destination)
    if only_ends and todo:
        last = len(opt["legs"]) - 1
        todo = [t for t in todo if t[0] in (0, last)]
    ends = [walk_ends(db, a, b, fs, ts) for _, a, b, fs, ts in todo]
    found = router.routes([(a, b) for a, b, extra in ends if not extra.get("same_station")])
    it = iter(found)
    for (i, *_), (_, _, extra) in zip(todo, ends):
        f = None if extra.get("same_station") else next(it)
        walk = {**f, **extra} if f else extra
        if walk and not walk.get("same_station"):
            apply_walk(opt, i, walk, now)
