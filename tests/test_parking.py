"""Parking and park & ride (planning/parking.py, parking.toml, the find_parking tool)."""

from __future__ import annotations

import json

from transit_mcp.db.database import Database, init_db
from transit_mcp.ingest.wmata_api import parking_details
from transit_mcp.planning import parking

WMATA = {"StationsParking": [
    {"Code": "E10", "Notes": "Oversize lot - 60 spaces.",
     "AllDayParking": {"TotalCount": 3399, "RiderCost": 4.95, "NonRiderCost": 8.95,
                       "SaturdayRiderCost": 0, "SaturdayNonRiderCost": 0},
     "ShortTermParking": {"TotalCount": 61, "Notes": "Parking available 5 AM - 2 AM."}},
    {"Code": "A15", "Notes": None,
     "AllDayParking": {"TotalCount": 5745, "RiderCost": 5.2, "NonRiderCost": 5.2},
     "ShortTermParking": {"TotalCount": 0, "Notes": None}},
    {"Code": "A07", "Notes": None, "AllDayParking": {"TotalCount": 0},
     "ShortTermParking": {"TotalCount": 0}},          # no parking at all: left out
]}

CURATED = """
[rules.wmata]
source = "https://www.wmata.com/ride/guides/parking.html"
checked = "2026-11-16"
text = "Free for riders on weekends."

[rules.marc]
source = "https://www.mta.maryland.gov/marc-parking-details"
checked = "2026-11-16"
text = "Most MARC lots are free."

[[lot]]
id = "marc-odenton"
operator = "MARC"
kind = "station"
name = "Odenton MARC station"
stop = "marc:ODENTON MARC"
spaces = 1977
cost = "Free"
source = "https://www.mta.maryland.gov/marc-parking-details"
checked = "2026-11-16"

[[lot]]
id = "marc-greenbelt"
operator = "MARC"
same_as = "wmata:E10"
name = "Greenbelt (Metro lot)"
notes = "Residential streets nearby are permit-only."
source = "https://www.mta.maryland.gov/marc-parking-details"
checked = "2026-11-16"

[[lot]]
id = "vre-crystal-city"
operator = "VRE"
kind = "station"
name = "Crystal City VRE station"
at = [38.8573, -77.0507]
available = false
cost = "No parking"
source = "https://www.vre.org/"
checked = "2026-11-16"

[[lot]]
id = "dca-parking"
operator = "Reagan National Airport"
kind = "airport"
name = "Reagan National Airport (DCA) parking"
airport = "DCA"
cost = "$7 an hour"
options = [{name = "Parking 1", spaces = 1080, cost = "$7 an hour, $35 a day"}]
source = "https://www.flyreagan.com/parking-transportation/parking-information"
checked = "2026-11-16"
"""


def _db(tmp_path):
    init_db(tmp_path / "t.db")
    db = Database(tmp_path / "t.db")
    c = db.conn
    c.executemany("INSERT OR REPLACE INTO stations (code, name, lat, lon, address_json) VALUES (?, ?, ?, ?, ?)",
                  [("E10", "Greenbelt", 39.011, -76.911,
                    json.dumps({"Street": "5717 Greenbelt Metro Dr", "City": "Greenbelt",
                                "State": "MD"})),
                   ("A15", "Shady Grove", 39.1198, -77.1649, "{}")])
    c.execute("INSERT OR IGNORE INTO gtfs_feeds (feed_id, agency_name, mode, source_url, status) "
              "VALUES ('marc', 'MARC', 'commuter_rail', '', 'loaded')")
    c.executemany("INSERT OR IGNORE INTO gtfs_stops (feed_id, stop_id, name, lat, lon) VALUES (?, ?, ?, ?, ?)",
                  [("marc", "11992", "ODENTON MARC nb", 39.0870, -76.7063),
                   ("marc", "11985", "ODENTON MARC sb", 39.0868, -76.7061)])
    db.replace_wmata_parking(parking_details(WMATA))
    db.commit()
    return db


def test_wmata_parking_is_read_with_rates_and_notes():
    rows = {r["code"]: r for r in parking_details(WMATA)}
    assert set(rows) == {"E10", "A15"}
    assert rows["E10"]["rider_cost"] == 4.95 and rows["E10"]["short_term_spaces"] == 61
    assert rows["A15"]["short_term_notes"] is None


def test_lots_merge_shared_metro_lots_and_find_their_places(tmp_path):
    path = tmp_path / "parking.toml"
    path.write_text(CURATED, "utf-8")
    db = _db(tmp_path)
    try:
        lots = {lot["id"]: lot for lot in parking.all_lots(db, path)}
    finally:
        db.close()
    greenbelt = lots["wmata-E10"]
    assert greenbelt["spaces"] == 3399 and greenbelt["also_serves"] == ["MARC"]
    assert "$4.95 a day for Metro riders, $8.95 for non-riders" in greenbelt["cost"]
    assert "permit-only" in greenbelt["notes"] and "Oversize" in greenbelt["notes"]
    assert greenbelt["address"] == "5717 Greenbelt Metro Dr, Greenbelt, MD"
    assert "for non-riders" not in lots["wmata-A15"]["cost"]          # same price for all
    assert "marc-greenbelt" not in lots                               # folded into Metro's
    odenton = lots["marc-odenton"]
    assert round(odenton["lat"], 3) == 39.087 and odenton["rules"] == "marc"
    assert lots["vre-crystal-city"]["available"] is False
    dca = lots["dca-parking"]
    assert dca["options"][0]["name"] == "Parking 1" and abs(dca["lat"] - 38.85) < 0.02


def test_near_lists_the_closest_lots_first(tmp_path, monkeypatch):
    path = tmp_path / "parking.toml"
    path.write_text(CURATED, "utf-8")
    monkeypatch.setattr(parking, "DEFAULT_FILE", path)
    monkeypatch.setattr(parking.all_lots, "__defaults__", (path,))
    db = _db(tmp_path)
    try:
        found = parking.near(db, 39.0869, -76.7062, radius_m=1000)
        assert [lot["id"] for lot in found] == ["marc-odenton"] and found[0]["distance_m"] < 50
        assert parking.near(db, 38.0, -76.0, radius_m=1000) == []
    finally:
        db.close()


def test_find_parking_tool(tmp_path, monkeypatch):
    from transit_mcp.planning.live import Point
    from transit_mcp.server import app as server

    path = tmp_path / "parking.toml"
    path.write_text(CURATED, "utf-8")
    monkeypatch.setattr(parking.all_lots, "__defaults__", (path,))
    monkeypatch.setattr(parking.rules, "__defaults__", (path,))
    monkeypatch.setattr(server, "_db", lambda: _db(tmp_path))
    monkeypatch.setattr(server, "_place", lambda db, text: Point(39.011, -76.911, "Greenbelt")
                        if text == "Greenbelt" else None)
    out = server.find_parking("Greenbelt", radius_m=500)
    assert out["lots"][0]["id"] == "wmata-E10" and set(out["rules"]) == {"wmata"}
    assert "fill early" in out["note"] and "confirm" not in out["note"]
    assert server.find_parking("Nowhere")["error"] == "place_not_found"


def test_every_curated_lot_names_its_source():
    data = parking.curated(parking.DEFAULT_FILE)
    ids = [lot["id"] for lot in data["lot"]]
    assert len(ids) == len(set(ids))
    for lot in data["lot"]:
        assert lot["source"].startswith("https://") and lot["checked"], lot["id"]
        assert lot.get("same_as") or lot.get("stop") or lot.get("airport") or lot.get("at")
        for option in lot.get("options", []):
            assert option["name"] and option["cost"], lot["id"]
    for name, rule in data["rules"].items():
        assert rule["source"].startswith("https://") and rule["text"], name
