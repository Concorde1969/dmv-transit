"""Live departures from Transitland (scheduled times enriched with GTFS-realtime).

One call per stop (``/stops/<feed onestop id>:<stop_id>/departures``), so calls are
precious on the Explorer plan: each answer is reused for ``TRIP_LIVE_CACHE_S``
(60 s) and every call is reserved from the monthly ``ApiBudget`` first. When the key is
missing or the budget is spent, :meth:`available` is False and the planner shows
scheduled times, labelled as such.
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
