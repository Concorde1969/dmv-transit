"""Walking directions (walking.py): openrouteservice answers, the cap and cache, station
entrances, and how a routed walk moves an option's times."""

from __future__ import annotations

import httpx

from transit_mcp import walking
from transit_mcp.db.database import Database, init_db
from transit_mcp.planning.describe import brief_option

ORS = {"type": "FeatureCollection", "features": [{
    "type": "Feature",
    "geometry": {"type": "LineString", "coordinates": [
        [-77.0300, 38.8980], [-77.0300, 38.8990], [-77.0285, 38.8990]]},
    "properties": {
        "summary": {"distance": 245.0, "duration": 176.0},
        "segments": [{"distance": 245.0, "duration": 176.0, "steps": [
            {"distance": 110.0, "duration": 79.0, "type": 11,
             "instruction": "Head north on 13th Street Northwest", "name": "13th Street Northwest",
             "way_points": [0, 1]},
            {"distance": 135.0, "duration": 97.0, "type": 1,
             "instruction": "Turn right onto G Street Northwest", "name": "G Street Northwest",
             "way_points": [1, 2]},
            {"distance": 0.0, "duration": 0.0, "type": 10, "instruction": "Arrive at G Street",
             "name": "-", "way_points": [2, 2]}]}]}}]}

A, B = (38.8980, -77.0300), (38.8990, -77.0285)


def _router(tmp_path, handler, **kw):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return walking.WalkRouter("test-key", cache_path=tmp_path / "walks.db", http=http,
                              pace_m_per_min=70, **kw)


def test_parse_keeps_turns_and_drops_arrival():
    body = walking.parse(ORS)
    assert body["distance_m"] == 245.0 and len(body["path"]) == 3
    assert [s["text"] for s in body["steps"]] == ["Head north on 13th St NW",
                                                  "Turn right onto G St NW"]
    assert body["steps"][1]["turn"] == "right" and body["steps"][0]["distance_m"] == 110


def test_route_asks_once_then_uses_the_cache(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=ORS)

    router = _router(tmp_path, handler)
    first = router.route(A, B)
    assert first["routed"] and first["distance_m"] == 245
    assert first["duration_s"] == 210                  # 245 m at 70 m a minute, rounded up
    assert calls[0].headers["Authorization"] == "test-key"
    sent = calls[0].read().decode()
    assert '[[-77.03,38.898],' in sent and '"instructions":true' in sent
    again = router.route(A, B)
    assert again == first and len(calls) == 1           # from the cache


def test_route_gives_none_when_it_cannot(tmp_path):
    router = _router(tmp_path, lambda r: httpx.Response(429, json={}))
    assert router.route(A, B) is None and router.paused_until > 0
    assert router.route((38.9, -77.03), (38.901, -77.03)) is None   # still backing off
    keyless = walking.WalkRouter(None, cache_path=tmp_path / "w2.db")
    assert not keyless.available and keyless.route(A, B) is None
    capped = _router(tmp_path, lambda r: httpx.Response(200, json=ORS), per_day=1)
    assert capped.route(A, B) is not None
    assert capped.route((38.91, -77.03), (38.912, -77.03)) is None  # over the cap
    assert capped.route(A, A) is None                               # nothing to route


def _db(tmp_path):
    init_db(tmp_path / "t.db")
    db = Database(tmp_path / "t.db")
    db.conn.execute("INSERT OR IGNORE INTO gtfs_feeds (feed_id, agency_name, mode, source_url, "
                    "status) VALUES ('wmata', 'Metrorail', 'subway', '', 'loaded')")
    db.conn.executemany(
        "INSERT INTO gtfs_stops (feed_id, stop_id, name, lat, lon, location_type, "
        "parent_station, wheelchair_boarding) VALUES ('wmata', ?, ?, ?, ?, ?, ?, ?)",
        [("STN_A01", "Metro Center", 38.8983, -77.0281, 1, None, None),
         ("PF_A01_1", "Metro Center, Red Line Track 1 Platform", 38.8983, -77.0281, 0,
          "STN_A01", None),
         ("PF_A01_C", "Metro Center, Blue Line Platform", 38.8984, -77.0280, 0, "STN_A01", None),
         ("ENT_A01_N", "Entrance A - 12Th and G Sts.", 38.89848, -77.02789, 2, "STN_A01", 2),
         ("ENT_A01_S", "Entrance C - 12Th and F Sts. NW", 38.89715, -77.02859, 2, "STN_A01", 2),
         ("ENT_A01_EL", "Entrance A - 12Th and G Sts. NW (Elevator)", 38.89892, -77.02792, 2,
          "STN_A01", 1)])
    db.commit()
    return db


def test_walks_use_the_nearest_entrance_and_name_the_elevator(tmp_path):
    db = _db(tmp_path)
    try:
        doors = walking.entrances(db, "wmata", "PF_A01_1")
        assert len(doors) == 3 and doors[0]["name"].startswith("Entrance A - 12th and G")
        a, b, extra = walking.walk_ends(db, (38.8960, -77.0290), (38.8983, -77.0281),
                                        to_stop=("wmata", "PF_A01_1"))
        assert extra["entrance"] == "Entrance C - 12th and F Sts. NW"   # from the south
        assert extra["elevator"] == "Entrance A - 12th and G Sts. NW (Elevator)"
        assert b == (38.89715, -77.02859)
        _, _, inside = walking.walk_ends(db, b, b, ("wmata", "PF_A01_1"), ("wmata", "PF_A01_C"))
        assert inside == {"same_station": True}
        _, _, bus = walking.walk_ends(db, a, b, to_stop=("metrobus", "1001"))
        assert bus == {}
    finally:
        db.close()


def _option():
    ride = {"mode": "ride", "agency": "wmata", "agency_name": "Metrorail", "route_short_name": "RD", "route_long_name": "Red",
            "route_id": "RED", "headsign": "Glenmont", "live": False, "stops": 3,
            "board": {"name": "Metro Center", "stop_id": "PF_A01_1", "lat": 38.8983,
                      "lon": -77.0281, "expected": 1_000_600},
            "alight": {"name": "Union Station", "stop_id": "PF_B03_1", "lat": 38.8977,
                       "lon": -77.0074, "expected": 1_001_200}}
    return {"leave_at": 1_000_300, "arrive_at": 1_001_500, "duration_s": 1200, "wait_s": 0,
            "walk_s": 600, "transfers": 0, "live": False, "fare_estimate": 2.25, "legs": [
                {"mode": "walk", "duration_s": 300, "to": ride["board"]},
                ride,
                {"mode": "walk", "duration_s": 300, "from": ride["alight"]}]}


def test_a_routed_walk_moves_the_times():
    opt = _option()
    walking.apply_walk(opt, 0, {"routed": True, "duration_s": 420, "distance_m": 490,
                                "steps": [{"text": "Head east on F Street"}],
                                "entrance": "Entrance C"}, now=1_000_000)
    assert opt["leave_at"] == 1_000_180 and opt["walk_s"] == 720
    walking.apply_walk(opt, 2, {"routed": True, "duration_s": 240, "distance_m": 280,
                                "steps": []}, now=1_000_000)
    assert opt["arrive_at"] == 1_001_440 and opt["duration_s"] == 1_001_440 - 1_000_180
    walking.apply_walk(opt, 2, {"exit": "Entrance B"}, now=1_000_000)   # names only
    assert opt["arrive_at"] == 1_001_440


def test_route_option_and_words(tmp_path):
    from zoneinfo import ZoneInfo

    db = _db(tmp_path)
    router = _router(tmp_path, lambda r: httpx.Response(200, json=ORS))
    try:
        opt = _option()
        walking.route_option(db, router, opt, {"lat": 38.8960, "lon": -77.0290},
                             {"lat": 38.8990, "lon": -77.0060}, now=1_000_000, only_ends=True)
    finally:
        db.close()
    first = opt["legs"][0]["walk"]
    assert first["routed"] and first["entrance"].startswith("Entrance C")
    text = brief_option(opt, ZoneInfo("America/New_York"))["steps"][0]
    assert "go in at Entrance C" in text and "Turn right onto G St NW" in text
    assert "0.2 miles" in text and "OpenStreetMap" in text


def test_unnamed_paths_say_which_way_and_sidewalks_join_their_street():
    def step(kind, meters, name, bearing, text="Turn left"):
        return {"type": kind, "distance": meters, "name": name, "instruction": text,
                "maneuver": {"bearing_after": bearing}}

    answer = {"features": [{"geometry": {"coordinates": [[0, 0], [1, 1]]}, "properties": {
        "summary": {"distance": 600},
        "segments": [{"steps": [
            step(11, 30, "F Street Northwest", 270, "Head west on F Street Northwest"),
            step(0, 200, "-", 268),              # F Street's sidewalk, mapped with no name
            step(1, 12, "-", 0),                 # a crosswalk
            step(1, 150, "-", 2),
            step(4, 60, "-", 330),               # bends a little
            step(0, 140, "-", 270),
            step(10, 0, "-", 0, "Arrive")]}]}}]}
    steps = walking.parse(answer)["steps"]
    assert [(s["text"], s["distance_m"]) for s in steps] == [
        ("Head west on F St NW", 242), ("Walk north", 210), ("Walk west", 140)]
