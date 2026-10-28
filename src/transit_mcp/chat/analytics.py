"""What a map search and its results looked like, for the chat log's ``trip_searches``.

Privacy rules (the site's promise): start and end points are rounded to ~100 m by the
chat log; the only place names kept are landmarks, stations and areas picked from the
address suggestions (``chatlog.place_name``) — never typed text or street addresses.
Everything else here is about the transit network: nearest stops, the options shown.
"""

from __future__ import annotations

import json
import time

from transit_mcp.chatlog import place_name
from transit_mcp.ingest.gtfs.clusters import haversine_m


def nearest_stops(point, stops, agencies: dict[str, dict]) -> dict:
    """The nearest stop and the nearest rail station (any agency) to one end."""
    def meters(s) -> int:
        return int(haversine_m(point.lat, point.lon, s.lat, s.lon))

    out: dict = {}
    by_distance = sorted(stops, key=meters)
    if by_distance:
        s = by_distance[0]
        out.update(stop=s.name, stop_agency=s.feed, stop_m=meters(s))
    rail = [s for s in by_distance if "rail" in (agencies.get(s.feed, {}).get("mode") or "")]
    if rail:
        out.update(station=rail[0].name, station_m=meters(rail[0]))
    return out


def _route(leg: dict) -> str:
    short, long_ = leg.get("route_short_name"), leg.get("route_long_name")
    # Metro's one- or two-letter codes ("R") read better as names ("Red").
    name = (long_ if long_ and (not short or len(short) <= 2) else short) or leg.get("route_id")
    return f"{leg.get('agency_name') or leg.get('agency')} {name}"


def _option_summary(opt: dict, now: float) -> dict:
    rides = [leg for leg in opt.get("legs", []) if leg.get("mode") == "ride"]
    waits = [leg.get("wait_s") or 0 for leg in opt.get("legs", []) if leg.get("transfer")]
    return {
        "routes": [_route(r) for r in rides],
        "agencies": [r.get("agency") for r in rides],
        "minutes": round((opt.get("duration_s") or 0) / 60),
        "leave_in_min": round(((opt.get("leave_at") or now) - now) / 60),
        "walk_min": round((opt.get("walk_s") or 0) / 60),
        "transfer_wait_min": [round(w / 60) for w in waits],
        "transfers": opt.get("transfers"),
        "live": bool(opt.get("live")),
        "fare": opt.get("fare_estimate"),
        "reports": len(opt.get("reports") or []),
    }


def search_row(*, visitor_id: str, device: str, search_id: str, origin, destination,
               result: dict | None, ends: dict | None, agencies: dict[str, dict],
               latency_ms: int | None, outcome: str) -> dict:
    """One ``trip_searches`` row. ``origin``/``destination`` are the request's places
    (lat, lon, label, source, kind); ``ends`` holds the planner's nearby stops."""
    now = time.time()
    row = {
        "search_id": search_id, "visitor_id": visitor_id, "device": device,
        "outcome": outcome, "latency_ms": latency_ms,
        "origin_lat": origin.lat, "origin_lon": origin.lon,
        "dest_lat": destination.lat, "dest_lon": destination.lon,
        "distance_km": round(haversine_m(origin.lat, origin.lon,
                                         destination.lat, destination.lon) / 1000, 2),
        "origin_source": origin.source, "dest_source": destination.source,
        "origin_kind": origin.kind, "dest_kind": destination.kind,
        "origin_place": place_name(origin.label, origin.kind, origin.source),
        "dest_place": place_name(destination.label, destination.kind, destination.source),
    }
    for side, key in (("origin", "origin"), ("dest", "destination")):
        for k, v in ((ends or {}).get(key) or {}).items():
            row[f"{side}_{k}"] = v
    if result is None:
        return row
    options = result.get("options") or []
    summaries = [_option_summary(o, now) for o in options]
    row.update(n_options=len(options), live_checks=result.get("live_checks", 0),
               notice=result.get("notice"), window_min=result.get("window_min"),
               n_live=sum(s["live"] for s in summaries),
               n_with_reports=sum(1 for s in summaries if s["reports"]),
               agencies=json.dumps(sorted({a for s in summaries for a in s["agencies"] if a})),
               options_json=json.dumps(summaries, ensure_ascii=False))
    if summaries:
        best = summaries[0]
        row.update(best_minutes=best["minutes"], best_leave_in_min=best["leave_in_min"],
                   best_walk_min=best["walk_min"], best_transfers=best["transfers"],
                   best_wait_min=max(best["transfer_wait_min"], default=0),
                   best_routes=" > ".join(best["routes"]))
    return row
