"""Flight status from FlightAware AeroAPI, for flights to and from IAD, DCA and BWI.

One look-up (``GET /flights/{ident}``, $0.005 a result set on the Standard plan) gives a
flight's scheduled, estimated and actual gate times, terminals, gates and baggage claim.
Answers are cached for 10 minutes per flight, and look-ups stop at a monthly cap
(``FLIGHTAWARE_MONTHLY_DOLLARS``, counted with :class:`ApiBudget`). The API key stays on
the server and never reaches a browser or a log.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

BASE_URL = "https://aeroapi.flightaware.com/aeroapi"
DOLLARS_PER_LOOKUP = 0.005
CACHE_S = 600
# Our airports, by IATA code: their ICAO codes (FlightAware's default) and names.
AIRPORTS = {
    "IAD": {"icao": "KIAD", "name": "Washington Dulles International Airport"},
    "DCA": {"icao": "KDCA", "name": "Ronald Reagan Washington National Airport"},
    "BWI": {"icao": "KBWI", "name": "Baltimore/Washington International Airport"},
}
_ICAO = {a["icao"]: code for code, a in AIRPORTS.items()}
# "UA 123", "ual123", "B6-1234" -> "UA123", "UAL123", "B61234"
_IDENT = re.compile(r"^[A-Z0-9]{2,3}\d{1,5}[A-Z]?$")


class FlightError(Exception):
    """A look-up that didn't work; the message can be shown to the rider."""


class FlightBudgetSpent(FlightError):
    pass


def normalize_ident(text: str) -> str | None:
    """A flight number as FlightAware takes it, or None if it doesn't look like one."""
    ident = re.sub(r"[\s\-]", "", (text or "").upper())
    return ident if _IDENT.match(ident) else None


def _code(airport: dict | None) -> str | None:
    """An airport's IATA code (or its ICAO/other code when it has none)."""
    if not airport:
        return None
    return airport.get("code_iata") or airport.get("code") or airport.get("code_icao")


def _ours(airport: dict | None) -> str | None:
    """IAD, DCA or BWI if this is one of our airports."""
    if not airport:
        return None
    for key in ("code_iata", "code", "code_icao"):
        value = (airport.get(key) or "").upper()
        if value in AIRPORTS:
            return value
        if value in _ICAO:
            return _ICAO[value]
    return None


def _domestic(airport: dict | None) -> bool:
    """US airports: ICAO K (lower 48), P (Alaska, Hawaii, Pacific), TJ/TI (PR, USVI)."""
    icao = ((airport or {}).get("code_icao") or (airport or {}).get("code") or "").upper()
    return icao[:1] in ("K", "P") or icao[:2] in ("TJ", "TI")


def _when(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _zone(airport: dict, fallback: ZoneInfo) -> ZoneInfo:
    try:
        return ZoneInfo(airport.get("timezone") or "")
    except (KeyError, ValueError):
        return fallback


def summarize(f: dict, tz: ZoneInfo) -> dict:
    """The parts of a FlightAware flight a rider needs. Departure times are in the origin
    airport's local time and arrival times in the destination's (with their offsets)."""
    origin, destination = f.get("origin") or {}, f.get("destination") or {}
    out_tz, in_tz = _zone(origin, tz), _zone(destination, tz)

    def local(key: str, zone: ZoneInfo) -> str | None:
        t = _when(f.get(key))
        return t.astimezone(zone).isoformat(timespec="minutes") if t else None

    return {
        "flight": f.get("ident_iata") or f.get("ident"),
        "airline": f.get("operator_iata") or f.get("operator"),
        "status": f.get("status"),
        "cancelled": bool(f.get("cancelled")),
        "diverted": bool(f.get("diverted")),
        "origin": {"code": _code(origin), "name": origin.get("name"), "city": origin.get("city"),
                   "ours": _ours(origin)},
        "destination": {"code": _code(destination), "name": destination.get("name"),
                        "city": destination.get("city"), "ours": _ours(destination)},
        "international": not (_domestic(origin) and _domestic(destination)),
        "departure": {"scheduled": local("scheduled_out", out_tz),
                      "estimated": local("estimated_out", out_tz),
                      "actual": local("actual_out", out_tz), "terminal": f.get("terminal_origin"),
                      "gate": f.get("gate_origin")},
        "arrival": {"scheduled": local("scheduled_in", in_tz),
                    "estimated": local("estimated_in", in_tz),
                    "actual": local("actual_in", in_tz), "terminal": f.get("terminal_destination"),
                    "gate": f.get("gate_destination"), "baggage_claim": f.get("baggage_claim")},
        "departure_delay_min": round((f.get("departure_delay") or 0) / 60),
        "arrival_delay_min": round((f.get("arrival_delay") or 0) / 60),
    }


def pick(flights: list[dict], now: datetime) -> dict | None:
    """The flight a rider means: through IAD, DCA or BWI, and the soonest one that hasn't
    arrived yet (else the latest that has)."""
    ours = [f for f in flights if _ours(f.get("origin")) or _ours(f.get("destination"))]
    if not ours:
        return None

    def gate_out(f):
        return _when(f.get("estimated_out") or f.get("scheduled_out")) or now

    upcoming = [f for f in ours if not f.get("actual_in") and not f.get("cancelled")
                and gate_out(f) >= now - timedelta(hours=12)]
    if upcoming:
        return min(upcoming, key=gate_out)
    return max(ours, key=gate_out)


class FlightClient:
    def __init__(self, api_key: str | None, *, http: httpx.Client | None = None, budget=None,
                 timezone: str = "America/New_York", clock=time.time):
        self.api_key = api_key
        self.http = http or httpx.Client(timeout=10.0)
        self.budget = budget            # ApiBudget or None (not metered)
        self.tz = ZoneInfo(timezone)
        self._clock = clock
        self._cache: dict[str, tuple[float, dict | None]] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings, *, http: httpx.Client | None = None) -> FlightClient:
        from transit_mcp.api_budget import ApiBudget

        lookups = int(settings.flightaware_monthly_dollars / DOLLARS_PER_LOOKUP)
        budget = ApiBudget(service="flightaware", monthly_limit=lookups,
                           timezone=settings.timezone, db_path=settings.db_path)
        return cls(settings.flightaware_api_key, http=http, budget=budget,
                   timezone=settings.timezone)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def status(self, flight: str) -> dict | None:
        """The rider's flight through IAD, DCA or BWI (None if none matches)."""
        if not self.available:
            raise FlightError("Flight tracking isn't set up on this site.")
        ident = normalize_ident(flight)
        if ident is None:
            raise FlightError("That doesn't look like a flight number. Please give the "
                              "airline code and number, like UA 123 or AA2045.")
        now = datetime.now(UTC)
        with self._lock:
            hit = self._cache.get(ident)
            if hit and self._clock() - hit[0] < CACHE_S:
                return hit[1]
        if self.budget is not None and not self.budget.try_spend():
            raise FlightBudgetSpent("This month's flight look-ups are used up. Please check "
                                    "the flight with your airline or the airport.")
        params = {"start": (now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "end": (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "max_pages": 1}
        try:
            r = self.http.get(f"{BASE_URL}/flights/{ident}", params=params,
                              headers={"x-apikey": self.api_key, "Accept": "application/json"})
        except httpx.HTTPError:
            raise FlightError("Flight status isn't responding. Please try again in a "
                              "moment.") from None
        if r.status_code == 404:
            found = None
        elif r.status_code != 200:
            raise FlightError("Flight status isn't available right now. Please try again "
                              "in a moment.")
        else:
            chosen = pick(r.json().get("flights") or [], now)
            found = summarize(chosen, self.tz) if chosen else None
        with self._lock:
            self._cache[ident] = (self._clock(), found)
            if len(self._cache) > 2000:
                self._cache.pop(next(iter(self._cache)))
        return found
