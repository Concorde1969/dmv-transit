"""The assistant's trip planning: tools for the signed-in rider, Places search, arriving
by a time, and the chat loop running these tools (chat/trip_tools.py)."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest

from tests.test_chat_agent_loop import (
    _FakeBridge,
    _FakeClient,
    _FakeStream,
    _text_block,
    _tool_use_block,
)
from transit_mcp.accounts import AccountStore, PlaceInput
from transit_mcp.chat import trip_tools
from transit_mcp.chat.agent import run_chat
from transit_mcp.geocode import BudgetSpent, PlaceSearch
from transit_mcp.planning import itinerary
from transit_mcp.planning.live import Point

TZ = ZoneInfo("America/New_York")
TODAY = datetime.now(TZ).date()
SAT = (TODAY + timedelta(days=5)).isoformat()
HOTEL = (38.8977, -77.0060)          # near Union Station
MUSEUM = (38.8882, -77.0199)         # Air and Space Museum


class FakeGeocoder:
    def search(self, q):
        return [{"lat": 38.8893, "lon": -76.9803, "label": "1717 E Capitol St SE",
                 "full_label": "1717 E Capitol St SE, Washington, DC"}] if "capitol" in q.lower() else []


class FakePlaces:
    available = True

    def __init__(self):
        self.asked, self.error = [], None

    def search(self, query, *, near=None, open_now=False):
        self.asked.append((query, near, open_now))
        if self.error:
            raise self.error
        return [{"name": "Good Lunch", "point": "38.889000,-77.020000", "rating": 4.6,
                 "price": "$$", "open_now": True, "url": "https://maps.google.com/?cid=1"}]


@pytest.fixture
def tools(tmp_path):
    store = AccountStore(tmp_path / "a.db")
    user, _, _ = store.register("tina", "a long walk to the metro")
    t = trip_tools.TripTools.__new__(trip_tools.TripTools)
    t.settings = SimpleNamespace(timezone="America/New_York", db_path=tmp_path / "t.db",
                                 trip_walk_m_per_min=70)
    t.tz = TZ
    t.accounts = SimpleNamespace(store=store)
    t.user = user
    places = FakePlaces()
    t._state = SimpleNamespace(places_search=places)
    t._trips = lambda: SimpleNamespace(geocoder=FakeGeocoder(), agencies={}, http=None)
    t.places = places
    t.events = []
    yield t
    store.close()


def call(t, tool, **args):
    text, is_error, changed = asyncio.run(t.call(tool, args))
    return json.loads(text), is_error, changed


def test_every_tool_asks_signed_out_riders_to_sign_in(tools):
    tools.user = None
    for spec in trip_tools.TOOLS:
        if spec["name"] == "show_trip":          # showing a trip works for everyone
            continue
        args = {k: "x" for k in spec["input_schema"].get("required", [])}
        if spec["name"] in ("remove_saved_place",):
            args = {"saved_place_id": 1}
        if spec["name"] in ("change_plan_stop", "remove_from_plan"):
            args = {"stop_id": 1}
        result, is_error, changed = call(tools, spec["name"], **args)
        assert result["error"] == "sign_in_required", spec["name"]
        assert is_error and not changed


def test_find_places_near_a_saved_place(tools):
    call(tools, "save_place", name="My hotel", kind="hotel", where=f"{HOTEL[0]},{HOTEL[1]}")
    result, is_error, changed = call(tools, "find_places", query="lunch", near="my hotel",
                                     open_now=True)
    assert not is_error and not changed
    assert result["source"] == "Google Maps" and result["results"][0]["name"] == "Good Lunch"
    assert tools.places.asked == [("lunch", HOTEL, True)]


def test_find_places_explains_a_spent_budget(tools):
    tools.places.error = BudgetSpent("spent")
    result, is_error, _ = call(tools, "find_places", query="pizza")
    assert result["error"] == "search_paused" and is_error


def test_save_place_and_plan_a_day(tools):
    saved, _, changed = call(tools, "save_place", name="My hotel", kind="hotel",
                             where=f"{HOTEL[0]},{HOTEL[1]}", start_date=SAT,
                             end_date=(TODAY + timedelta(days=7)).isoformat())
    assert changed and saved["saved"]["kind"] == "hotel"
    late, _, _ = call(tools, "add_to_plan", day=SAT, name="Dinner", where="1717 E Capitol St SE",
                      arrive_by="19:00")
    early, _, _ = call(tools, "add_to_plan", day=SAT, name="Museum",
                       where=f"{MUSEUM[0]},{MUSEUM[1]}", arrive_by="9:30", leave_at="12:00",
                       notes="free timed passes")
    assert early["added"]["arrive_by"] == "09:30" and late["added"]["address"]
    trip, _, _ = call(tools, "my_trip")
    (day,) = trip["plan"]
    assert day["day"] == SAT and [s["name"] for s in day["stops"]] == ["Museum", "Dinner"]
    assert trip["saved_places"][0]["name"] == "My hotel"


def test_plan_rules(tools):
    for args, words in ((dict(day="2020-01-01"), "passed"), (dict(day="soon"), "dates like"),
                        (dict(day=SAT, arrive_by="7pm"), "HH:MM"),
                        (dict(day=SAT, arrive_by="14:00", leave_at="13:00"), "before")):
        result, is_error, changed = call(tools, "add_to_plan", name="X",
                                         where=f"{MUSEUM[0]},{MUSEUM[1]}", **args)
        assert words in result["message"] and is_error and not changed
    result, _, _ = call(tools, "add_to_plan", day=SAT, name="X", where="Nowhere at all")
    assert result["error"] == "place_not_found"


def test_change_and_remove_plan_stops(tools):
    stop = call(tools, "add_to_plan", day=SAT, name="Museum", where=f"{MUSEUM[0]},{MUSEUM[1]}",
                arrive_by="09:30")[0]["added"]
    sunday = (date.fromisoformat(SAT) + timedelta(days=1)).isoformat()
    changed = call(tools, "change_plan_stop", stop_id=stop["id"], day=sunday, arrive_by="",
                   leave_at="15:00")[0]["changed"]
    assert changed["day"] == sunday and changed["arrive_by"] is None and changed["leave_at"] == "15:00"
    assert call(tools, "remove_from_plan", stop_id=stop["id"])[0] == {"removed": stop["id"]}
    assert call(tools, "remove_from_plan", stop_id=stop["id"])[0]["error"] == "not_found"


def test_directions_start_at_the_hotel_and_chain_the_times(tools, monkeypatch):
    seen = []

    def fake_route(db, settings, agencies, origin, dest, day, *, arrive_by=None, leave_at=None):
        seen.append((origin.label, dest.label, arrive_by, leave_at))
        return {"from": origin.label, "to": dest.label, "status": "ok"}

    monkeypatch.setattr(itinerary, "route", fake_route)
    monkeypatch.setattr(trip_tools, "Database", lambda path: _NullDb())
    result = call(tools, "directions_for_day", day=SAT)[0]
    assert result["error"] == "no_stops"
    call(tools, "add_to_plan", day=SAT, name="Museum", where=f"{MUSEUM[0]},{MUSEUM[1]}",
         arrive_by="09:30", leave_at="12:00")
    call(tools, "add_to_plan", day=SAT, name="Lunch", where="1717 E Capitol St SE")
    assert call(tools, "directions_for_day", day=SAT)[0]["error"] == "no_start"
    call(tools, "save_place", name="My hotel", kind="hotel", where=f"{HOTEL[0]},{HOTEL[1]}",
         start_date=SAT)
    result = call(tools, "directions_for_day", day=SAT, start_time="8:15")[0]
    assert [leg["stop_id"] for leg in result["legs"]] and "timetable" in result["note"]
    assert seen[-2:] == [("My hotel", "Museum", "09:30", None),     # arrive by the first stop
                         ("Museum", "Lunch", None, "12:00")]        # then leave when it ends


class _NullDb:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ------------------------------------------------------------------------- arrive by
def _opt(leave_h, leave_m, arrive_h, arrive_m, day):
    at = lambda h, m: datetime(day.year, day.month, day.day, h, m, tzinfo=TZ).timestamp()  # noqa: E731
    return {"leave_at": at(leave_h, leave_m), "arrive_at": at(arrive_h, arrive_m),
            "duration_s": 1800, "transfers": 0, "wait_s": 0, "walk_s": 300, "live": False,
            "fare_estimate": 2.25, "legs": []}


def test_arrive_by_picks_the_latest_trip_that_is_in_time(monkeypatch):
    day = TODAY + timedelta(days=3)
    opts = [_opt(8, 30, 9, 0, day), _opt(9, 0, 9, 25, day), _opt(9, 10, 9, 40, day)]
    monkeypatch.setattr(itinerary, "_planner", lambda *a: SimpleNamespace(
        plan=lambda o, d: {"options": opts}))
    settings = SimpleNamespace(timezone="America/New_York", trip_walk_m_per_min=70)
    far = Point(38.99, -77.10, "Far"), Point(38.85, -76.95, "Museum")
    leg = itinerary.route(None, settings, {}, *far, day, arrive_by="09:30")
    # 9:00 -> 9:25 is latest but leaves 5 minutes; 8:30 -> 9:00 leaves a comfortable 30.
    assert leg["status"] == "ok" and leg["option"]["leave_at"] == "8:30 AM"
    assert leg["minutes_to_spare"] == 30 and leg["other_options"] == 1
    leg = itinerary.route(None, settings, {}, *far, day, arrive_by="09:27")
    assert leg["option"]["leave_at"] == "8:30 AM"
    leg = itinerary.route(None, settings, {}, *far, day, arrive_by="09:08")
    assert leg["option"]["leave_at"] == "8:30 AM" and leg["minutes_to_spare"] == 8
    leg = itinerary.route(None, settings, {}, *far, day, arrive_by="08:45")
    assert leg["status"] == "late" and leg["minutes_late"] == 15


def test_short_hops_are_a_walk_and_past_times_are_refused():
    settings = SimpleNamespace(timezone="America/New_York", trip_walk_m_per_min=70)
    a, b = Point(38.8882, -77.0199, "A"), Point(38.8890, -77.0230, "B")
    leg = itinerary.route(None, settings, {}, a, b, TODAY + timedelta(days=1), arrive_by="10:00")
    assert leg["status"] == "walk" and leg["walk_minutes"] >= 4 and leg["leave_at"]
    far = Point(38.99, -77.10, "Far")
    yesterday = TODAY - timedelta(days=1)
    assert itinerary.route(None, settings, {}, far, b, yesterday, arrive_by="10:00")["status"] == "past"
    assert itinerary.route(None, settings, {}, far, b, TODAY + timedelta(days=1))["status"] == "no_time"


# ---------------------------------------------------------------------------- Places
def test_places_text_search_reads_what_tourists_need():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"places": [
            {"id": "abc", "displayName": {"text": "Ben's Chili Bowl"},
             "formattedAddress": "1213 U St NW, Washington, DC", "location": {
                 "latitude": 38.917, "longitude": -77.029},
             "primaryTypeDisplayName": {"text": "Restaurant"}, "rating": 4.4,
             "userRatingCount": 9000, "priceLevel": "PRICE_LEVEL_INEXPENSIVE",
             "currentOpeningHours": {"openNow": True},
             "regularOpeningHours": {"weekdayDescriptions": ["Monday: 11 AM-11 PM"]},
             "googleMapsUri": "https://maps.google.com/?cid=1", "businessStatus": "OPERATIONAL"},
            {"id": "gone", "displayName": {"text": "Closed Place"}, "location": {
                "latitude": 38.9, "longitude": -77.0}, "businessStatus": "CLOSED_PERMANENTLY"},
            {"id": "far", "displayName": {"text": "Far Away"}, "location": {
                "latitude": 40.7, "longitude": -74.0}}]})

    places = PlaceSearch("KEY", http=httpx.Client(transport=httpx.MockTransport(handler)))
    (hit,) = places.search("chili", near=(38.9, -77.03), open_now=True)
    assert hit["name"] == "Ben's Chili Bowl" and hit["price"] == "$" and hit["open_now"]
    assert hit["point"] == "38.917000,-77.029000" and hit["meters_away"] > 1000
    assert hit["hours"] == ["Monday: 11 AM-11 PM"] and hit["url"].startswith("https://")
    req = seen[0]
    assert req.url.path.endswith("/places:searchText") and req.headers["X-Goog-Api-Key"] == "KEY"
    assert "places.rating" in req.headers["X-Goog-FieldMask"]
    body = json.loads(req.content)
    assert body["openNow"] is True and body["locationBias"]["circle"]["radius"] == 1500.0
    places.search("chili", near=(38.9, -77.03), open_now=True)
    assert len(seen) == 1                                   # cached


# --------------------------------------------------------------------- the chat loop
class _Local:
    names = frozenset({"save_place"})
    definitions = [{"name": "save_place", "description": "", "input_schema": {}}]

    def __init__(self):
        self.calls = []

    async def call(self, name, args):
        self.calls.append((name, args))
        return json.dumps({"saved": {"name": "My hotel"}}), False, True


def test_chat_runs_planning_tools_here_and_tells_the_page():
    final1 = SimpleNamespace(content=[_tool_use_block("t1", "save_place", {"name": "My hotel"})],
                             stop_reason="tool_use", usage=None, model="m")
    final2 = SimpleNamespace(content=[_text_block("Saved your hotel.")], stop_reason="end_turn",
                             usage=None, model="m")
    client = _FakeClient([_FakeStream([], final1), _FakeStream(["Saved your hotel."], final2)])
    bridge, local = _FakeBridge(), _Local()

    async def go():
        return [e async for e in run_chat(client=client, bridge=bridge, model="m",
                                          messages=[{"role": "user", "content": "save it"}],
                                          max_tokens=100, max_iterations=4, local=local,
                                          context="[Context] today")]

    events = asyncio.run(go())
    assert local.calls == [("save_place", {"name": "My hotel"})] and bridge.calls == []
    assert "account_changed" in [e["type"] for e in events]
    sent = client.messages.calls[0]
    assert {t["name"] for t in sent["tools"]} == {"get_fare", "save_place"}
    assert sent["system"][1]["text"] == "[Context] today"
    assert "find_places" in sent["system"][0]["text"]


def test_show_trip_puts_the_trip_on_the_page(tools, monkeypatch):
    tools.user = None
    result, is_error, changed = call(tools, "show_trip", destination="1717 E Capitol St SE")
    assert not is_error and not changed and result["shown"]
    assert result["from"] == "the rider's current location" and "browser" in result["note"]
    (event,) = tools.events
    assert event["type"] == "show_trip" and event["from"] is None
    assert event["to"]["label"] == "1717 E Capitol St SE" and not event["to"]["saved"]
    assert call(tools, "show_trip", destination="nowhere at all")[0]["error"] == "place_not_found"


def test_show_trip_from_a_saved_place_gets_timetable_options(tools, monkeypatch):
    seen = []

    def fake_route(db, settings, agencies, origin, dest, day, *, arrive_by=None, leave_at=None,
                   now=None):
        seen.append((origin.label, dest.label, day, leave_at))
        return {"status": "ok", "option": {"leave_at": "5:04 PM"}}

    monkeypatch.setattr(itinerary, "route", fake_route)
    monkeypatch.setattr(trip_tools, "Database", lambda path: _NullDb())
    call(tools, "save_place", name="My hotel", kind="hotel", where=f"{HOTEL[0]},{HOTEL[1]}",
         address="1 Hotel Way NW")
    tools.events.clear()
    result = call(tools, "show_trip", origin="my hotel", destination=f"{MUSEUM[0]},{MUSEUM[1]}")[0]
    assert result["timetable"]["status"] == "ok" and result["from"]["name"] == "My hotel"
    (event,) = tools.events
    assert event["from"]["saved"] and event["from"]["address"] == "1 Hotel Way NW"
    assert event["to"]["label"] == "The place you chose"        # never bare coordinates
    assert seen and seen[0][0] == "My hotel" and seen[0][2] == TODAY and seen[0][3]


def test_screen_note_describes_the_trip_on_screen():
    note = trip_tools.screen_note({"from": "My hotel", "to": "Union Station", "options": 3,
                                   "selected": "leave 5:04 PM; Red Line\n" + "x" * 2000})
    assert "from My hotel to Union Station, 3 shown" in note and "picked: leave 5:04 PM" in note
    assert len(note) < 1300 and "\n" not in note
    assert trip_tools.screen_note(None) == trip_tools.screen_note({"from": "x"}) == ""
    assert trip_tools.screen_note({"to": ["not", "text"]}) == ""
    now = datetime(2026, 11, 2, 16, 40, tzinfo=TZ)
    assert "Union Station" in trip_tools.context_note(None, TZ, now, screen={"to": "Union Station"})


def test_chat_sends_page_events_right_after_the_tool():
    class Shows(_Local):
        names = frozenset({"show_trip"})
        definitions = [{"name": "show_trip", "description": "", "input_schema": {}}]

        async def call(self, name, args):
            self.events = [{"type": "show_trip", "to": {"label": "Zoo"}, "from": None}]
            return json.dumps({"shown": True}), False, False

    final1 = SimpleNamespace(content=[_tool_use_block("t1", "show_trip", {"destination": "Zoo"})],
                             stop_reason="tool_use", usage=None, model="m")
    final2 = SimpleNamespace(content=[_text_block("On your map.")], stop_reason="end_turn",
                             usage=None, model="m")
    client = _FakeClient([_FakeStream([], final1), _FakeStream(["On your map."], final2)])
    local = Shows()

    async def go():
        return [e async for e in run_chat(client=client, bridge=_FakeBridge(), model="m",
                                          messages=[{"role": "user", "content": "zoo"}],
                                          max_tokens=100, max_iterations=4, local=local)]

    types = [e["type"] for e in asyncio.run(go())]
    assert types.index("show_trip") < types.index("tool_result") < types.index("text")
    assert local.events == []


def test_context_note_gives_the_dates_and_who_is_signed_in():
    now = datetime(2026, 11, 2, 16, 40, tzinfo=TZ)
    note = trip_tools.context_note({"username": "tina"}, TZ, now)
    assert "Monday, November 02, 2026" in note and "Sat Nov 07 = 2026-11-07" in note
    assert "signed in as tina" in note
    assert "NOT signed in" in trip_tools.context_note(None, TZ, now)


def test_planning_tool_inputs_stay_out_of_the_chat_log():
    from transit_mcp.chat import app as app_mod

    assert "trip_tools.NAMES" in open(app_mod.__file__, encoding="utf-8").read()
    assert trip_tools.NAMES >= {"save_place", "add_to_plan", "find_places", "my_trip"}


def test_addresses_never_become_bare_coordinates(tools):
    point = f"{HOTEL[0]},{HOTEL[1]}"
    saved = call(tools, "save_place", name="My hotel", kind="hotel", where=point)[0]["saved"]
    assert saved["address"] is None
    saved = call(tools, "save_place", name="Hilton", kind="hotel", where=point,
                 address="1001 16th St NW, Washington, DC")[0]["saved"]
    assert saved["address"] == "1001 16th St NW, Washington, DC"
    stop = call(tools, "add_to_plan", day=SAT, name="Dinner", where="1717 E Capitol St SE")[0]
    assert stop["added"]["address"] == "1717 E Capitol St SE, Washington, DC"


def test_saved_place_input_reaches_the_store(tools):
    tools.accounts.store.add_place(tools.user["id"], PlaceInput("Home", "home", 38.9, -77.0))
    trip = call(tools, "my_trip")[0]
    assert [p["name"] for p in trip["saved_places"]] == ["Home"]



def test_scripts_are_revalidated_but_fonts_are_cached(isolated_settings, monkeypatch):
    from fastapi.testclient import TestClient

    from transit_mcp.chat import app as app_mod

    settings = isolated_settings.model_copy(update={"anthropic_api_key": "k"})
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    async def nothing(self):
        self._tools = []

    monkeypatch.setattr(app_mod.McpBridge, "start", nothing)
    monkeypatch.setattr(app_mod.McpBridge, "stop", nothing)
    with TestClient(app_mod.app) as c:
        for path in ("/", "/static/chat.js", "/static/app.css"):
            assert c.get(path).headers["cache-control"] == "no-cache", path
        assert "cache-control" not in c.get("/static/fonts/KaushanScript-Regular.ttf").headers
