"""Flights at IAD, DCA and BWI: FlightAware look-ups (flights.py) and the assistant's
flight tools (chat/trip_tools.py)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from transit_mcp.chat import trip_tools
from transit_mcp.flights import (
    FlightBudgetSpent,
    FlightClient,
    FlightError,
    normalize_ident,
    pick,
    summarize,
)
from transit_mcp.planning import itinerary

TZ = ZoneInfo("America/New_York")
NOW = datetime.now(UTC)


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def _flight(**over) -> dict:
    """A FlightAware flight DCA -> ORD leaving in 5 hours."""
    out = NOW + timedelta(hours=5)
    f = {"ident": "UAL123", "ident_iata": "UA123", "operator_iata": "UA", "status": "Scheduled",
         "cancelled": False, "diverted": False,
         "origin": {"code": "KDCA", "code_iata": "DCA", "code_icao": "KDCA",
                    "name": "Reagan National", "city": "Washington",
                    "timezone": "America/New_York"},
         "destination": {"code": "KORD", "code_iata": "ORD", "code_icao": "KORD",
                         "name": "O'Hare Intl", "city": "Chicago",
                         "timezone": "America/Chicago"},
         "scheduled_out": _iso(out), "estimated_out": _iso(out + timedelta(minutes=20)),
         "actual_out": None, "scheduled_in": _iso(out + timedelta(hours=2)),
         "estimated_in": None, "actual_in": None, "terminal_origin": "2", "gate_origin": "35",
         "terminal_destination": "1", "gate_destination": "B12", "baggage_claim": "7",
         "departure_delay": 1200, "arrival_delay": 0}
    f.update(over)
    return f


def test_flight_numbers_are_tidied_and_checked():
    assert normalize_ident(" ua 123 ") == "UA123"
    assert normalize_ident("B6-1234") == "B61234"
    assert normalize_ident("UAL123") == "UAL123"
    assert normalize_ident("hello world") is None and normalize_ident("") is None


def test_summary_uses_each_airports_own_time():
    info = summarize(_flight(), TZ)
    assert info["flight"] == "UA123" and info["origin"]["ours"] == "DCA"
    assert info["destination"]["ours"] is None and not info["international"]
    assert info["departure"]["estimated"].endswith(("-05:00", "-04:00"))       # Eastern
    assert info["arrival"]["scheduled"].endswith(("-06:00", "-05:00"))         # Central
    assert info["departure"]["gate"] == "35" and info["arrival"]["baggage_claim"] == "7"
    assert info["departure_delay_min"] == 20
    intl = summarize(_flight(destination={"code_iata": "LHR", "code_icao": "EGLL"}), TZ)
    assert intl["international"]


def test_pick_prefers_the_next_flight_through_our_airports():
    elsewhere = _flight(origin={"code_iata": "SFO", "code_icao": "KSFO"})
    landed = _flight(scheduled_out=_iso(NOW - timedelta(hours=10)),
                     actual_in=_iso(NOW - timedelta(hours=7)))
    tomorrow = _flight(scheduled_out=_iso(NOW + timedelta(hours=29)), estimated_out=None)
    soon = _flight()
    assert pick([elsewhere, tomorrow, landed, soon], NOW) is soon
    assert pick([landed], NOW) is landed
    assert pick([elsewhere], NOW) is None


class _Budget:
    def __init__(self, left):
        self.left, self.limit = left, left

    def try_spend(self):
        self.left -= 1
        return self.left >= 0


def test_client_caches_and_stops_at_its_budget():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"flights": [_flight()], "links": None, "num_pages": 1})

    client = FlightClient("KEY", http=httpx.Client(transport=httpx.MockTransport(handler)),
                          budget=_Budget(1))
    info = client.status("ua 123")
    assert info["flight"] == "UA123"
    assert seen[0].url.path.endswith("/flights/UA123")
    assert seen[0].headers["x-apikey"] == "KEY" and "KEY" not in str(seen[0].url)
    assert client.status("UA123") == info and len(seen) == 1                   # cached
    with pytest.raises(FlightBudgetSpent):
        client.status("AA100")                                                  # cap reached
    with pytest.raises(FlightError, match="flight number"):
        client.status("not a flight")
    assert not FlightClient(None).available


# ----------------------------------------------------------------- the assistant's tools
class _Flights:
    def __init__(self, info):
        self.info = info

    def status(self, flight):
        if self.info is None:
            return None
        return self.info


class _NullDb:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def tools(tmp_path, monkeypatch):
    t = trip_tools.TripTools.__new__(trip_tools.TripTools)
    t.settings = SimpleNamespace(timezone="America/New_York", db_path=tmp_path / "t.db",
                                 trip_walk_m_per_min=70, flight_before_domestic_min=120,
                                 flight_before_international_min=180,
                                 flight_after_domestic_min=30, flight_after_international_min=60)
    t.tz = TZ
    t.accounts = None
    t.user = None
    t.events = []
    t._state = SimpleNamespace(flights=None)
    t._trips = lambda: SimpleNamespace(geocoder=SimpleNamespace(search=lambda q: [
        {"lat": 38.9026, "lon": -77.0365, "label": "Capital Hilton",
         "full_label": "1001 16th St NW, Washington, DC"}]), agencies={}, http=None)
    seen = []

    def fake_route(db, settings, agencies, origin, dest, day, *, arrive_by=None,
                   leave_at=None, now=None):
        seen.append((origin.label, dest.label, day, arrive_by, leave_at))
        return {"status": "ok", "from": origin.label, "to": dest.label}

    monkeypatch.setattr(itinerary, "route", fake_route)
    monkeypatch.setattr(trip_tools, "Database", lambda path: _NullDb())
    t.seen = seen
    yield t


def _call(t, tool, **args):
    text, is_error, changed = asyncio.run(t.call(tool, args))
    return json.loads(text), is_error


def test_track_flight_gives_the_time_to_be_at_the_airport(tools):
    tools._state.flights = _Flights(summarize(_flight(), TZ))
    result, is_error = _call(tools, "track_flight", flight="UA123")
    assert not is_error and result["source"] == "FlightAware"
    g = result["guidance"]
    assert g["kind"] == "departure" and g["airport"] == "DCA" and g["minutes_before"] == 120
    leaves = datetime.fromisoformat(result["flight"]["departure"]["estimated"])
    assert datetime.fromisoformat(g["be_at_airport_by"]) == leaves - timedelta(minutes=120)
    (card,) = tools.events
    assert card["type"] == "flight" and card["flight"]["flight"] == "UA123"


def test_get_to_flight_plans_to_the_terminal_by_that_time(tools):
    tools._state.flights = _Flights(summarize(_flight(), TZ))
    result, _ = _call(tools, "get_to_flight", flight="UA123", place="Capital Hilton")
    origin, dest, day, arrive_by, leave_at = tools.seen[-1]
    assert origin == "Capital Hilton" and "Reagan" in dest and arrive_by and leave_at is None
    assert result["plan"]["status"] == "ok" and result["airport_directions"]["source"]
    # Five hours away (be there in three): no live card yet.
    assert [e["type"] for e in tools.events] == ["flight"]


def test_arrivals_plan_from_the_terminal_after_landing(tools):
    landing = NOW + timedelta(minutes=40)
    arrival = _flight(origin={"code_iata": "LHR", "code_icao": "EGLL", "timezone": "Europe/London"},
                      destination={"code_iata": "IAD", "code_icao": "KIAD",
                                   "timezone": "America/New_York"},
                      scheduled_out=_iso(NOW - timedelta(hours=7)),
                      actual_out=_iso(NOW - timedelta(hours=7)),
                      scheduled_in=_iso(landing), estimated_in=_iso(landing))
    tools._state.flights = _Flights(summarize(arrival, TZ))
    result, _ = _call(tools, "get_to_flight", flight="BA293", place="Capital Hilton")
    g = result["guidance"]
    assert g["kind"] == "arrival" and g["airport"] == "IAD" and g["minutes_after"] == 60
    origin, dest, _, arrive_by, leave_at = tools.seen[-1]
    assert "Dulles" in origin and dest == "Capital Hilton" and leave_at and arrive_by is None
    # Ready to leave within the hour and 40 minutes: the live options go on the map too.
    assert [e["type"] for e in tools.events] == ["flight", "show_trip"]
    assert tools.events[1]["to"]["label"] == "Capital Hilton"


def test_cancelled_or_unknown_flights_say_so(tools):
    tools._state.flights = _Flights(summarize(_flight(cancelled=True), TZ))
    result, _ = _call(tools, "get_to_flight", flight="UA123", place="Capital Hilton")
    assert result["guidance"] is None and result["plan"]["status"] == "none"
    tools._state.flights = _Flights(None)
    result, is_error = _call(tools, "track_flight", flight="UA999")
    assert is_error and result["error"] == "flight_not_found"


def test_flight_tool_inputs_stay_out_of_the_chat_log():
    assert {"track_flight", "get_to_flight"} <= trip_tools.NAMES
