"""Live departure times for the planner, from two sources (both found via Transitland):

* :class:`DirectTripUpdates` — an agency's own GTFS-realtime TripUpdates feed, at the
  URL Transitland's registry lists for it. One download (cached 30 s) covers every
  trip, uses the same trip IDs as the timetable we loaded, and costs no Transitland
  quota. Used wherever the feed is reachable (WMATA rail and bus, VRE, Fairfax
  Connector, MARC).
* :class:`TransitlandRealtime` — Transitland's departures API, one call per stop, for
  feeds we can't read directly (e.g. those behind Swiftly keys). Calls are precious on
  the Explorer plan: each answer is reused for ``TRIP_LIVE_CACHE_S`` and every call is
  reserved from the monthly ``ApiBudget`` first.

:class:`CombinedRealtime` picks the right one per feed. When neither works, the planner
shows scheduled times, labelled as such.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from transit_mcp.api_budget import ApiBudget

log = logging.getLogger("transit_mcp.realtime")
REST_BASE = "https://transit.land/api/v2/rest"


class TransitlandRealtime:
    def __init__(self, *, api_key: str | None, budget: ApiBudget | None,
                 feeds: dict[str, str], cache_s: int = 60, tz: str = "America/New_York",
                 http: httpx.Client | None = None):
        """``feeds`` maps our agency slug -> the static feed's Transitland Onestop ID."""
        self.api_key = api_key
        self.budget = budget
        self.feeds = feeds
        self.cache_s = cache_s
        self.tz = ZoneInfo(tz)
        self.http = http or httpx.Client(timeout=8.0)
        self._cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def available(self) -> bool:
        return bool(self.api_key) and (self.budget is None or self.budget.remaining() > 0)

    def cached(self, feed: str, stop_id: str) -> bool:
        hit = self._cache.get((feed, stop_id))
        return bool(hit and time.monotonic() - hit[0] < self.cache_s)

    def free(self, feed: str, stop_id: str) -> bool:
        """True if this stop's answer is cached (no Transitland call needed)."""
        return self.cached(feed, stop_id)

    def lookup(self, feed: str, stop_id: str, trip_id: str, seq: int | None,
               sched_dep_s: int | None, day: date) -> tuple[dict | None, int]:
        """Live info for one trip at one stop, and how many Transitland calls it took."""
        deps, fetched = self.stop_departures(feed, stop_id, day)
        return (deps or {}).get(trip_id), int(fetched)

    def stop_departures(self, feed: str, stop_id: str, service_day: date
                        ) -> tuple[dict | None, bool]:
        """{trip_id: {"dep_s", "delay", "live", "cancelled"}} for one stop, and whether
        an API call was made. ``dep_s`` is seconds since local midnight of
        ``service_day`` (may exceed 24 h)."""
        key = (feed, stop_id)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < self.cache_s:
                return hit[1], False
        onestop = self.feeds.get(feed)
        if not onestop or not self.api_key:
            return None, False
        if self.budget is not None and not self.budget.try_spend():
            return None, False
        start = (datetime.now(self.tz) - timedelta(minutes=15)).strftime("%H:%M:%S")
        url = f"{REST_BASE}/stops/{quote(onestop + ':' + stop_id, safe='~:')}/departures"
        try:
            r = self.http.get(url, params={"start_time": start, "next": 5400, "limit": 100},
                              headers={"apikey": self.api_key})
            r.raise_for_status()
            parsed = parse_departures(r.json(), service_day, self.tz)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("live departures for %s:%s failed: %s", feed, stop_id, exc)
            parsed = None
        with self._lock:
            self._cache[key] = (time.monotonic(), parsed or {})
            if len(self._cache) > 5000:  # keep memory bounded
                oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:1000]
                for k, _ in oldest:
                    self._cache.pop(k, None)
        return parsed, True


def _seconds_since(midnight: datetime, value: str | None) -> int | None:
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=midnight.tzinfo)
    return int((t - midnight).total_seconds())


def _hms(value: str | None) -> int | None:
    if not value or value.count(":") != 2:
        return None
    try:
        h, m, s = (int(x) for x in value.split(":"))
        return h * 3600 + m * 60 + s
    except ValueError:
        return None


def parse_departures(data: dict, service_day: date, tz: ZoneInfo) -> dict:
    """Reduce a departures response to per-trip live info."""
    midnight = datetime.combine(service_day, datetime.min.time(), tz)
    out: dict[str, dict] = {}
    for stop in data.get("stops") or []:
        for st in stop.get("departures") or []:
            trip = st.get("trip") or {}
            trip_id = trip.get("trip_id")
            if not trip_id:
                continue
            dep = st.get("departure") or st.get("arrival") or {}
            rel = (st.get("schedule_relationship") or trip.get("schedule_relationship")
                   or "STATIC").upper()
            est = _seconds_since(midnight, dep.get("estimated_utc") or dep.get("estimated_local"))
            if est is None:
                est = _hms(dep.get("estimated"))
            delay = dep.get("delay")
            if delay is None:
                delay = dep.get("estimated_delay")
            live = rel != "STATIC" and (est is not None or delay is not None)
            out[trip_id] = {
                "dep_s": est if live else None,
                "delay": int(delay) if live and delay is not None else None,
                "live": live,
                "cancelled": rel in ("CANCELED", "CANCELLED", "SKIPPED"),
            }
    return out



class DirectTripUpdates:
    """Agencies' own GTFS-realtime TripUpdates feeds, one cached download per feed."""

    def __init__(self, feeds: dict[str, tuple[str, dict]], *, cache_s: int = 30,
                 tz: str = "America/New_York", http: httpx.Client | None = None):
        """``feeds``: agency slug -> (TripUpdates URL, request headers)."""
        self.feeds = feeds
        self.cache_s = cache_s
        self.tz = ZoneInfo(tz)
        self.http = http or httpx.Client(timeout=8.0)
        self._cache: dict[str, tuple[float, dict | None]] = {}
        self._lock = threading.Lock()

    def has(self, feed: str) -> bool:
        if feed not in self.feeds:
            return False
        hit = self._cache.get(feed)
        # A feed that failed recently is skipped (so the fallback can be used) for 5 min.
        return not (hit and hit[1] is None and time.monotonic() - hit[0] < 300)

    def _trips(self, feed: str) -> dict | None:
        with self._lock:
            hit = self._cache.get(feed)
            if hit and time.monotonic() - hit[0] < (self.cache_s if hit[1] is not None else 300):
                return hit[1]
        url, headers = self.feeds[feed]
        try:
            r = self.http.get(url, headers=headers)
            r.raise_for_status()
            data = parse_trip_updates(r.content)
        except Exception as exc:  # noqa: BLE001 - network, TLS or protobuf errors
            log.warning("realtime feed %s unavailable: %s", feed, exc)
            data = None
        with self._lock:
            self._cache[feed] = (time.monotonic(), data)
        return data

    def lookup(self, feed: str, stop_id: str, trip_id: str, seq: int | None,
               sched_dep_s: int | None, day: date) -> tuple[dict | None, int]:
        trips = self._trips(feed)
        if trips is None:
            return None, 0
        t = trips.get(trip_id)
        if t is None:
            return None, 0      # no live update for this trip: the timetable stands
        if t["cancelled"]:
            return {"dep_s": None, "delay": None, "live": True, "cancelled": True}, 0
        midnight = int(datetime.combine(day, datetime.min.time(), self.tz).timestamp())
        exact = next((u for u in t["stops"]
                      if u["stop_id"] == stop_id or (seq is not None and u["seq"] == seq)), None)
        if exact:
            if exact["skipped"]:
                return {"dep_s": None, "delay": None, "live": True, "cancelled": True}, 0
            if exact["time"] is not None:
                return {"dep_s": exact["time"] - midnight, "delay": None, "live": True,
                        "cancelled": False}, 0
            if exact["delay"] is not None and sched_dep_s is not None:
                return {"dep_s": sched_dep_s + exact["delay"], "delay": exact["delay"],
                        "live": True, "cancelled": False}, 0
        # Not listed at this stop: carry the latest known delay forward along the trip.
        before = [u for u in t["stops"] if u["delay"] is not None
                  and (seq is None or u["seq"] is None or u["seq"] <= seq)]
        delay = before[-1]["delay"] if before else t["delay"]
        if delay is None or sched_dep_s is None:
            return None, 0
        return {"dep_s": sched_dep_s + delay, "delay": delay, "live": True,
                "cancelled": False}, 0


def parse_trip_updates(content: bytes) -> dict:
    """trip_id -> {"cancelled", "delay", "stops": [{stop_id, seq, time, delay, skipped}]}"""
    from google.transit import gtfs_realtime_pb2

    msg = gtfs_realtime_pb2.FeedMessage()
    msg.ParseFromString(content)
    canceled = gtfs_realtime_pb2.TripDescriptor.CANCELED
    skipped = gtfs_realtime_pb2.TripUpdate.StopTimeUpdate.SKIPPED
    out: dict[str, dict] = {}
    for ent in msg.entity:
        if not ent.HasField("trip_update"):
            continue
        tu = ent.trip_update
        trip_id = tu.trip.trip_id
        if not trip_id:
            continue
        stops = []
        for u in tu.stop_time_update:
            ev = u.departure if u.HasField("departure") else (
                u.arrival if u.HasField("arrival") else None)
            stops.append({
                "stop_id": u.stop_id or None,
                "seq": u.stop_sequence if u.HasField("stop_sequence") else None,
                "time": (ev.time or None) if ev is not None else None,
                "delay": (ev.delay if ev is not None and ev.HasField("delay") else None),
                "skipped": u.schedule_relationship == skipped,
            })
        out[trip_id] = {
            "cancelled": tu.trip.schedule_relationship == canceled,
            "delay": tu.delay if tu.HasField("delay") else None,
            "stops": stops,
        }
    return out


class CombinedRealtime:
    """The agency's own feed when we can read it; Transitland's departures otherwise."""

    def __init__(self, direct: DirectTripUpdates | None, transitland: TransitlandRealtime | None):
        self.direct = direct
        self.transitland = transitland

    def available(self) -> bool:
        return bool((self.direct and self.direct.feeds)
                    or (self.transitland and self.transitland.available()))

    def free(self, feed: str, stop_id: str) -> bool:
        """True if checking this stop costs no Transitland call."""
        if self.direct and self.direct.has(feed):
            return True
        return bool(self.transitland and self.transitland.free(feed, stop_id))

    def lookup(self, feed: str, stop_id: str, trip_id: str, seq: int | None,
               sched_dep_s: int | None, day: date) -> tuple[dict | None, int]:
        if self.direct and self.direct.has(feed):
            info, calls = self.direct.lookup(feed, stop_id, trip_id, seq, sched_dep_s, day)
            if self.direct.has(feed):     # the download worked: trust its answer
                return info, calls
        if self.transitland:
            return self.transitland.lookup(feed, stop_id, trip_id, seq, sched_dep_s, day)
        return None, 0
