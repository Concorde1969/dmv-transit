"""Airport links, two-transfer trips and headway feeds, on the made-up network of
test_live_planner.py (Monday 2026-10-26, 08:00 Eastern).

    bus:  A ──R1── B          rail: X ──R2── Y          light rail: P ──L3── Q
          (B and X ~60 m apart; Y and P ~60 m apart; Q near FAR_END)
"""

from __future__ import annotations

import pytest

from tests.test_live_planner import NOW, START, _trip
from tests.test_live_planner import net as net  # noqa: F401 - fixture
from transit_mcp.ingest.gtfs.loader import expand_frequencies
from transit_mcp.planning.airports import AirportLink, load_links
from transit_mcp.planning.live import LivePlanner, LiveSettings, Point

FAR_END = Point(38.9905, -76.9800, "Far end")
AIRPORT = Point(38.9900, -76.9800, "Airport terminal")


@pytest.fixture
def three_lines(net):  # noqa: F811
    net.conn.executemany("INSERT INTO gtfs_stops (feed_id, stop_id, name, lat, lon) "
                         "VALUES (?, ?, ?, ?, ?)",
                         [("lr", "P", "Stop P", 38.9609, -76.9995),
                          ("lr", "Q", "Stop Q", 38.9900, -76.9800)])
    net.conn.execute("INSERT INTO gtfs_calendar (feed_id, service_id, monday, tuesday, "
                     "wednesday, thursday, friday, start_date, end_date) VALUES "
                     "('lr', 'WK', 1, 1, 1, 1, 1, '20260101', '20271231')")
    net.conn.execute("INSERT INTO gtfs_routes (feed_id, route_id, short_name, route_type) "
                     "VALUES ('lr', 'L3', 'Light', 0)")
    _trip(net, "bus", "t1", "R1", "WK", [("A", "08:05:00"), ("B", "08:15:00")])
    _trip(net, "rail", "t2", "R2", "WK", [("X", "08:22:00"), ("Y", "08:35:00")])
    _trip(net, "lr", "t3", "L3", "WK", [("P", "08:42:00"), ("Q", "08:55:00")])
    net.commit()
    return net


def test_two_transfers_when_nothing_simpler_works(three_lines):
    p = LivePlanner(three_lines, cfg=LiveSettings(), now=NOW, airports=())
    (opt,) = p.plan(START, FAR_END)["options"]
    rides = [leg for leg in opt["legs"] if leg["mode"] == "ride"]
    assert [r["route_id"] for r in rides] == ["R1", "R2", "L3"] and opt["transfers"] == 2
    waits = [leg["wait_s"] for leg in opt["legs"] if leg.get("transfer")]
    assert all(0 < w <= 10 * 60 for w in waits)
    one = LivePlanner(three_lines, cfg=LiveSettings(max_transfers=1), now=NOW, airports=())
    assert one.plan(START, FAR_END)["options"] == []


def test_two_transfers_use_the_latest_first_ride_that_connects(three_lines):
    # An earlier bus also makes the train, with a long wait at B: the later one wins.
    _trip(three_lines, "bus", "t0", "R1", "WK", [("A", "08:04:00"), ("B", "08:06:00")])
    p = LivePlanner(three_lines, cfg=LiveSettings(), now=NOW, airports=())
    (opt,) = p.plan(START, FAR_END)["options"]
    assert opt["id"].startswith("bus:t1|")


BWI_LIKE = AirportLink(
    code="TST", name="Test Airport", terminal=(AIRPORT.lat, AIRPORT.lon), area_m=1500,
    station=(38.9605, -77.0000), station_name="Station Y", station_m=150, mode="shuttle",
    to_station="Take the shuttle to the station.",
    from_station="Take the shuttle to the terminal.", source="https://example.org",
    minutes=10, every_min=12)


def test_airport_shuttle_reaches_the_rail_station(net):  # noqa: F811
    _trip(net, "bus", "t1", "R1", "WK", [("A", "08:05:00"), ("B", "08:15:00")])
    _trip(net, "rail", "t2", "R2", "WK", [("X", "08:22:00"), ("Y", "08:35:00")])
    net.commit()
    assert LivePlanner(net, now=NOW, airports=()).plan(START, AIRPORT)["options"] == []
    p = LivePlanner(net, cfg=LiveSettings(), now=NOW, airports=(BWI_LIKE,))
    (opt,) = p.plan(START, AIRPORT)["options"]
    last = opt["legs"][-1]
    assert last["airport"]["mode"] == "shuttle" and last["airport"]["shuttle_s"] == 16 * 60
    assert last["airport"]["text"] == "Take the shuttle to the terminal."
    assert last["duration_s"] >= 16 * 60
    assert opt["walk_s"] < 10 * 60                      # the shuttle ride isn't walking
    # From the airport: the shuttle comes first.
    back = p.plan(AIRPORT, START)
    assert back["options"] == [] or back["options"][0]["legs"][0]["airport"]["text"] == (
        "Take the shuttle to the station.")


def test_airport_walkway_uses_the_airports_walk_time(net):  # noqa: F811
    _trip(net, "rail", "t2", "R2", "WK", [("X", "08:22:00"), ("Y", "08:35:00")])
    net.commit()
    walkway = AirportLink(**{**BWI_LIKE.__dict__, "mode": "walkway", "minutes": 5,
                             "every_min": None})
    p = LivePlanner(net, cfg=LiveSettings(), now=NOW, airports=(walkway,))
    (opt,) = p.plan(Point(38.9404, -77.0105, "At X"), AIRPORT)["options"]
    assert opt["legs"][-1]["duration_s"] == 300 and opt["legs"][-1]["airport"]["shuttle_s"] == 0


def test_service_running_yesterday_and_today_counts_stops_once(net):  # noqa: F811
    net.conn.execute("INSERT INTO gtfs_calendar (feed_id, service_id, monday, tuesday, "
                     "wednesday, thursday, friday, saturday, sunday, start_date, end_date) "
                     "VALUES ('bus', 'DAILY', 1, 1, 1, 1, 1, 1, 1, '20260101', '20271231')")
    _trip(net, "bus", "t1", "R1", "DAILY", [("A", "08:06:00"), ("B", "08:15:00"),
                                             ("C", "08:20:00")])
    net.commit()
    p = LivePlanner(net, cfg=LiveSettings(), now=NOW, airports=())
    (opt,) = p.plan(START, Point(38.9200, -77.0300))["options"]
    assert opt["legs"][1]["stops"] == 2


def test_the_three_airports_load():
    links = {a.code: a for a in load_links()}
    assert set(links) == {"BWI", "DCA", "IAD"}
    bwi = links["BWI"]
    assert bwi.mode == "shuttle" and bwi.wait_s(2 * 3600) == 25 * 30
    assert bwi.wait_s(12 * 3600) == 15 * 30
    assert links["DCA"].mode == links["IAD"].mode == "walkway" and links["IAD"].minutes == 5
    assert bwi.at_airport(39.1774, -76.6684) and not bwi.at_airport(39.1923, -76.6946)
    assert "baggage claim" in bwi.info("origin")["text"]


def test_headway_trips_become_one_trip_per_run(tmp_db):
    db = tmp_db
    db.conn.execute("INSERT INTO gtfs_trips (feed_id, trip_id, route_id, service_id) "
                    "VALUES ('sh', 'loop', 'R', 'S')")
    db.conn.executemany(
        "INSERT INTO gtfs_stop_times (feed_id, trip_id, stop_id, stop_sequence, "
        "arrival_time, departure_time) VALUES ('sh', 'loop', ?, ?, ?, ?)",
        [("G", 1, "00:00:00", "00:00:00"), ("T", 2, "00:06:00", "00:06:00")])
    db.conn.execute("INSERT INTO gtfs_frequencies (feed_id, trip_id, start_time, end_time, "
                    "headway_secs) VALUES ('sh', 'loop', '08:00:00', '09:00:00', 1200)")
    assert expand_frequencies(db, "sh") == 3
    rows = db.conn.execute("SELECT trip_id, stop_id, departure_time FROM gtfs_stop_times "
                           "WHERE feed_id = 'sh' ORDER BY trip_id, stop_sequence").fetchall()
    assert [tuple(r) for r in rows][:2] == [("loop@08:00:00", "G", "08:00:00"),
                                           ("loop@08:00:00", "T", "08:06:00")]
    assert {r[0] for r in rows} == {"loop@08:00:00", "loop@08:20:00", "loop@08:40:00"}
    assert db.conn.execute("SELECT service_id FROM gtfs_trips WHERE trip_id = "
                           "'loop@08:40:00'").fetchone()[0] == "S"
    assert expand_frequencies(db, "sh") == 0              # already expanded
