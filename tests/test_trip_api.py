"""Address search proxy and the trip endpoints behind the map."""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from transit_mcp.geocode import Geocoder

CAPITAL_HILTON = {"lat": "38.9026", "lon": "-77.0365",
                  "display_name": "Capital Hilton, 1001, 16th Street Northwest, Washington, "
                                  "District of Columbia, 20036, United States",
                  "type": "hotel"}
SPRINGFIELD_IL = {"lat": "39.78", "lon": "-89.65", "display_name": "Springfield, Illinois"}


def _geocoder(seen):
    def handler(request: httpx.Request):
        seen.append(request)
        if request.url.path == "/search":
            return httpx.Response(200, json=[CAPITAL_HILTON, SPRINGFIELD_IL])
        return httpx.Response(200, json={"display_name": "1600, Pennsylvania Avenue "
                                         "Northwest, Washington, 20500, United States"})
    return Geocoder("GEO-KEY", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_geocoder_filters_region_hides_key_and_caches():
    seen = []
    g = _geocoder(seen)
    (hit,) = g.search("Capitol Hilton DC")
    assert hit["label"] == "Capital Hilton, 16th Street Northwest, Washington"
    assert (hit["lat"], hit["lon"]) == (38.9026, -77.0365)
    req = seen[0]
    assert req.headers["Authorization"] == "Bearer GEO-KEY"
    assert "GEO-KEY" not in str(req.url)
    g.search("Capitol Hilton DC")
    assert len(seen) == 1                     # cached
    assert g.search("ab") == []               # too short: no request
    assert g.reverse(38.8977, -77.0365) == "Pennsylvania Avenue Northwest, Washington"


def test_geocoder_without_key_does_nothing():
    assert Geocoder(None).search("Union Station") == []


@pytest.fixture
def client(isolated_settings, monkeypatch):
    from transit_mcp.chat import app as app_mod
    from transit_mcp.chat import trip_api

    settings = isolated_settings.model_copy(update={
        "anthropic_api_key": "test-key", "app_env": "production", "geocode_api_key": "GEO",
        "trip_rate_per_minute": 3})
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    async def no_start(self):
        self._tools = []

    async def no_stop(self):
        pass

    monkeypatch.setattr(app_mod.McpBridge, "start", no_start)
    monkeypatch.setattr(app_mod.McpBridge, "stop", no_stop)
    from transit_mcp.db.database import init_db
    init_db(settings.db_path)

    def fake_plan(self, origin, destination):
        return {"options": [{"id": "x", "legs": []}], "later_options": [],
                "live_checks": 2, "origin": {"label": origin.label}}

    monkeypatch.setattr(trip_api.TripService, "plan", fake_plan)
    with TestClient(app_mod.app) as c:
        yield c


def _trip(client, **over):
    body = {"from": {"lat": 38.902612, "lon": -77.036541, "label": "1001 16th St NW"},
            "to": {"lat": 38.871048, "lon": -77.056084, "label": "Pentagon"}}
    body.update(over)
    return client.post("/api/trip", json=body)


def test_trip_endpoint_and_privacy_preserving_log(client):
    r = _trip(client)
    assert r.status_code == 200 and r.json()["options"][0]["id"] == "x"
    log = client.app.state.chatlog
    row = dict(log.conn.execute("SELECT * FROM trip_searches").fetchone())
    assert (row["origin_lat"], row["origin_lon"]) == (38.903, -77.037)
    assert row["n_options"] == 1 and row["live_checks"] == 2
    assert b"16th St" not in log.path.read_bytes()   # typed addresses aren't kept


def test_trip_outside_region_and_rate_limit(client):
    r = _trip(client, to={"lat": 39.78, "lon": -89.65, "label": "Springfield"})
    assert r.status_code == 422
    for _ in range(3):
        _trip(client)
    assert _trip(client).status_code == 429


def test_trip_rejects_bad_input(client):
    assert client.post("/api/trip", json={"from": {"lat": "x"}}).status_code == 422


def test_reverse_outside_region_needs_no_lookup(client):
    r = client.get("/api/reverse", params={"lat": 51.5, "lon": -0.12})
    assert r.json() == {"label": None, "outside_region": True}


def test_live_endpoint_skips_feeds_without_realtime(client):
    r = client.post("/api/trip/live", json={"rides": [
        {"agency": "loudoun", "stop_id": "s1", "trip_id": "t1"}]})
    assert r.status_code == 200 and json.loads(r.text)["rides"] == []
