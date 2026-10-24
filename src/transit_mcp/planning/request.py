"""Turn a rider's free-text origin/destination into a :class:`TripRequest`.

Geocoding and nearby-stop lookup happen here, once, so every engine receives
already-resolved places and never needs its own place-name matching.
"""

from __future__ import annotations

from pathlib import Path

from transit_mcp.config import Settings, get_settings
from transit_mcp.db.database import Database
from transit_mcp.planning.models import (
    FeedRef,
    NearbyStop,
    Place,
    PlannerContext,
    TripRequest,
)
from transit_mcp.retrieval.regional import find_stops_near, resolve_point

ENDPOINT_RADIUS_M = 900  # ~11 minutes on foot


class UnresolvedPlace(LookupError):
    def __init__(self, origin_label: str | None, destination_label: str | None):
        super().__init__("could not resolve origin and/or destination")
        self.origin_label = origin_label
        self.destination_label = destination_label


def _place(db: Database, query: str) -> Place | None:
    pt = resolve_point(db, query)
    if not pt:
        return None
    near = find_stops_near(db, f"{pt['lat']},{pt['lon']}", radius_m=ENDPOINT_RADIUS_M)
    return Place(
        query=query,
        label=pt["label"],
        lat=pt["lat"],
        lon=pt["lon"],
        kind=pt["kind"],
        agency=pt.get("feed"),
        stop_id=pt.get("id"),
        nearby_stops=[
            NearbyStop(
                agency=s["agency"], stop_id=s["stop_id"], name=s["name"],
                kind=s["kind"], lat=s.get("lat"), lon=s.get("lon"),
                distance_m=s["distance_m"], walk_min=s["walk_min"],
                routes=[str(r) for r in s.get("routes") or []],
            )
            for s in near.get("stops") or []
        ],
    )


def planner_context(db: Database, settings: Settings) -> PlannerContext:
    archive = Path(settings.gtfs_archive_dir).resolve()
    feeds = []
    for r in db.conn.execute(
        "SELECT feed_id, agency_name, mode, fetched_at, feed_start_date, feed_end_date "
        "FROM gtfs_feeds WHERE status = 'loaded' ORDER BY feed_id"
    ):
        zip_path = archive / f"{r['feed_id']}.zip"
        feeds.append(FeedRef(
            agency=r["feed_id"], name=r["agency_name"], mode=r["mode"],
            path=str(zip_path) if zip_path.is_file() else None,
            fetched_at=r["fetched_at"], valid_from=r["feed_start_date"],
            valid_until=r["feed_end_date"],
        ))
    manifest = archive / "manifest.json"
    return PlannerContext(
        gtfs_dir=str(archive),
        manifest=str(manifest) if manifest.is_file() else None,
        feeds=feeds,
        walk_speed_m_per_min=settings.walk_speed_m_per_min,
        transfer_radius_m=settings.transfer_link_radius_m,
    )


def build_request(
    db: Database,
    origin: str,
    destination: str,
    *,
    day: str | None = None,
    date: str | None = None,
    depart_after: str | None = None,
    arrive_by: str | None = None,
    rider_category: str | None = None,
    max_options: int = 3,
    settings: Settings | None = None,
) -> TripRequest:
    settings = settings or get_settings()
    o, d = _place(db, origin), _place(db, destination)
    if o is None or d is None:
        raise UnresolvedPlace(o.label if o else None, d.label if d else None)
    return TripRequest(
        origin=o, destination=d, day=day, date=date, depart_after=depart_after,
        arrive_by=arrive_by, rider_category=rider_category or "regular",
        max_options=max_options, context=planner_context(db, settings),
    )
