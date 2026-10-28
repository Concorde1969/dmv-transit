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


UNION_STATION_G = {
    "formatted_address": "50 Massachusetts Ave NE, Washington, DC 20002, USA",
    "geometry": {"location": {"lat": 38.8973, "lng": -77.0063}},
    "types": ["establishment", "point_of_interest", "transit_station"],
    "address_components": [
        {"long_name": "Union Station", "types": ["establishment", "point_of_interest",
                                                 "transit_station"]},
        {"long_name": "50", "types": ["street_number"]},
        {"long_name": "Washington", "types": ["locality", "political"]}]}
ADDRESS_G = {
    "formatted_address": "1000 N Glebe Rd, Arlington, VA 22201, USA",
    "geometry": {"location": {"lat": 38.8836, "lng": -77.1166}},
    "types": ["street_address"],
    "address_components": [{"long_name": "Arlington", "types": ["locality", "political"]}]}
SPRINGFIELD_G = {"formatted_address": "Springfield, IL, USA",
                 "geometry": {"location": {"lat": 39.78, "lng": -89.65}},
                 "types": ["locality", "political"], "address_components": []}


class FakeBudget:
    def __init__(self, left):
        self.left, self.limit = left, left

    def try_spend(self, n=1):
        if self.left < n:
            return False
        self.left -= n
        return True


def _google(seen, reply=None, budget=None):
    def handler(request: httpx.Request):
        seen.append(request)
        if reply is not None:
            return reply
        if "latlng" in request.url.params:
            return httpx.Response(200, json={"status": "OK", "results": [ADDRESS_G]})
        return httpx.Response(200, json={"status": "OK",
                                         "results": [UNION_STATION_G, ADDRESS_G, SPRINGFIELD_G]})
    return Geocoder("G-KEY", provider="google", budget=budget,
                    http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_google_geocoder_names_places_filters_region_and_caches():
    seen = []
    g = _google(seen, budget=FakeBudget(10))
    station, address = g.search("union station")
    assert station == {"label": "Union Station, Washington",
                       "full_label": UNION_STATION_G["formatted_address"],
                       "sub": "50 Massachusetts Ave NE, Washington",
                       "lat": 38.8973, "lon": -77.0063, "kind": "transit_station"}
    assert (address["label"], address["sub"], address["kind"]) == (
        "1000 N Glebe Rd, Arlington", "", "street_address")
    params = seen[0].url.params
    assert seen[0].url.host == "maps.googleapis.com" and seen[0].url.path.endswith("/json")
    assert params["address"] == "union station" and params["key"] == "G-KEY"
    assert params["components"] == "country:US" and "|" in params["bounds"]
    g.search("union station")
    assert len(seen) == 1 and g.budget.left == 9           # cached: not billed twice
    assert g.reverse(38.8836, -77.1166) == "1000 N Glebe Rd, Arlington"
    assert g.attribution == "Google Maps"


def test_google_geocoder_errors_never_show_the_key():
    from transit_mcp.geocode import BudgetSpent, GeocodeError

    for reply in (httpx.Response(403, text="forbidden"),
                  httpx.Response(200, json={"status": "REQUEST_DENIED",
                                            "error_message": "The provided API key is invalid."})):
        with pytest.raises(GeocodeError) as err:
            _google([], reply=reply).search("union station")
        assert "G-KEY" not in str(err.value) and err.value.__cause__ is None
    zero = httpx.Response(200, json={"status": "ZERO_RESULTS", "results": []})
    assert _google([], reply=zero).search("nowhere at all") == []
    seen = []
    with pytest.raises(BudgetSpent):
        _google(seen, budget=FakeBudget(0)).search("union station")
    assert seen == []                                      # stopped before calling Google


def test_google_labels_name_once_and_drop_suites():
    from transit_mcp.geocode import _google_item

    airport = _google_item({
        "formatted_address": "Ronald Reagan Washington National Airport (DCA), "
                             "2401 Smith Blvd, Arlington, VA 22202, USA",
        "geometry": {"location": {"lat": 38.85, "lng": -77.04}}, "types": ["airport"],
        "address_components": [
            {"long_name": "Ronald Reagan Washington National Airport", "types": ["airport"]},
            {"long_name": "Arlington", "types": ["locality"]}]})
    assert airport["label"] == "Ronald Reagan Washington National Airport, Arlington"
    assert airport["sub"] == "2401 Smith Blvd, Arlington"
    office = _google_item({"formatted_address": "1601 K St NW # 1, Washington, DC 20006, USA",
                           "geometry": {"location": {"lat": 38.9, "lng": -77.03}},
                           "types": ["subpremise"], "address_components": []})
    assert office["label"] == "1601 K St NW, Washington"


def test_geocoder_from_settings_prefers_google(isolated_settings):
    s = isolated_settings.model_copy(update={"google_geocoding_api_key": "G",
                                             "geocode_api_key": "M"})
    g = Geocoder.from_settings(s)
    assert (g.provider, g.api_key, g.budget.limit) == ("google", "G", 9000)
    m = Geocoder.from_settings(s.model_copy(update={"google_geocoding_api_key": None}))
    assert (m.provider, m.api_key, m.budget, m.attribution) == ("mapsco", "M", None, None)


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


def test_google_address_names_are_never_kept_in_the_search_log():
    from transit_mcp.chatlog import place_name

    assert place_name("1000 N Glebe Rd, Arlington", "street_address", "suggestion") is None
    assert place_name("Watergate", "premise", "suggestion") is None
    assert place_name("Union Station, Washington", "transit_station",
                      "suggestion") == "Union Station, Washington"


def _places(seen, reply=None, budget=None):
    from transit_mcp.geocode import PlaceSearch

    def handler(request: httpx.Request):
        seen.append(request)
        if reply is not None:
            return reply
        if request.url.path.endswith(":autocomplete"):
            return httpx.Response(200, json={"suggestions": [
                {"placePrediction": {
                    "placeId": "ChIJ_capital_hilton", "types": ["lodging", "establishment"],
                    "text": {"text": "Capital Hilton, 16th Street Northwest, Washington, DC, USA"},
                    "structuredFormat": {
                        "mainText": {"text": "Capital Hilton"},
                        "secondaryText": {"text": "16th Street Northwest, Washington, DC, USA"}}}},
                {"queryPrediction": {"text": {"text": "capital hilton restaurants"}}}]})
        return httpx.Response(200, json={
            "id": "ChIJ_capital_hilton", "types": ["lodging", "establishment"],
            "displayName": {"text": "Capital Hilton"},
            "formattedAddress": "1001 16th St NW, Washington, DC 20036, USA",
            "location": {"latitude": 38.9026, "longitude": -77.0365}})
    return PlaceSearch("P-KEY", budget=budget,
                       http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_places_suggestions_then_the_picked_place():
    seen = []
    p = _places(seen, budget=FakeBudget(10))
    (hit,) = p.autocomplete("capital hil", "session-1234")
    assert hit == {"label": "Capital Hilton", "sub": "16th Street Northwest, Washington, DC",
                   "place_id": "ChIJ_capital_hilton", "kind": "lodging",
                   "full_label": "Capital Hilton, 16th Street Northwest, Washington, DC, USA"}
    req = seen[0]
    body = json.loads(req.content)
    assert req.headers["X-Goog-Api-Key"] == "P-KEY" and "P-KEY" not in str(req.url)
    assert body["sessionToken"] == "session-1234" and body["includedRegionCodes"] == ["us"]
    assert body["locationRestriction"]["rectangle"]["low"]["latitude"] == 37.4
    place = p.details("ChIJ_capital_hilton", "session-1234")
    assert place["label"] == "Capital Hilton" and place["kind"] == "lodging"
    assert (place["lat"], place["lon"]) == (38.9026, -77.0365)
    assert seen[1].headers["X-Goog-FieldMask"] == "id,displayName,formattedAddress,location,types"
    assert seen[1].url.params["sessionToken"] == "session-1234"
    assert p.details("../../etc", None) is None and len(seen) == 2   # bad IDs never sent
    assert p.budget.left == 8


def test_places_errors_never_show_the_key():
    from transit_mcp.geocode import BudgetSpent, GeocodeError

    denied = httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED",
                                                 "message": "Places API (New) is not enabled"}})
    with pytest.raises(GeocodeError) as err:
        _places([], reply=denied).autocomplete("union station")
    assert str(err.value) == "places: HTTP 403 PERMISSION_DENIED"
    seen = []
    with pytest.raises(BudgetSpent):
        _places(seen, budget=FakeBudget(0)).autocomplete("union station")
    assert seen == []


def test_suggestions_fall_back_to_geocoding_when_places_fails():
    from transit_mcp.chat.trip_api import _suggest

    denied = httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})

    class Svc:
        places = _places([], reply=denied)
        geocoder = _geocoder([])
        places_paused_until = 0.0

    svc = Svc()
    found, via = _suggest(svc, "Capitol Hilton DC", None)
    assert via == "geocode" and found[0]["label"].startswith("Capital Hilton")
    assert svc.places_paused_until > 0                    # not retried for a while
    svc.places = _places([])
    assert _suggest(svc, "capital hil", None)[1] == "geocode"
    svc.places_paused_until = 0.0
    assert _suggest(svc, "capital hil", None)[1] == "places"
