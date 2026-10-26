"""Leave-now planner on a tiny made-up network (Monday 2026-10-26, 08:00 Eastern).

    bus feed:   A ──R1── B ──R1── C            (A near START, C near END_DIRECT)
                                 B and X are ~60 m apart (same cluster)
    rail feed:              X ──R2── Y         (Y near END_TRANSFER)
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from transit_mcp.ingest.gtfs.clusters import build_clusters
from transit_mcp.planning.live import LivePlanner, LiveSettings, Point
from transit_mcp.planning.realtime import parse_departures

TZ = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 26, 8, 0, tzinfo=TZ)          # a Monday
START = Point(38.9000, -77.0300, "Start")
END_DIRECT = Point(38.9200, -77.0300, "Direct end")
END_TRANSFER = Point(38.9600, -77.0000, "Transfer end")

STOPS = [  # feed, stop_id, name, lat, lon
    ("bus", "A", "Stop A", 38.9005, -77.0300),
    ("bus", "B", "Stop B", 38.9400, -77.0100),
    ("bus", "C", "Stop C", 38.9205, -77.0300),
    ("rail", "X", "Station X", 38.9404, -77.0105),
    ("rail", "Y", "Station Y", 38.9605, -77.0000),
]


def _trip(db, feed, trip_id, route, service, calls, shape=None):
    db.conn.execute("INSERT INTO gtfs_trips (feed_id, trip_id, route_id, service_id, "
                    "headsign, shape_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (feed, trip_id, route, service, f"to {calls[-1][0]}", shape))
    db.conn.executemany(
        "INSERT INTO gtfs_stop_times (feed_id, trip_id, stop_id, stop_sequence, "
        "arrival_time, departure_time) VALUES (?, ?, ?, ?, ?, ?)",
        [(feed, trip_id, stop, i + 1, t, t) for i, (stop, t) in enumerate(calls)])


@pytest.fixture
def net(tmp_db):
    db = tmp_db
    db.conn.executemany("INSERT INTO gtfs_stops (feed_id, stop_id, name, lat, lon) "
                        "VALUES (?, ?, ?, ?, ?)", STOPS)
    for feed in ("bus", "rail"):
        db.conn.execute("INSERT INTO gtfs_calendar (feed_id, service_id, monday, tuesday, "
                        "wednesday, thursday, friday, start_date, end_date) VALUES "
                        "(?, 'WK', 1, 1, 1, 1, 1, '20260101', '20271231')", (feed,))
    db.conn.executemany("INSERT INTO gtfs_routes (feed_id, route_id, short_name, color, "
                        "route_type) VALUES (?, ?, ?, ?, ?)",
                        [("bus", "R1", "10", "C8102E", 3), ("rail", "R2", "Red", "BF0D3E", 1)])
    db.commit()
    return db


def plan(db, end, now=NOW, realtime=None, agencies=None, **cfg):
    p = LivePlanner(db, cfg=LiveSettings(**cfg), realtime=realtime,
                    agencies=agencies or {}, now=now)
    build_clusters(db)
    return p.plan(START, end)


def test_direct_trip_leaving_soon(net):
    _trip(net, "bus", "t1", "R1", "WK", [("A", "08:06:00"), ("B", "08:15:00"),
                                          ("C", "08:20:00")])
    res = plan(net, END_DIRECT)
    (opt,) = res["options"]
    ride = opt["legs"][1]
    assert opt["transfers"] == 0 and ride["route_short_name"] == "10"
    assert ride["board"]["name"] == "Stop A" and ride["alight"]["name"] == "Stop C"
    assert ride["color"] == "#C8102E" and ride["stops"] == 2
    assert 0 <= opt["wait_s"] <= 600 and opt["live"] is False
    assert res["window_min"] == 60
    assert opt["legs"][0]["mode"] == "walk" and opt["legs"][-1]["mode"] == "walk"


def test_later_departures_in_the_next_hour_are_offered(net):
    _trip(net, "bus", "t1", "R1", "WK", [("A", "08:25:00"), ("C", "08:40:00")])  # 25 min
    _trip(net, "bus", "t2", "R1", "WK", [("A", "09:30:00"), ("C", "09:45:00")])  # too late
    (opt,) = plan(net, END_DIRECT)["options"]
    assert opt["id"] == "bus:t1" and opt["wait_s"] > 20 * 60


def test_nothing_in_the_next_hour(net):
    _trip(net, "bus", "t2", "R1", "WK", [("A", "09:30:00"), ("C", "09:45:00")])
    res = plan(net, END_DIRECT)
    assert res["options"] == [] and "No trips in the next 60 minutes" in res["notice"]


def test_stops_farther_than_300_m_are_used_with_their_walk(net):
    _trip(net, "bus", "t1", "R1", "WK", [("A", "08:20:00"), ("C", "08:35:00")])
    far_start = Point(38.9005, -77.0200, "About 870 m east of stop A")
    p = LivePlanner(net, cfg=LiveSettings(), now=NOW)
    (opt,) = p.plan(far_start, END_DIRECT)["options"]
    assert opt["legs"][0]["mode"] == "walk" and 12 * 60 < opt["legs"][0]["duration_s"] < 20 * 60
    assert opt["walk_s"] >= opt["legs"][0]["duration_s"]


def test_only_each_agencys_closest_stops(net):
    p = LivePlanner(net, cfg=LiveSettings(stops_per_feed=1, max_walk_m=10_000), now=NOW)
    near = p.stops_near(START)
    assert sorted((s.feed, s.stop_id) for s in near) == [("bus", "A"), ("rail", "X")]


def test_missed_bus_is_thrown_out(net):
    _trip(net, "bus", "t1", "R1", "WK", [("A", "07:58:00"), ("C", "08:10:00")])
    assert plan(net, END_DIRECT)["options"] == []


def test_one_transfer_within_a_cluster(net):
    _trip(net, "bus", "t1", "R1", "WK", [("A", "08:05:00"), ("B", "08:15:00")])
    _trip(net, "rail", "t2", "R2", "WK", [("X", "08:22:00"), ("Y", "08:35:00")])
    res = plan(net, END_TRANSFER)
    (opt,) = res["options"]
    assert opt["transfers"] == 1
    walk = opt["legs"][2]
    assert walk["mode"] == "walk" and walk["transfer"] and 60 <= walk["wait_s"] <= 600
    assert [leg["route_short_name"] for leg in opt["legs"] if leg["mode"] == "ride"] == [
        "10", "Red"]


def test_long_wait_at_a_transfer_is_thrown_out(net):
    _trip(net, "bus", "t1", "R1", "WK", [("A", "08:05:00"), ("B", "08:15:00")])
    _trip(net, "rail", "t2", "R2", "WK", [("X", "08:45:00"), ("Y", "08:58:00")])  # 28 min
    assert plan(net, END_TRANSFER)["options"] == []


def test_transfer_options_only_when_they_help(net):
    _trip(net, "bus", "t1", "R1", "WK", [("A", "08:05:00"), ("B", "08:15:00")])
    _trip(net, "rail", "t2", "R2", "WK", [("X", "08:22:00"), ("Y", "08:35:00")])
    _trip(net, "bus", "t3", "R1", "WK", [("A", "08:06:00"), ("C", "08:20:00")])
    res = plan(net, END_DIRECT)
    assert all(o["transfers"] == 0 for o in res["options"])


def test_after_midnight_trip_from_yesterdays_service(net):
    _trip(net, "bus", "late", "R1", "WK", [("A", "24:08:00"), ("C", "24:20:00")])
    tuesday_0002 = datetime(2026, 10, 27, 0, 2, tzinfo=TZ)
    (opt,) = plan(net, END_DIRECT, now=tuesday_0002)["options"]
    assert opt["depart_at"] == int(datetime(2026, 10, 27, 0, 8, tzinfo=TZ).timestamp())


class FakeRealtime:
    def __init__(self, by_stop):
        self.by_stop = by_stop
        self.calls = []

    def available(self):
        return True

    def free(self, feed, stop):
        return False

    def lookup(self, feed, stop_id, trip_id, seq, sched_dep_s, day):
        self.calls.append((feed, stop_id))
        return (self.by_stop.get((feed, stop_id)) or {}).get(trip_id), 1


AGENCIES = {"bus": {"name": "Bus Co", "has_realtime": True, "base_fare": 2.0},
            "rail": {"name": "Rail Co", "has_realtime": False}}


def test_live_delay_moves_times_and_is_labelled(net):
    _trip(net, "bus", "t1", "R1", "WK", [("A", "08:06:00"), ("C", "08:20:00")])
    rt = FakeRealtime({("bus", "A"): {"t1": {"dep_s": 8 * 3600 + 9 * 60, "delay": 180,
                                              "live": True, "cancelled": False}}})
    (opt,) = plan(net, END_DIRECT, realtime=rt, agencies=AGENCIES)["options"]
    ride = opt["legs"][1]
    assert opt["live"] and ride["delay_s"] == 180
    assert ride["board"]["expected"] - ride["board"]["scheduled"] == 180
    assert ride["alight"]["expected"] - ride["alight"]["scheduled"] == 180  # carried on
    assert opt["fare_estimate"] == 2.0
    assert rt.calls == [("bus", "A")]


def test_a_very_late_bus_is_still_offered_at_its_live_time(net):
    _trip(net, "bus", "t1", "R1", "WK", [("A", "08:06:00"), ("C", "08:20:00")])
    rt = FakeRealtime({("bus", "A"): {"t1": {"dep_s": 8 * 3600 + 19 * 60, "delay": 780,
                                              "live": True, "cancelled": False}}})
    (opt,) = plan(net, END_DIRECT, realtime=rt, agencies=AGENCIES)["options"]
    assert opt["depart_at"] == int(datetime(2026, 10, 26, 8, 19, tzinfo=TZ).timestamp())


def test_cancelled_trip_is_dropped(net):
    _trip(net, "bus", "t1", "R1", "WK", [("A", "08:06:00"), ("C", "08:20:00")])
    rt = FakeRealtime({("bus", "A"): {"t1": {"dep_s": None, "delay": None, "live": True,
                                              "cancelled": True}}})
    assert plan(net, END_DIRECT, realtime=rt, agencies=AGENCIES)["options"] == []


def test_no_live_calls_for_feeds_without_realtime(net):
    _trip(net, "rail", "t9", "R2", "WK", [("X", "08:05:00"), ("Y", "08:20:00")])
    rt = FakeRealtime({})
    p = LivePlanner(net, realtime=rt, agencies=AGENCIES, now=NOW)
    p.plan(Point(38.9404, -77.0105), END_TRANSFER)
    assert rt.calls == []


def test_no_stops_nearby(net):
    res = plan(net, Point(39.5, -76.0))
    assert res["options"] == [] and "within about a mile" in res["notice"]


def test_parse_transitland_departures():
    data = {"stops": [{"departures": [
        {"trip": {"trip_id": "t1"}, "schedule_relationship": "SCHEDULED",
         "departure": {"scheduled": "08:06:00", "estimated_utc": "2026-10-26T12:09:00Z",
                       "delay": 180}},
        {"trip": {"trip_id": "t2"}, "schedule_relationship": "STATIC",
         "departure": {"scheduled": "08:30:00"}},
        {"trip": {"trip_id": "t3", "schedule_relationship": "CANCELED"},
         "departure": {"scheduled": "08:40:00"}},
    ]}]}
    out = parse_departures(data, NOW.date(), TZ)
    assert out["t1"] == {"dep_s": 8 * 3600 + 9 * 60, "delay": 180, "live": True,
                         "cancelled": False}
    assert out["t2"]["live"] is False and out["t2"]["dep_s"] is None
    assert out["t3"]["cancelled"] is True


def test_transfer_trip_gets_off_where_the_rider_arrives_soonest(net):
    # Y2 comes first on the train but is ~800 m from the end; Y is next to it.
    net.conn.execute("INSERT INTO gtfs_stops (feed_id, stop_id, name, lat, lon) VALUES "
                     "('rail', 'Y2', 'Station Y2', 38.9530, -77.0000)")
    _trip(net, "bus", "t1", "R1", "WK", [("A", "08:05:00"), ("B", "08:15:00")])
    _trip(net, "rail", "t2", "R2", "WK", [("X", "08:22:00"), ("Y2", "08:30:00"),
                                          ("Y", "08:35:00")])
    (opt,) = plan(net, END_TRANSFER)["options"]
    assert opt["legs"][-2]["alight"]["name"] == "Station Y"
