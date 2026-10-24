from __future__ import annotations

from transit_mcp.retrieval import regional


def test_list_agencies_includes_wmata_and_loaded_feeds(region_db):
    rows = regional.list_agencies(region_db)
    by = {r["feed_id"]: r for r in rows}
    assert rows[0]["feed_id"] == "wmata"
    assert by["wmata"]["fare_headline"]
    assert "mini-a" in by and by["mini-a"]["status"] == "loaded"
    # A real agency seed carries a role even before its feed is loaded.
    assert by["vre"]["role"] and by["vre"]["frequency_tier"] == "peak"


def test_find_stops_near_spans_agencies(region_db):
    res = regional.find_stops_near(region_db, "Downtown A", radius_m=400)
    agencies = {s["agency"] for s in res["stops"]}
    assert "mini-a" in agencies and "wmata" in agencies
    assert res["stops"][0]["walk_min"] >= 1


def test_get_route_reports_service_window(region_db):
    r = regional.get_route(region_db, "mini-b", "9")
    assert r["route"].startswith("9")
    assert r["runs"] == "Mon–Fri"
    assert r["weekend"] == {"saturday": False, "sunday": False}


def test_transfers_at_flags_feasibility_by_day(region_db):
    sat = regional.transfers_at(region_db, "Hub Stop", day="saturday")
    minib = sat["connections"].get("mini-b")
    assert minib and minib[0]["feasible"] is False
    assert "saturday" in minib[0]["feasibility"].lower()

    wkday = regional.transfers_at(region_db, "Hub Stop", day="wednesday")
    assert wkday["connections"]["mini-b"][0]["feasible"] is True


def test_how_to_pay_returns_methods_and_sources(region_db):
    hp = regional.how_to_pay(region_db, "vre")
    assert "VRE Mobile app" in " ".join(hp["payment_methods"])
    assert hp["accepts_smartrip"] is False
    assert hp["fare_page"].startswith("http")

    free = regional.how_to_pay(region_db, "dash")
    assert free["base_fare"] == 0
