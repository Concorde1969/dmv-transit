"""Trip-planning entry points used by the MCP tools.

The actual planning is done by whichever engine ``PLANNER_ENGINE`` selects (see
:mod:`transit_mcp.planning`); this module turns the rider's free text into a
``TripRequest`` and shapes the ``TripPlan`` for the assistant.
"""

from __future__ import annotations

from pathlib import Path

from transit_mcp.config import Settings, get_settings
from transit_mcp.db.database import Database
from transit_mcp.planning import UnresolvedPlace, build_request, get_planner
from transit_mcp.retrieval.disruptions import TripScope, for_trip

_SEPARATE_PAYMENTS = (
    "Each leg is a separate payment unless a pass or a transfer discount applies — "
    "check each agency's fare page (fare_page) for its transfer rules."
)


def _settings_for(db: Database, settings: Settings | None) -> Settings:
    """Planner settings pointed at the same database the caller is reading."""
    settings = settings or get_settings()
    if Path(settings.db_path).resolve() != Path(db.path).resolve():
        settings = settings.model_copy(update={"db_path": Path(db.path)})
    return settings


def plan_connection(
    db: Database, origin: str, destination: str, when: str | None = None,
    *, settings: Settings | None = None,
) -> dict:
    settings = _settings_for(db, settings)
    try:
        request = build_request(db, origin, destination, day=when, settings=settings)
    except UnresolvedPlace as exc:
        return {
            "error": "could_not_resolve",
            "origin_resolved": exc.origin_label,
            "destination_resolved": exc.destination_label,
        }
    plan = get_planner(settings).plan(request)
    out = plan.model_dump(mode="json", exclude_none=True)
    scope = TripScope()
    for opt in out.get("options", []):
        for lg in opt.get("legs", []):
            if lg.get("mode", "ride") == "ride":
                scope.add(lg.get("agency"), lg.get("route_id"),
                          (lg.get("board_stop_id"), lg.get("alight_stop_id")))
    out.update(for_trip(db, scope))
    return out


def estimate_trip_cost(
    db: Database, origin: str, destination: str, when: str | None = None,
    rider_category: str | None = None, *, settings: Settings | None = None,
) -> dict:
    plan = plan_connection(db, origin, destination, when, settings=settings)
    if plan.get("error"):
        return plan
    options = []
    for opt in plan.get("options", []):
        rides = [lg for lg in opt.get("legs", []) if lg.get("mode", "ride") == "ride"]
        known = [lg["fare"] for lg in rides if isinstance(lg.get("fare"), (int, float))]
        options.append({
            "route": opt.get("summary"),
            "feasible": opt.get("feasible"),
            "per_leg": [
                {"system": lg.get("system"), "pay_with": lg.get("pay_with"),
                 "fare": lg.get("fare"), "fare_page": lg.get("fare_page")}
                for lg in rides
            ],
            "total_estimate": round(sum(known), 2) if known else None,
            "unknown_legs": len(rides) - len(known),
            "note": _SEPARATE_PAYMENTS,
        })
    return {
        "origin": plan.get("origin"),
        "destination": plan.get("destination"),
        "rider_category": rider_category or "regular",
        "engine": plan.get("engine"),
        "options": options,
        "warnings": plan.get("warnings") or None,
        "caveat": plan.get("caveat"),
    }
