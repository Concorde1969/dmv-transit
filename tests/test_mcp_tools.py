"""Addresses -> geocoding -> trip options through the MCP tools, and the MCP server at a
web address with API keys (server/app.py, server/http.py, mcp_keys.py)."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from transit_mcp.chat.guard import DailyCap, RateLimiter
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
    settings = SimpleNamespace(
        mcp_rate_per_minute=1000, mcp_rate_per_day=0, mcp_open=True,
        mcp_open_rate_per_minute=1000, mcp_open_rate_per_day=0, mcp_open_daily_cap=0,
        mcp_google_geocoding_monthly_budget=5, timezone="America/New_York")
    app = mcp_http.create_app(settings, keys)
    try:
        with TestClient(app, base_url="https://dmvtransit.com") as client:
            client.keys, client.guard = keys, app
            yield client
    finally:
        keys.close()
        server.GEOCODE_BUDGET.clear()
        server._geocoder.cache_clear()


def _auth(key):
    return {**ACCEPT, "Authorization": f"Bearer {key}"}


def test_anyone_can_connect_without_a_key(web):
    r = web.post("/mcp", json=INIT, headers=ACCEPT)
    assert r.status_code == 200, r.text
    assert r.json()["result"]["serverInfo"]["name"] == "dmv-transit"
    assert web.post("/mcp", json=LIST, headers=ACCEPT).json()["result"]["tools"]


def test_a_wrong_key_is_still_refused(web):
    r = web.post("/mcp", json=INIT, headers=_auth("dmvt_nope"))
    assert r.status_code == 401 and "isn't valid" in r.json()["error"]


def test_mcp_callers_get_their_own_google_allowance(web):
    assert server.GEOCODE_BUDGET == {"service": "google_geocoding_mcp", "monthly_limit": 5}


def test_keyless_callers_are_limited_per_network(web):
    saved = web.guard.open_limiter
    web.guard.open_limiter = RateLimiter(1, 0)
    try:
        assert web.post("/mcp", json=INIT, headers=ACCEPT).status_code == 200
        r = web.post("/mcp", json=INIT, headers=ACCEPT)
        assert r.status_code == 429 and r.headers["retry-after"] == "60"
        assert "mcp-server" in r.json()["error"]
        _, key = web.keys.add("past the open limit")
        assert web.post("/mcp", json=INIT, headers=_auth(key)).status_code == 200
    finally:
        web.guard.open_limiter = saved


def _gate_call(gate, headers=(), client=("203.0.113.5", 1234)):
    """One request straight through a Gate, with a stand-in for the MCP app."""
    sent = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(msg):
        sent.append(msg)

    gate.app = app
    asyncio.run(gate({"type": "http", "headers": list(headers), "client": client},
                     receive, send))
    body = sent[-1]["body"]
    return sent[0]["status"], (json.loads(body) if body != b"ok" else None)


def test_a_closed_server_still_needs_a_key(keys):
    gate = mcp_http.Gate(None, keys, RateLimiter(10, 0))
    status, body = _gate_call(gate)
    assert status == 401 and "API key is required" in body["error"]


def test_all_keyless_callers_share_a_daily_cap(keys):
    gate = mcp_http.Gate(None, keys, RateLimiter(10, 0), open_limiter=RateLimiter(10, 0),
                         open_cap=DailyCap(2))
    assert _gate_call(gate, client=("198.51.100.1", 1))[0] == 200
    assert _gate_call(gate, client=("198.51.100.2", 1))[0] == 200
    status, body = _gate_call(gate, client=("198.51.100.3", 1))
    assert status == 429 and "busy today" in body["error"]


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


def test_every_tool_carries_the_grounding_rule():
    tools = asyncio.run(server.mcp.list_tools())
    assert len(tools) == 18
    for t in tools:
        assert t.description.endswith(server.GROUNDING), t.name
        assert len(t.description) > len(server.GROUNDING) + 40, t.name   # docstring kept


def test_every_tool_has_a_title_and_is_marked_read_only():
    # Claude's and ChatGPT's directories check these on every tool.
    for t in asyncio.run(server.mcp.list_tools()):
        assert t.title and t.annotations.title == t.title, t.name
        assert t.annotations.readOnlyHint is True and t.annotations.destructiveHint is False


def test_registry_entry_points_at_the_public_server():
    entry = json.loads((Path(__file__).parents[1] / "server.json").read_text("utf-8"))
    assert entry["name"] == "com.dmvtransit/transit" and len(entry["description"]) <= 100
    assert entry["remotes"] == [{"type": "streamable-http", "url": "https://dmvtransit.com/mcp"}]


def test_privacy_page_is_served(monkeypatch, isolated_settings):
    from transit_mcp.chat import app as app_mod

    settings = isolated_settings.model_copy(update={"anthropic_api_key": "k", "app_env": "production"})
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    async def nothing(self):
        self._tools = []

    monkeypatch.setattr(app_mod.McpBridge, "start", nothing)
    monkeypatch.setattr(app_mod.McpBridge, "stop", nothing)
    with TestClient(app_mod.app) as c:
        r = c.get("/privacy")
        assert r.status_code == 200 and "What we keep" in r.text
        assert "The MCP server for AI assistants" in r.text
        assert 'href="/privacy"' in c.get("/").text
        docs = c.get("/mcp-server")
        assert docs.status_code == 200 and "https://dmvtransit.com/mcp" in docs.text
        assert 'href="/terms"' in docs.text and 'href="/privacy"' in docs.text
        assert "as is" in c.get("/terms").text
        proof = c.get("/.well-known/mcp-registry-auth")
        assert proof.status_code == 200 and proof.text.startswith("v=MCPv1; k=ed25519; p=")
