"""Addresses and place names -> map points, via geocode.maps.co (server-side only).

The API key never reaches the browser. Results are limited to the DMV/Baltimore region,
cached for a day, and requests are spaced to respect the free plan (5 per second,
then 1 per second).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

import httpx

log = logging.getLogger("transit_mcp.geocode")
BASE = "https://geocode.maps.co"
# Covers the area the site's agencies serve (Fredericksburg to Baltimore, the Eastern
# Shore to Winchester).
REGION = {"south": 37.4, "north": 40.1, "west": -79.3, "east": -75.3}
_MIN_INTERVAL_S = 0.25
_CACHE_S = 24 * 3600


def in_region(lat: float, lon: float) -> bool:
    return REGION["south"] <= lat <= REGION["north"] and REGION["west"] <= lon <= REGION["east"]


class Geocoder:
    def __init__(self, api_key: str | None, *, http: httpx.Client | None = None,
                 max_entries: int = 2000):
        self.api_key = api_key
        self.http = http or httpx.Client(timeout=8.0)
        self._cache: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
        self._max = max_entries
        self._lock = threading.Lock()
        self._last = 0.0

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _get(self, path: str, params: dict):
        key = (path, tuple(sorted(params.items())))
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < _CACHE_S:
                self._cache.move_to_end(key)
                return hit[1]
            wait = self._last + _MIN_INTERVAL_S - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        # Key in a header, not the URL, so it never lands in access logs.
        r = self.http.get(f"{BASE}/{path}", params=params,
                          headers={"Authorization": f"Bearer {self.api_key}"})
        r.raise_for_status()
        data = r.json()
        with self._lock:
            self._cache[key] = (time.time(), data)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
        return data

    def search(self, query: str, limit: int = 6) -> list[dict]:
        q = " ".join(query.split())[:200]
        if not self.api_key or len(q) < 3:
            return []
        data = self._get("search", {"q": q})
        out = []
        for item in data if isinstance(data, list) else []:
            try:
                lat, lon = float(item["lat"]), float(item["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if not in_region(lat, lon):
                continue
            out.append({"label": _short_label(item.get("display_name") or q),
                        "full_label": item.get("display_name"),
                        "lat": lat, "lon": lon,
                        "kind": item.get("type") or item.get("class")})
            if len(out) >= limit:
                break
        return out

    def reverse(self, lat: float, lon: float) -> str | None:
        if not self.api_key:
            return None
        data = self._get("reverse", {"lat": f"{lat:.5f}", "lon": f"{lon:.5f}"})
        if isinstance(data, dict) and data.get("display_name"):
            return _short_label(data["display_name"])
        return None


def _short_label(display_name: str) -> str:
    """'Capital Hilton, 1001, 16th Street NW, ..., Washington, DC, 20036, USA' ->
    'Capital Hilton, 16th Street NW, Washington' (first few meaningful parts)."""
    parts = [p.strip() for p in display_name.split(",") if p.strip()]
    keep = [p for p in parts if not p.isdigit() and p not in ("United States",
                                                              "United States of America")]
    return ", ".join(keep[:3]) if keep else display_name
