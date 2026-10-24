"""Disruptions that touch a trip: live alerts plus the agencies' service-change pages.

Trip answers always come from GTFS schedules; this module adds what may change them
today, so the assistant can give the scheduled route *and* warn the rider:

* live alerts in the ``alerts`` table — WMATA API incidents (Metrorail lines and
  stations, Metrobus routes, elevator/escalator outages) and agencies' GTFS-realtime
  alerts (matched by GTFS route and stop ids);
* the agencies' own alert / service-change web pages (``alerts`` documents), as
  pointers with a short excerpt.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field

from transit_mcp.db.database import Database

# Metrobus incidents come from the WMATA API (stored under "wmata", mode "bus").
_ALERT_FEED = {"metrobus": ("wmata", "bus")}
_MAX_NOTICES_PER_AGENCY = 3
_ROUTE_IN_TEXT = re.compile(r"\broutes?\s+([A-Za-z]*\d+[A-Za-z0-9]*)", re.IGNORECASE)


@dataclass
class TripScope:
    """What a trip uses, per agency: GTFS route ids / Metrorail line codes, and stop
    ids / Metrorail station codes."""

    routes: dict[str, set[str]] = field(default_factory=dict)
    stops: dict[str, set[str]] = field(default_factory=dict)

    def add(self, agency: str | None, route: str | None = None,
            stops: Iterable[str | None] = ()) -> None:
        if not agency:
            return
        self.routes.setdefault(agency, set())
        self.stops.setdefault(agency, set())
        if route:
            self.routes[agency].add(route)
        self.stops[agency].update(s for s in stops if s)

    @property
    def agencies(self) -> list[str]:
        return sorted(self.routes)


def _matches(row, agency: str, scope: TripScope) -> str | None:
    """Why this alert affects the trip ("Blue Line", "route 401", "stop 1792"), or
    None if it doesn't."""
    lines = set(json.loads(row["affected_lines"] or "[]"))
    stations = set(json.loads(row["affected_stations"] or "[]"))
    routes = set(json.loads(row["affected_routes"] or "[]"))
    stops = set(json.loads(row["affected_stops"] or "[]"))
    my_routes, my_stops = scope.routes.get(agency, set()), scope.stops.get(agency, set())
    hit_routes = (lines | routes) & my_routes
    hit_stops = (stations | stops) & my_stops
    if hit_routes:
        return ", ".join(sorted(hit_routes))
    if hit_stops:
        return "stop/station " + ", ".join(sorted(hit_stops))
    if row["mode"] != "elevator_escalator" and not (lines or stations or routes or stops):
        # Tagged system-wide, but some feeds do that for single-route notices
        # ("Route 321LOOP - Missed Trip"): skip those unless they name this trip's route.
        named = _ROUTE_IN_TEXT.findall(row["headline"] or "")
        if named and not any(n.upper().startswith(tuple(r.upper() for r in my_routes))
                             for n in named):
            return None
        return "the whole system"
    return None


def for_trip(db: Database, scope: TripScope) -> dict:
    """``{"active_disruptions": [...], "service_change_notices": [...]}`` for a trip."""
    alerts: list[dict] = []
    for agency in scope.agencies:
        feed, mode = _ALERT_FEED.get(agency, (agency, None))
        sql = "SELECT * FROM alerts WHERE resolved_at IS NULL AND feed_id = ?"
        params: list = [feed]
        if mode:
            sql += " AND mode = ?"
            params.append(mode)
        elif agency == "wmata":
            sql += " AND mode IN ('rail', 'elevator_escalator')"
        for row in db.conn.execute(sql + " ORDER BY updated_at DESC", params):
            why = _matches(row, agency, scope)
            if not why:
                continue
            desc = row["description"] or ""
            alerts.append({
                "agency": agency,
                "kind": ("elevator/escalator outage" if row["mode"] == "elevator_escalator"
                         else "service alert"),
                "headline": row["headline"],
                "details": desc[:400] + (" ..." if len(desc) > 400 else ""),
                "affects": why,
                "url": row["url"],
                "updated_at": row["updated_at"] or row["seen_at"],
                "source": ("WMATA real-time incidents (WMATA API)" if feed == "wmata"
                           else f"{agency} GTFS-realtime service alerts"),
            })

    notices: list[dict] = []
    for agency in scope.agencies:
        feed = _ALERT_FEED.get(agency, (agency, None))[0]
        for row in db.conn.execute(
            "SELECT title, url, summary, retrieved_at FROM documents "
            "WHERE feed_id = ? AND category = 'alerts' ORDER BY retrieved_at DESC LIMIT ?",
            (feed, _MAX_NOTICES_PER_AGENCY),
        ):
            notices.append({
                "agency": agency, "title": row["title"], "url": row["url"],
                "excerpt": (row["summary"] or "")[:300],
                "retrieved_at": row["retrieved_at"],
            })
    return {
        "active_disruptions": alerts,
        "service_change_notices": notices,
        "disruption_note": (
            "Routes and times above come from the scheduled timetable (GTFS). "
            + ("Tell the rider about each active disruption — it may change this trip. "
               if alerts else "No live alert matches this trip. ")
            + "Service-change notices are the agencies' own pages; mention any that "
            "apply to the rider's travel date."
        ),
    }
