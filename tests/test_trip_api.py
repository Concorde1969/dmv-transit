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


class FakeLive:
    """Gives exact times only (like WMATA's feed): 2 minutes after the timetable."""

    def available(self):
        return True

    def lookup(self, feed, stop_id, trip_id, seq, sched, day):
        if sched is None:
            return None, 0
        return {"dep_s": sched + 120, "delay": None, "live": True, "cancelled": False}, 0


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
        return ({"options": [{"id": "x", "legs": []}], "live_checks": 2, "outcome": "ok",
                 "origin": {"label": origin.label}},
                {"origin": {"stop": "Farragut North", "stop_agency": "wmata", "stop_m": 250,
                            "station": "Farragut North", "station_m": 250},
                 "destination": {"stop": "Pentagon", "stop_agency": "wmata", "stop_m": 80}})

    monkeypatch.setattr(trip_api.TripService, "plan", fake_plan)
    monkeypatch.setattr(trip_api, "build_realtime", lambda *a, **k: FakeLive())
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


def test_live_endpoint_works_out_delay_from_exact_times(client):
    client.post("/api/trip/live", json={"rides": []})        # creates the service
    svc = client.app.state.trips
    svc.agencies = {**svc.agencies, "bus": {"name": "Bus", "has_realtime": True}}
    sched = 1_793_037_000
    r = client.post("/api/trip/live", json={"rides": [
        {"agency": "bus", "stop_id": "A", "trip_id": "t1", "scheduled": sched}]})
    (ride,) = r.json()["rides"]
    assert ride["live"] and ride["delay_s"] == 120 and ride["expected"] == sched + 120


def _rows(client, table="trip_searches"):
    return list(client.app.state.chatlog.table_rows(table))


def test_search_log_keeps_landmarks_and_stops_but_never_addresses(client):
    body = {"from": {"lat": 38.902612, "lon": -77.036541, "label": "1001 16th St NW",
                     "source": "suggestion", "kind": "house"},
            "to": {"lat": 38.871048, "lon": -77.056084, "label": "The Pentagon",
                   "source": "suggestion", "kind": "office"}}
    r = client.post("/api/trip", json=body).json()
    (row,) = _rows(client)
    assert r["search_id"] == row["search_id"] and row["outcome"] == "ok"
    assert row["origin_place"] is None and row["dest_place"] == "The Pentagon"
    assert row["origin_station"] == "Farragut North" and row["dest_stop"] == "Pentagon"
    assert row["origin_source"] == "suggestion" and row["distance_km"] > 3
    assert row["weekday"] and row["hour"] is not None
    assert b"1001 16th" not in client.app.state.chatlog.path.read_bytes()


def test_typed_or_linked_names_are_not_kept(client):
    _trip(client, to={"lat": 38.871, "lon": -77.056, "label": "Aunt May's house",
                      "source": "link", "kind": None})
    assert _rows(client)[0]["dest_place"] is None


def test_refused_searches_are_logged_too(client):
    _trip(client, to={"lat": 39.78, "lon": -89.65, "label": "Springfield"})
    assert _rows(client)[0]["outcome"] == "outside_region"


def test_page_events(client):
    ok = client.post("/api/events", json={"event": "option_selected", "search_id": "abc",
                                          "option_index": 2, "detail": {"view": "list"}})
    bad = client.post("/api/events", json={"event": "anything_else"})
    assert ok.json()["ok"] and not bad.json()["ok"]
    (ev,) = _rows(client, "events")
    assert ev["event"] == "option_selected" and ev["option_index"] == 2
    assert ev["detail_json"] == {"view": "list"}


def test_removal_request_clears_searches_and_events(client):
    _trip(client)
    client.post("/api/events", json={"event": "page_view"})
    log = client.app.state.chatlog
    visitor = _rows(client)[0]["visitor_id"]
    log.delete(visitor=visitor)
    assert _rows(client) == [] and _rows(client, "events") == []


def test_trip_export_and_stats(client):
    _trip(client)
    log = client.app.state.chatlog
    csv_text = log.export_table_csv("trip_searches")
    assert "origin_station" in csv_text.splitlines()[0] and "Farragut North" in csv_text
    stats = log.trip_stats(7)
    assert stats["searches"] == 1 and stats["top_start_stations"] == {"Farragut North": 1}
