"""Addresses and place names -> map points (server-side only).

Two providers, picked by which key is set (Google first):

- Google Geocoding API (``GOOGLE_GEOCODING_API_KEY``) — billed per request, so every
  request is counted against ``GOOGLE_GEOCODING_MONTHLY_BUDGET`` first. Google only
  takes the key in the URL; it never appears in errors or logs from here.
- geocode.maps.co (``GEOCODE_API_KEY``) — the key goes in a header.

Keys never reach the browser. Results are limited to the DMV/Baltimore region, cached
for a day, and requests are spaced out.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import OrderedDict

import httpx

log = logging.getLogger("transit_mcp.geocode")
BASE = "https://geocode.maps.co"
GOOGLE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
# Covers the area the site's agencies serve (Fredericksburg to Baltimore, the Eastern
# Shore to Winchester).
REGION = {"south": 37.4, "north": 40.1, "west": -79.3, "east": -75.3}
_MIN_INTERVAL_S = {"mapsco": 0.25, "google": 0.05}
_CACHE_S = 24 * 3600

# Google result types: what names a place (vs. an address) and what says nothing.
_NAMED = {"establishment", "point_of_interest", "airport", "park", "natural_feature",
          "transit_station", "train_station", "subway_station", "bus_station",
          "light_rail_station", "university", "stadium", "museum", "tourist_attraction",
          "colloquial_area"}
_GENERIC = {"political", "establishment", "point_of_interest", "geocode"}
_STATE_ZIP = re.compile(r"^[A-Z]{2}(?: \d{5}(?:-\d{4})?)?$")
_SUITE = re.compile(r"\s+(?:#|Suite |Ste |Apt |Unit )\s*\S+$")   # '1601 K St NW # 1'


class GeocodeError(Exception):
    """A failed lookup. The message is safe to log: no key, no URL."""


class BudgetSpent(GeocodeError):
    """This month's paid geocoding requests are used up."""


def in_region(lat: float, lon: float) -> bool:
    return REGION["south"] <= lat <= REGION["north"] and REGION["west"] <= lon <= REGION["east"]


class Geocoder:
    def __init__(self, api_key: str | None, *, provider: str = "mapsco",
                 http: httpx.Client | None = None, budget=None, max_entries: int = 2000):
        if provider not in _MIN_INTERVAL_S:
            raise ValueError(f"unknown geocoder {provider!r}")
        self.api_key = api_key
        self.provider = provider
        self.budget = budget            # ApiBudget or None (not metered)
        self.http = http or httpx.Client(timeout=8.0)
        self._cache: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
        self._max = max_entries
        self._lock = threading.Lock()
        self._last = 0.0

    @classmethod
    def from_settings(cls, settings, *, http: httpx.Client | None = None) -> Geocoder:
        """Google if its key is set, else geocode.maps.co (or nothing, if neither)."""
        if settings.google_geocoding_api_key:
            from transit_mcp.api_budget import ApiBudget

            budget = ApiBudget(service="google_geocoding",
                               monthly_limit=settings.google_geocoding_monthly_budget,
                               timezone=settings.timezone, db_path=settings.db_path)
            return cls(settings.google_geocoding_api_key, provider="google", http=http,
                       budget=budget)
        return cls(settings.geocode_api_key, http=http)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    @property
    def attribution(self) -> str | None:
        """Credit to show next to results (Google requires it without a Google map)."""
        return "Google Maps" if self.provider == "google" else None

    def _get(self, path: str, params: dict):
        key = (self.provider, path, tuple(sorted(params.items())))
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < _CACHE_S:
                self._cache.move_to_end(key)
                return hit[1]
        if self.budget is not None and not self.budget.try_spend():
            raise BudgetSpent(f"{self.provider}: monthly budget of {self.budget.limit:,} "
                              "requests used")
        with self._lock:
            wait = self._last + _MIN_INTERVAL_S[self.provider] - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        data = self._fetch(path, params)
        with self._lock:
            self._cache[key] = (time.time(), data)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
        return data

    def _fetch(self, path: str, params: dict):
        if self.provider == "google":
            url, params, headers = GOOGLE_URL, {**params, "key": self.api_key}, None
        else:
            # Key in a header, not the URL, so it never lands in access logs.
            url, headers = f"{BASE}/{path}", {"Authorization": f"Bearer {self.api_key}"}
        try:
            r = self.http.get(url, params=params, headers=headers)
            r.raise_for_status()
            data = r.json()
        # httpx errors carry the request URL (with Google's key): keep only the kind.
        except httpx.HTTPStatusError as exc:
            raise GeocodeError(f"{self.provider}: HTTP {exc.response.status_code}") from None
        except (httpx.HTTPError, ValueError) as exc:
            raise GeocodeError(f"{self.provider}: {type(exc).__name__}") from None
        if self.provider == "google":
            status = data.get("status") if isinstance(data, dict) else None
            if status == "ZERO_RESULTS":
                return {"results": []}
            if status != "OK":
                raise GeocodeError(f"google: {status} {data.get('error_message') or ''}"
                                   .strip() if isinstance(data, dict) else "google: bad reply")
        return data

    def search(self, query: str, limit: int = 6) -> list[dict]:
        q = " ".join(query.split())[:200]
        if not self.api_key or len(q) < 3:
            return []
        if self.provider == "google":
            data = self._get("search", {
                "address": q, "region": "us", "components": "country:US",
                "bounds": f"{REGION['south']},{REGION['west']}|{REGION['north']},{REGION['east']}"})
            items = [_google_item(r) for r in data.get("results", [])]
        else:
            data = self._get("search", {"q": q})
            items = [_mapsco_item(r, q) for r in (data if isinstance(data, list) else [])]
        out = []
        for item in items:
            if item is None or not in_region(item["lat"], item["lon"]):
                continue
            out.append(item)
            if len(out) >= limit:
                break
        return out

    def reverse(self, lat: float, lon: float) -> str | None:
        if not self.api_key:
            return None
        if self.provider == "google":
            data = self._get("reverse", {"latlng": f"{lat:.5f},{lon:.5f}"})
            for r in data.get("results", []):
                item = _google_item(r)
                if item:
                    return item["label"]
            return None
        data = self._get("reverse", {"lat": f"{lat:.5f}", "lon": f"{lon:.5f}"})
        if isinstance(data, dict) and data.get("display_name"):
            return _short_label(data["display_name"])
        return None


def _mapsco_item(item: dict, q: str) -> dict | None:
    try:
        lat, lon = float(item["lat"]), float(item["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    return {"label": _short_label(item.get("display_name") or q),
            "full_label": item.get("display_name"),
            "lat": lat, "lon": lon,
            "kind": item.get("type") or item.get("class")}


def _google_item(r: dict) -> dict | None:
    """One Google result -> a suggestion: 'Union Station, Washington' over its street
    address when the result names a place; the address otherwise."""
    try:
        loc = r["geometry"]["location"]
        lat, lon = float(loc["lat"]), float(loc["lng"])
    except (KeyError, TypeError, ValueError):
        return None
    types = r.get("types") or []
    comps = r.get("address_components") or []

    def part(*wanted):
        for t in wanted:
            for c in comps:
                if t in (c.get("types") or []):
                    return c.get("long_name")
        return None

    address = _google_short(r.get("formatted_address") or "")
    name = None
    if _NAMED & set(types):
        name = next((c.get("long_name") for c in comps
                     if _NAMED & set(c.get("types") or [])), None)
    town = part("locality", "sublocality", "neighborhood", "administrative_area_level_2")
    if name:
        label = f"{name}, {town}" if town and town not in name else name
        # 'Ronald Reagan ... Airport (DCA), 2401 Smith Blvd, Arlington': name once only.
        rest = address.split(", ")
        if rest and rest[0].startswith(name):
            rest = rest[1:]
        sub = ", ".join(rest) if rest and ", ".join(rest) != label else ""
    else:
        label, sub = address or town or "", ""
    if not label:
        return None
    specific = [t for t in types if t not in _GENERIC]
    return {"label": label, "full_label": r.get("formatted_address"), "sub": sub,
            "lat": lat, "lon": lon, "kind": (specific or types or [None])[0]}


def _google_short(formatted: str) -> str:
    """'50 Massachusetts Ave NE, Washington, DC 20002, USA' ->
    '50 Massachusetts Ave NE, Washington' (drops the state, ZIP code and country)."""
    parts = [_SUITE.sub("", p).strip() for p in formatted.split(",") if p.strip()]
    keep = [p for p in parts if p and p not in ("USA", "United States")
            and not _STATE_ZIP.match(p)]
    return ", ".join(keep[:3])


def _short_label(display_name: str) -> str:
    """'Capital Hilton, 1001, 16th Street NW, ..., Washington, DC, 20036, USA' ->
    'Capital Hilton, 16th Street NW, Washington' (first few meaningful parts)."""
    parts = [p.strip() for p in display_name.split(",") if p.strip()]
    keep = [p for p in parts if not p.isdigit() and p not in ("United States",
                                                              "United States of America")]
    return ", ".join(keep[:3]) if keep else display_name


PLACES_URL = "https://places.googleapis.com/v1"
_PLACE_ID = re.compile(r"^[A-Za-z0-9_-]{10,400}$")
SESSION_TOKEN = re.compile(r"^[A-Za-z0-9-]{8,64}$")
# Asking for the name (a "Pro" field) when a place is picked makes the typing requests
# before it in the same session free (Google's session pricing).
_DETAIL_FIELDS = "id,displayName,formattedAddress,location,types"


class PlaceSearch:
    """Google Places Autocomplete (New) for the address box, and Place Details for the
    suggestion a rider picks (suggestions carry no coordinates). Same key as the
    Geocoding API, sent in a header; every request counts against
    ``GOOGLE_PLACES_MONTHLY_BUDGET``. A browser-made session token ties one round of
    typing to its pick."""

    def __init__(self, api_key: str | None, *, http: httpx.Client | None = None,
                 budget=None, max_entries: int = 2000):
        self.api_key = api_key
        self.budget = budget
        self.http = http or httpx.Client(timeout=8.0)
        self._cache: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
        self._max = max_entries
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings, *, http: httpx.Client | None = None) -> PlaceSearch:
        budget = None
        if settings.google_geocoding_api_key:
            from transit_mcp.api_budget import ApiBudget

            budget = ApiBudget(service="google_places",
                               monthly_limit=settings.google_places_monthly_budget,
                               timezone=settings.timezone, db_path=settings.db_path)
        return cls(settings.google_geocoding_api_key, http=http, budget=budget)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _call(self, method: str, path: str, *, cache_key: tuple, ttl: int,
              body: dict | None = None, params: dict | None = None,
              fields: str | None = None):
        with self._lock:
            hit = self._cache.get(cache_key)
            if hit and time.time() - hit[0] < ttl:
                self._cache.move_to_end(cache_key)
                return hit[1]
        if self.budget is not None and not self.budget.try_spend():
            raise BudgetSpent(f"places: monthly budget of {self.budget.limit:,} requests used")
        headers = {"X-Goog-Api-Key": self.api_key}
        if fields:
            headers["X-Goog-FieldMask"] = fields
        try:
            r = self.http.request(method, f"{PLACES_URL}{path}", json=body, params=params,
                                  headers=headers)
            if r.status_code >= 400:
                try:
                    status = r.json().get("error", {}).get("status") or ""
                except ValueError:
                    status = ""
                raise GeocodeError(f"places: HTTP {r.status_code} {status}".strip())
            data = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GeocodeError(f"places: {type(exc).__name__}") from None
        with self._lock:
            self._cache[cache_key] = (time.time(), data)
            while len(self._cache) > self._max:
                self._cache.popitem(last=False)
        return data

    def autocomplete(self, query: str, session: str | None = None,
                     limit: int = 6) -> list[dict]:
        q = " ".join(query.split())[:200]
        if not self.api_key or len(q) < 3:
            return []
        body = {"input": q, "includedRegionCodes": ["us"], "languageCode": "en",
                "locationRestriction": {"rectangle": {
                    "low": {"latitude": REGION["south"], "longitude": REGION["west"]},
                    "high": {"latitude": REGION["north"], "longitude": REGION["east"]}}}}
        if session and SESSION_TOKEN.match(session):
            body["sessionToken"] = session
        # Suggestions change little: a short cache spares repeat typing of the same text.
        data = self._call("POST", "/places:autocomplete", cache_key=("ac", q.lower()),
                          ttl=600, body=body)
        out = []
        for s in data.get("suggestions") or []:
            p = s.get("placePrediction")
            if not p or not p.get("placeId"):
                continue
            fmt = p.get("structuredFormat") or {}
            main = ((fmt.get("mainText") or {}).get("text")
                    or (p.get("text") or {}).get("text") or "")
            sub = (fmt.get("secondaryText") or {}).get("text") or ""
            sub = re.sub(r",?\s*(USA|United States)$", "", sub).strip()
            types = p.get("types") or []
            specific = [t for t in types if t not in _GENERIC]
            out.append({"label": main, "sub": sub, "place_id": p["placeId"],
                        "full_label": (p.get("text") or {}).get("text"),
                        "kind": (specific or types or [None])[0]})
            if len(out) >= limit:
                break
        return out

    def details(self, place_id: str, session: str | None = None) -> dict | None:
        """Where a picked suggestion is: lat/lon, a label, its kind (None if unknown or
        outside the region)."""
        if not self.api_key or not _PLACE_ID.match(place_id or ""):
            return None
        params = {"sessionToken": session} if session and SESSION_TOKEN.match(session) else None
        data = self._call("GET", f"/places/{place_id}", cache_key=("pd", place_id),
                          ttl=_CACHE_S, params=params, fields=_DETAIL_FIELDS)
        try:
            lat = float(data["location"]["latitude"])
            lon = float(data["location"]["longitude"])
        except (KeyError, TypeError, ValueError):
            return None
        if not in_region(lat, lon):
            return None
        types = data.get("types") or []
        specific = [t for t in types if t not in _GENERIC]
        address = _google_short(data.get("formattedAddress") or "")
        name = (data.get("displayName") or {}).get("text")
        named = bool(_NAMED & set(types))
        return {"lat": lat, "lon": lon, "label": (name if named and name else address or name),
                "full_label": data.get("formattedAddress"),
                "kind": (specific or types or [None])[0]}
