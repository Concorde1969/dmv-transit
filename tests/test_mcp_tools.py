"""Addresses -> geocoding -> trip options through the MCP tools, and the MCP server at a
web address with API keys (server/app.py, server/http.py, mcp_keys.py)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from transit_mcp.chat.guard import RateLimiter
from transit_mcp.geocode import BudgetSpent, GeocodeError
from transit_mcp.mcp_keys import McpKeys
from transit_mcp.planning import live
from transit_mcp.server import app as server
from transit_mcp.server import http as mcp_http

HOME = {"label": "1717 East Capitol St SE, Washington", "kind": "premise",
        "full_label": "1717 East Capitol St SE, Washington, DC 20003, USA",
        "lat": 38.8893363, "lon": -76.9803116}
TZ = ZoneInfo("America/New_York")


class FakeGeocoder:
    available = True
    attribution = "Google Maps"

    def __init__(self, hits=(HOME,), error=None):
        self.hits, self.error, self.asked = list(hits), error, []

    def search(self, q):
        self.asked.append(q)
        if self.error:
            raise self.error
        return self.hits


class FakeDb:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakePlanner:
    made: list = []

    def __init__(self, db, *, cfg, realtime, agencies, tz, now):
        FakePlanner.made.append({"realtime": realtime, "now": now})

    def plan(self, o, d):
        FakePlanner.made[-1].update(origin=o, destination=d)
        return {"options": [], "notice": "none"}


@pytest.fixture
def tools(monkeypatch):
    geo = FakeGeocoder()
    points = {"Union Station": {"lat": 38.8976, "lon": -77.0064, "label": "Union Station station",
                                "kind": "wmata"}}
    FakePlanner.made = []
    monkeypatch.setattr(server, "_geocoder", lambda: geo)
    monkeypatch.setattr(server, "_db", FakeDb)
    monkeypatch.setattr(server.regional, "resolve_point", lambda db, t: points.get(t))
    monkeypatch.setattr(server, "_trip_helpers", lambda: {
        "settings": type("S", (), {"timezone": "America/New_York"})(),
        "agencies": {}, "realtime": "LIVE"})
    monkeypatch.setattr(live, "LivePlanner", FakePlanner)
    monkeypatch.setattr(live.LiveSettings, "from_settings", staticmethod(lambda s: None))
    return geo


def test_geocode_tool_gives_a_point_the_planner_can_use(tools):
    out = server.geocode_address("1717 E Capitol St SE")
    (hit,) = out["results"]
    assert hit["point"] == "38.889336,-76.980312" and "20003" in hit["address"]
    assert out["source"] == "Google Maps"       # Google's attribution travels with results


def test_geocode_tool_explains_when_it_finds_nothing_or_fails(tools):
    tools.hits = []
    assert "fuller address" in server.geocode_address("nowhere")["message"]
    tools.error = BudgetSpent("spent")
    assert server.geocode_address("x")["error"] == "address_search_paused"
    tools.error = GeocodeError("down")
    assert server.geocode_address("x")["error"] == "address_search_failed"
    tools.available = False
    assert server.geocode_address("x")["error"] == "address_search_not_set_up"


def test_plan_trip_geocodes_addresses_but_not_stations(tools):
    out = server.plan_trip("1717 E Capitol St SE", "Union Station")
    assert out["from"] == HOME["label"] and out["to"] == "Union Station station"
    assert tools.asked == ["1717 E Capitol St SE"]
    assert "unofficial" in out["note"]


def test_plan_trip_takes_a_point_without_geocoding(tools):
    server.plan_trip("38.889336,-76.980312", "Union Station")
    assert tools.asked == []
    assert FakePlanner.made[-1]["origin"].lat == pytest.approx(38.889336)


def test_street_address_outranks_a_similar_stop_name(tools, monkeypatch):
    stop = {"lat": 39.0, "lon": -77.1, "label": "Stop", "kind": "gtfs"}
    monkeypatch.setattr(server.regional, "resolve_point", lambda db, t: stop)
    assert server.plan_trip("100 Main St", "200 Main St")["from"] == HOME["label"]
    tools.hits = []                             # the stop is still better than nothing
    assert server.plan_trip("7 Corners", "200 Main St")["from"] == "Stop"


def test_plan_trip_says_when_a_place_cant_be_found(tools):
    tools.hits = []
    out = server.plan_trip("nowhere at all", "Union Station")
    assert out["error"] == "could_not_find_place" and out["origin_found"] is False
    assert out["destination_found"] is True


def test_later_trips_use_the_timetable_only(tools):
    server.plan_trip("Union Station", "Union Station")
    assert FakePlanner.made[-1]["realtime"] == "LIVE"
    later = (datetime.now(TZ) + timedelta(days=2)).replace(hour=9, minute=30, second=0,
                                                           microsecond=0)
    out = server.plan_trip("Union Station", "Union Station", later.strftime("%Y-%m-%dT%H:%M"))
    made = FakePlanner.made[-1]
    assert made["realtime"] is None and made["now"] == later
    assert "9:30 AM" in out["leaving_after"]


@pytest.mark.parametrize("bad", ["tomorrow", "2020-01-01T09:00"])
def test_past_or_unreadable_times_are_refused(tools, bad):
    assert server.plan_trip("Union Station", "Union Station", bad)["error"] == "bad_depart_at"


def test_engine_gets_addresses_as_points(tools, monkeypatch):
    seen = {}

    def fake_plan(db, o, d, when):
        seen.update(o=o, d=d, when=when)
        return {"options": []}

    monkeypatch.setattr(server.connect, "plan_connection", fake_plan)
    out = server.plan_connection("1717 E Capitol St SE", "Union Station", "sunday")
    assert seen == {"o": "38.889336,-76.980312", "d": "Union Station", "when": "sunday"}
    assert out["origin_found_as"] == HOME["label"] and "destination_found_as" not in out


def test_urls_with_keys_stay_out_of_the_logs():
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING


# ----------------------------------------------------------------------------- keys
@pytest.fixture
def keys(tmp_path):
    k = McpKeys(tmp_path / "keys.db")
    yield k
    k.close()


def test_keys_work_until_revoked_and_only_hashes_are_stored(keys):
    key_id, key = keys.add("  Ana's   assistant ")
    assert key.startswith("dmvt_") and len(key) > 40
    assert keys.check(key)["name"] == "Ana's assistant"
    assert key.encode() not in keys.path.read_bytes()
    assert keys.check("dmvt_wrong") is None and keys.check(None) is None
    (row,) = keys.list()
    assert row["calls"] == 1 and row["last_used"] and "key_hash" not in row
    assert keys.revoke(key_id) and not keys.revoke(key_id)
    assert keys.check(key) is None


def test_a_key_needs_a_name(keys):
    with pytest.raises(ValueError):
        keys.add("   ")


# ----------------------------------------------------------------------------- web
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "test", "version": "1"}}}
LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
ACCEPT = {"Accept": "application/json, text/event-stream"}


@pytest.fixture(scope="module")
def web(tmp_path_factory):
    # The SDK's HTTP session manager starts once per process, so one app for these tests.
    keys = McpKeys(tmp_path_factory.mktemp("mcp") / "keys.db")
    settings = type("S", (), {"mcp_rate_per_minute": 1000, "mcp_rate_per_day": 0})()
    app = mcp_http.create_app(settings, keys)
    with TestClient(app, base_url="https://dmvtransit.com") as client:
        client.keys, client.guard = keys, app
        yield client
    keys.close()


def _auth(key):
    return {**ACCEPT, "Authorization": f"Bearer {key}"}


def test_no_key_no_entry(web):
    r = web.post("/mcp", json=INIT, headers=ACCEPT)
    assert r.status_code == 401 and r.headers["www-authenticate"] == "Bearer"
    assert web.post("/mcp", json=INIT, headers=_auth("dmvt_nope")).status_code == 401
    assert web.get("/mcp", headers=ACCEPT).status_code == 401


def test_a_key_opens_the_tools_on_the_public_host_name(web):
    _, key = web.keys.add("tools")
    r = web.post("/mcp", json=INIT, headers=_auth(key))
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "dmv-transit"
    assert "geocode_address" in r.json()["result"]["instructions"]
    names = {t["name"] for t in web.post("/mcp", json=LIST, headers=_auth(key)).json()
             ["result"]["tools"]}
    assert {"geocode_address", "plan_trip", "plan_connection", "get_metro_route"} <= names
    assert "plan_trip_now" not in names


def test_a_revoked_key_stops_at_once(web):
    key_id, key = web.keys.add("revoked")
    assert web.post("/mcp", json=INIT, headers=_auth(key)).status_code == 200
    web.keys.revoke(key_id)
    assert web.post("/mcp", json=INIT, headers=_auth(key)).status_code == 401


def test_each_key_has_its_own_limit(web):
    _, busy = web.keys.add("busy")
    _, quiet = web.keys.add("quiet")
    saved, web.guard.limiter = web.guard.limiter, RateLimiter(1, 0)
    try:
        assert web.post("/mcp", json=INIT, headers=_auth(busy)).status_code == 200
        r = web.post("/mcp", json=INIT, headers=_auth(busy))
        assert r.status_code == 429 and r.headers["retry-after"] == "60"
        assert web.post("/mcp", json=INIT, headers=_auth(quiet)).status_code == 200
    finally:
        web.guard.limiter = saved
