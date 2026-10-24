from __future__ import annotations

from transit_mcp.retrieval import connect


def test_plan_connection_builds_two_leg_option_with_hub(region_db):
    plan = connect.plan_connection(region_db, "Downtown A", "Office Park")
    assert not plan.get("error")
    assert plan["options"]
    assert plan["engine"] == "heuristic"
    opt = plan["options"][0]
    systems = [leg["system"] for leg in opt["legs"] if leg["mode"] == "ride"]
    assert systems == ["Mini A Transit", "Mini B Transit"]
    assert opt["transfers"] == 1
    assert any(leg["mode"] == "walk" for leg in opt["legs"])


def test_plan_connection_feasibility_respects_day(region_db):
    sun = connect.plan_connection(region_db, "Downtown A", "Office Park", when="sunday")
    opt = sun["options"][0]
    assert opt["feasible"] is False
    assert "sunday" in opt["feasibility"].lower()

    wk = connect.plan_connection(region_db, "Downtown A", "Office Park", when="weekday")
    assert wk["options"][0]["feasible"] is True


def test_estimate_trip_cost_sums_leg_fares(region_db):
    est = connect.estimate_trip_cost(region_db, "Downtown A", "Office Park")
    opt = est["options"][0]
    assert opt["total_estimate"] == 5.5  # 2.00 (Mini A) + 3.50 (Mini B)
    pays = [leg["pay_with"] for leg in opt["per_leg"]]
    assert "SmarTrip" in pays and "Mobile app" in pays
    assert "separate payment" in opt["note"].lower()


def test_plan_connection_unresolvable_place(region_db):
    out = connect.plan_connection(region_db, "Nowheresville XYZ", "Office Park")
    assert out["error"] == "could_not_resolve"
