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
