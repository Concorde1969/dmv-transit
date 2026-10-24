"""Metrorail station resolution, line-change routing, and the planner's Metro legs.

Regression cover for the "Silver Line from DCA to Dulles nonstop" error: airport names
must resolve to the right station, and a trip that needs a change of trains must say so.
"""

from __future__ import annotations

import json

import pytest

from transit_mcp.retrieval import connect, metro
from transit_mcp.retrieval.queries import lookup_alias, resolve_station

STATIONS = {
    # code: (name, lat, lon, lines, together)
    "J03": ("Franconia-Springfield", 38.7665, -77.1681, ["BL"], []),
    "C10": ("Ronald Reagan Washington National Airport", 38.8534, -77.0441, ["BL", "YL"], []),
    "C05": ("Rosslyn", 38.8960, -77.0718, ["BL", "OR", "SV"], []),
    "C04": ("Foggy Bottom-GWU", 38.9007, -77.0503, ["BL", "OR", "SV"], []),
    "C01": ("Metro Center", 38.8983, -77.0281, ["BL", "OR", "SV"], ["A01"]),
    "D01": ("Federal Triangle", 38.8932, -77.0282, ["BL", "OR", "SV"], []),
    "N12": ("Ashburn", 39.0054, -77.4912, ["SV"], []),
    "N10": ("Washington Dulles International Airport", 38.9558, -77.4482, ["SV"], []),
    "N06": ("Wiehle-Reston East", 38.9478, -77.3402, ["SV"], []),
    "K05": ("East Falls Church", 38.8859, -77.1568, ["OR", "SV"], []),
    "A15": ("Shady Grove", 39.1199, -77.1646, ["RD"], []),
    "A01": ("Metro Center", 38.8983, -77.0280, ["RD"], ["C01"]),
    "B01": ("Gallery Pl-Chinatown", 38.8983, -77.0219, ["RD"], []),
}
LINES = {
    "BL": ("Blue Line", ["J03", "C10", "C05", "C04", "C01", "D01"]),
    "SV": ("Silver Line", ["N12", "N10", "N06", "K05", "C05", "C04", "C01", "D01"]),
    "RD": ("Red Line", ["A15", "A01", "B01"]),
}


@pytest.fixture
def metro_db(tmp_db):
    for code, (name, lat, lon, lines, together) in STATIONS.items():
        tmp_db.conn.execute(
            "INSERT INTO stations (code, name, lat, lon, line_codes, together_codes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (code, name, lat, lon, json.dumps(lines), json.dumps(together)),
        )
    for code, (display, seq) in LINES.items():
        tmp_db.conn.execute(
            "INSERT INTO lines (code, display_name) VALUES (?, ?)", (code, display)
        )
        tmp_db.conn.executemany(
            "INSERT INTO rail_line_stops (line_code, seq, station_code) VALUES (?, ?, ?)",
            [(code, i, c) for i, c in enumerate(seq)],
        )
    tmp_db.conn.executemany(
        "INSERT INTO place_aliases (alias, feed_id, stop_id) VALUES (?, 'wmata', ?)",
        [("dca", "C10"), ("reagan", "C10"), ("iad", "N10"), ("dulles", "N10")],
    )
    tmp_db.commit()
    return tmp_db


@pytest.mark.parametrize("text, code", [
    ("DCA", "C10"),
    ("Reagan", "C10"),
    ("National Airport", "C10"),        # used to resolve to Dulles
    ("IAD", "N10"),
    ("dulles", "N10"),
    ("Washington Dulles International Airport", "N10"),
    ("Rosslyn station", "C05"),
    ("rosslyn metro", "C05"),
    ("C05", "C05"),
    ("Shady Grove", "A15"),
])
def test_resolve_station(metro_db, text, code):
    assert resolve_station(metro_db, text)["code"] == code


def test_unknown_station_does_not_resolve(metro_db):
    assert resolve_station(metro_db, "Union Station") is None


def test_wmata_definition_seeds_airport_aliases(tmp_db, isolated_settings):
    from transit_mcp.agencies import load_agencies, seed_curated_tables

    seed_curated_tables(tmp_db, load_agencies(isolated_settings))
    assert lookup_alias(tmp_db, "DCA")["stop_id"] == "C10"
    assert lookup_alias(tmp_db, " Dulles Airport ")["stop_id"] == "N10"
    assert lookup_alias(tmp_db, "Old Town Alexandria")["stop_id"] == "C13"


def test_dca_to_iad_requires_change_at_rosslyn(metro_db):
    r = metro.metro_route(metro_db, "DCA", "IAD")
    assert r["direct"] is False
    assert r["transfers"] == 1
    assert r["change_trains_at"] == ["Rosslyn"]
    assert [leg["line"] for leg in r["legs"]] == ["BL", "SV"]
    assert r["legs"][0]["toward"] == "Federal Triangle"
    assert r["legs"][1]["toward"] == "Ashburn"
    assert r["legs"][1]["stops"] == 3
    assert "Change to the Silver Line" in r["summary"]


def test_shared_track_is_direct(metro_db):
    r = metro.metro_route(metro_db, "Wiehle-Reston East", "Federal Triangle")
    assert r["transfers"] == 0
    assert r["legs"][0]["line"] == "SV"


def test_station_complex_counts_as_one_place(metro_db):
    r = metro.metro_route(metro_db, "Shady Grove", "Rosslyn")
    assert r["transfers"] == 1
    assert r["change_trains_at"] == ["Metro Center"]
    assert r["legs"][0]["line"] == "RD"


def test_metro_route_errors(metro_db):
    assert metro.metro_route(metro_db, "Nowhere", "Rosslyn")["error"] == "station_not_found"
    assert metro.metro_route(metro_db, "Rosslyn", "C05")["error"] == "same_station"
    metro_db.conn.execute("DELETE FROM rail_line_stops")
    assert metro.metro_route(metro_db, "DCA", "IAD")["error"] == "line_data_not_loaded"


def test_planner_never_calls_a_line_change_direct(metro_db, isolated_settings):
    plan = connect.plan_connection(metro_db, "DCA", "IAD")
    assert plan["options"], plan
    best = plan["options"][0]
    rides = [lg for lg in best["legs"] if lg["mode"] == "ride"]
    assert [lg["route"] for lg in rides] == ["Blue Line", "Silver Line"]
    assert best["transfers"] == 1
    assert "Rosslyn" in best["summary"]
    assert "no transfer" not in best["summary"].lower()
    # one Metro fare covers the whole trip
    assert rides[1]["fare"] == 0.0


# --- schedules from the Metrorail GTFS -------------------------------------------
def _rail_gtfs(db):
    """A tiny rail GTFS: Blue runs every day; Silver Rosslyn->Dulles only Mon-Sat.
    Platforms hang off parent stations named the WMATA way (STN_<code>)."""
    db.conn.execute(
        "INSERT INTO gtfs_feeds (feed_id, agency_name, status, transitland_id, "
        "feed_start_date, feed_end_date, fetched_at, index_stops) VALUES "
        "('wmata', 'Metrorail (WMATA)', 'loaded', 'f-dqc-wmata~rail', '20261022', "
        "'20261030', datetime('now'), 0)"
    )
    for code in ("C10", "C05", "N10", "C04"):
        db.conn.execute(
            "INSERT INTO gtfs_stops (feed_id, stop_id, name, parent_station) "
            "VALUES ('wmata', ?, ?, ?)", (f"PF_{code}_1", code, f"STN_{code}"))
    db.conn.executemany(
        "INSERT INTO gtfs_calendar (feed_id, service_id, monday, tuesday, wednesday, "
        "thursday, friday, saturday, sunday) VALUES ('wmata', ?, ?, ?, ?, ?, ?, ?, ?)",
        [("WK", 1, 1, 1, 1, 1, 0, 0), ("SAT", 0, 0, 0, 0, 0, 1, 0),
         ("SUN", 0, 0, 0, 0, 0, 0, 1)],
    )
    trips = [  # trip, route, service, [(stop, time), ...]
        ("b-wk", "BLUE", "WK", [("C10", "05:21:00"), ("C05", "05:35:00"), ("C04", "05:38:00")]),
        ("b-late", "BLUE", "WK", [("C10", "23:44:00"), ("C05", "23:58:00")]),
        ("b-sun", "BLUE", "SUN", [("C10", "07:00:00"), ("C05", "07:14:00")]),
        ("s-wk", "SILVER", "WK", [("C05", "05:40:00"), ("N10", "06:20:00")]),
        ("s-sat", "SILVER", "SAT", [("C05", "06:36:00"), ("N10", "07:16:00")]),
        ("s-back", "SILVER", "WK", [("N10", "05:00:00"), ("C05", "05:40:00")]),
    ]
    for trip, route, service, calls in trips:
        db.conn.execute("INSERT INTO gtfs_trips (feed_id, trip_id, route_id, service_id) "
                        "VALUES ('wmata', ?, ?, ?)", (trip, route, service))
        db.conn.executemany(
            "INSERT INTO gtfs_stop_times (feed_id, trip_id, stop_id, stop_sequence, "
            "departure_time) VALUES ('wmata', ?, ?, ?, ?)",
            [(trip, f"PF_{code}_1", i + 1, t) for i, (code, t) in enumerate(calls)],
        )
    db.commit()


def test_metro_route_reports_service_days_and_first_last_trains(metro_db):
    _rail_gtfs(metro_db)
    r = metro.metro_route(metro_db, "DCA", "IAD")
    blue, silver = r["legs"]
    assert blue["service"]["days"] == ["mon", "tue", "wed", "thu", "fri", "sun"]
    assert blue["service"]["first_and_last_trains"][0] == {
        "days": "Mon–Fri", "first_departure": "5:21 am", "last_departure": "11:44 pm"}
    # Silver Rosslyn -> Dulles: no Sunday train (the reverse-direction trip doesn't count)
    assert silver["service"]["days"] == ["mon", "tue", "wed", "thu", "fri", "sat"]
    assert r["service_note"] is None
    assert r["url"] == "https://www.transit.land/feeds/f-dqc-wmata~rail"
    assert "Transitland" in r["source"]


def test_planner_uses_gtfs_days_for_metro(metro_db, isolated_settings):
    _rail_gtfs(metro_db)
    plan = connect.plan_connection(metro_db, "DCA", "IAD", "sunday")
    best = plan["options"][0]
    assert best["feasible"] is False  # Blue runs Sunday, Silver doesn't
    assert "Sunday" in best["feasibility"]


def test_without_rail_gtfs_service_is_not_claimed(metro_db):
    r = metro.metro_route(metro_db, "DCA", "IAD")
    assert all(leg["service"] is None for leg in r["legs"])
    assert "isn't loaded" in r["service_note"]
