"""Explore: the whole network for the map (chat/explore_api.py)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from transit_mcp.chat.explore_api import build_network, clip, route_kind, stop_detail
from transit_mcp.db.database import Database, init_db
from transit_mcp.ingest.gtfs.shapes import decode, encode

INSIDE = [(38.89, -77.03), (38.90, -77.00), (38.91, -76.98)]      # downtown DC
FAR = [(40.75, -73.99), (40.50, -74.40)]                           # New York, outside


def _fill(db: Database) -> None:
    c = db.conn
    c.executemany("INSERT INTO gtfs_feeds (feed_id, agency_name, mode, source_url, status, "
                  "fetched_at) VALUES (?, ?, ?, '', 'loaded', '2026-11-15')",
                  [("wmata", "WMATA", "rail"), ("metrobus", "Metrobus", "bus"),
                   ("amtrak", "Amtrak", "intercity_rail")])
    c.executemany("INSERT INTO gtfs_routes (feed_id, route_id, short_name, long_name, "
                  "route_type, color) VALUES (?, ?, ?, ?, ?, ?)",
                  [("wmata", "RED", "R", "Red", 1, "C80F2D"),
                   ("metrobus", "D80", "D80", "Wisconsin Av", 3, None),
                   ("amtrak", "NER", "", "Northeast Regional", 2, None)])
    c.executemany("INSERT INTO gtfs_shapes (feed_id, shape_id, polyline, n_points) "
                  "VALUES (?, ?, ?, ?)",
                  [("wmata", "s1", encode(INSIDE), 3), ("metrobus", "b1", encode(INSIDE), 3),
                   ("amtrak", "a1", encode(INSIDE + FAR), 5)])
    c.executemany("INSERT INTO gtfs_trips (feed_id, trip_id, route_id, service_id, "
                  "direction_id, shape_id) VALUES (?, ?, ?, 'wk', 0, ?)",
                  [("wmata", "t1", "RED", "s1"), ("metrobus", "t2", "D80", "b1"),
                   ("amtrak", "t3", "NER", "a1")])
    c.executemany("INSERT INTO gtfs_stops (feed_id, stop_id, name, lat, lon, location_type, "
                  "parent_station, wheelchair_boarding) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  [("wmata", "STN", "Metro Center", 38.898, -77.028, 1, None, 1),
                   ("wmata", "PF1", "Metro Center platform", 38.898, -77.028, 0, "STN", None),
                   ("metrobus", "1001", "11th St & G St", 38.8985, -77.0275, None, None, None),
                   ("amtrak", "NYP", "New York Penn", 40.75, -73.99, None, None, None)])
    c.executemany("INSERT INTO gtfs_stop_times (feed_id, trip_id, stop_id, stop_sequence, "
                  "arrival_time, departure_time) VALUES (?, ?, ?, 1, '08:00:00', '08:00:00')",
                  [("wmata", "t1", "PF1"), ("metrobus", "t2", "1001")])
    c.execute("INSERT INTO stop_clusters (cluster_id, lat, lon, n_stops, n_feeds, computed_at) "
              "VALUES (1, 38.898, -77.028, 2, 2, '2026-11-15')")
    c.executemany("INSERT INTO cluster_stops (cluster_id, feed_id, stop_id) VALUES (1, ?, ?)",
                  [("wmata", "STN"), ("metrobus", "1001")])
    c.commit()


def test_route_kinds():
    assert [route_kind(t) for t in (0, 1, 2, 3, 4, 700, 109, None)] == \
        ["rail", "rail", "rail", "bus", "ferry", "bus", "rail", "bus"]


def test_lines_are_clipped_to_the_region():
    (part,) = clip(INSIDE + FAR)
    assert part[:3] == INSIDE and part[-1] == FAR[0]       # runs on to the edge
    assert clip(FAR) == []


def test_network_has_routes_stops_and_hubs_for_the_region(tmp_path):
    init_db(tmp_path / "t.db")
    with Database(tmp_path / "t.db") as db:
        _fill(db)
        net = build_network(db, {"wmata": "Metrorail (WMATA)"})
    ids = [a["id"] for a in net["agencies"]]
    routes = {r[1]: r for r in net["routes"]}
    assert routes["RED"][4] == "#c80f2d" and routes["RED"][5] == "rail"
    assert routes["D80"][4] is None and routes["D80"][5] == "bus"   # the agency color is used
    amtrak = decode(routes["NER"][6][0])
    assert amtrak[-1] == FAR[0] and FAR[1] not in amtrak           # stops at the region's edge
    stops = {s[1]: s for s in net["stops"]}
    assert set(stops) == {"STN", "1001"}            # the platform shows as its station
    assert net["agencies"][ids.index("wmata")]["name"] == "Metrorail (WMATA)"
    assert net["agencies"][ids.index("wmata")]["rail"] is True
    assert net["hubs"] == [[38.898, -77.028, 2, 2]]


def test_a_stop_lists_its_routes_and_hub(tmp_path):
    init_db(tmp_path / "t.db")
    with Database(tmp_path / "t.db") as db:
        _fill(db)
        names = {"wmata": "Metrorail (WMATA)", "metrobus": "Metrobus"}
        station = stop_detail(db, "wmata", "STN", names)
        assert [r["id"] for r in station["routes"]] == ["RED"]     # through its platform
        assert station["wheelchair"] is True and station["agency"] == "Metrorail (WMATA)"
        assert station["hub"] == {"stops": 2, "agencies": ["Metrobus"]}
        assert stop_detail(db, "wmata", "NOPE", names) is None


def test_explore_endpoints(isolated_settings, monkeypatch):
    from transit_mcp.chat import app as app_mod

    init_db(isolated_settings.db_path)
    with Database(isolated_settings.db_path) as db:
        _fill(db)
    settings = isolated_settings.model_copy(update={"anthropic_api_key": "k"})
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    async def nothing(self):
        self._tools = []

    monkeypatch.setattr(app_mod.McpBridge, "start", nothing)
    monkeypatch.setattr(app_mod.McpBridge, "stop", nothing)
    with TestClient(app_mod.app) as c:
        r = c.get("/api/explore")
        assert r.status_code == 200 and len(r.json()["routes"]) == 3
        assert c.get("/api/explore", headers={"If-None-Match": r.headers["etag"]}).status_code == 304
        s = c.get("/api/explore/stop", params={"feed": "metrobus", "id": "1001"})
        assert s.status_code == 200 and s.json()["routes"][0]["short"] == "D80"
        assert c.get("/api/explore/stop", params={"feed": "x", "id": "y"}).status_code == 404
        assert 'id="exploreBtn"' in c.get("/").text
