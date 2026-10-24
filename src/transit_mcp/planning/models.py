"""The trip-planning contract shared by every scheduling engine.

This is the boundary between the assistant and a planner. Python engines receive
and return these objects directly; out-of-process engines (e.g. Node.js) exchange
the same shapes as JSON. ``transit-mcp planner schema`` writes JSON Schemas for
both sides.

Versioning: additive changes (new optional fields) keep ``CONTRACT_VERSION``;
anything that would break an existing engine bumps it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "1"


class NearbyStop(BaseModel):
    """A boarding point within walking distance of a place."""

    agency: str = Field(description="Agency slug, e.g. 'dash' or 'wmata'.")
    stop_id: str = Field(description="GTFS stop_id (or WMATA station code).")
    name: str
    kind: Literal["gtfs", "wmata"] = Field(
        description="'gtfs' = a stop in that agency's GTFS feed; 'wmata' = a Metrorail "
        "station code (WMATA data comes from its API, not the GTFS archive)."
    )
    lat: float | None = None
    lon: float | None = None
    distance_m: int
    walk_min: int
    routes: list[str] = Field(default_factory=list, description="route_ids / line codes")


class Place(BaseModel):
    """An origin or destination, already geocoded by the assistant."""

    query: str = Field(description="What the rider typed.")
    label: str = Field(description="What it resolved to, e.g. 'Ballston-MU station'.")
    lat: float
    lon: float
    kind: Literal["wmata", "gtfs", "point"]
    agency: str | None = None
    stop_id: str | None = None
    nearby_stops: list[NearbyStop] = Field(
        default_factory=list, description="Stops within ~900 m, nearest first."
    )


class FeedRef(BaseModel):
    """One archived GTFS feed the engine may read."""

    agency: str
    name: str | None = None
    mode: str | None = None
    path: str | None = Field(None, description="Absolute path to the GTFS .zip.")
    fetched_at: str | None = None
    valid_from: str | None = None
    valid_until: str | None = None


class PlannerContext(BaseModel):
    gtfs_dir: str = Field(description="Absolute path of the GTFS archive folder.")
    manifest: str | None = Field(None, description="Absolute path to manifest.json.")
    feeds: list[FeedRef] = Field(default_factory=list)
    walk_speed_m_per_min: float = 80.0
    transfer_radius_m: int = 250


class TripRequest(BaseModel):
    contract_version: str = CONTRACT_VERSION
    origin: Place
    destination: Place
    day: str | None = Field(
        None, description="monday..sunday, 'weekday', 'weekend', or null for any day."
    )
    date: str | None = Field(None, description="YYYY-MM-DD if the rider gave one.")
    depart_after: str | None = Field(None, description="HH:MM, local time.")
    arrive_by: str | None = Field(None, description="HH:MM, local time.")
    rider_category: str = Field("regular", description="regular | senior | disabled | ...")
    max_options: int = 3
    context: PlannerContext


class Leg(BaseModel):
    model_config = ConfigDict(extra="allow")  # engines may attach extra detail

    mode: Literal["ride", "walk"] = "ride"
    agency: str | None = None
    system: str | None = Field(None, description="Display name, e.g. 'DASH'.")
    route: str | None = None
    route_id: str | None = Field(
        None, description="GTFS route_id (Metrorail: line code) — used to match alerts.")
    headsign: str | None = None
    board: str | None = None
    alight: str | None = None
    board_stop_id: str | None = None
    alight_stop_id: str | None = None
    depart_time: str | None = Field(None, description="HH:MM when known.")
    arrive_time: str | None = None
    duration_min: int | None = None
    role: str | None = Field(None, description="Why this system is used for this leg.")
    fare: float | None = None
    pay_with: str | None = None
    fare_page: str | None = None
    note: str | None = None


class TripOption(BaseModel):
    model_config = ConfigDict(extra="allow")

    summary: str
    legs: list[Leg] = Field(default_factory=list)
    transfers: int | None = None
    feasible: bool | None = Field(
        None, description="Does this work on the requested day/time?"
    )
    feasibility: str | None = Field(
        None, description="Plain-language reason, e.g. 'works Mon–Fri'."
    )
    depart_time: str | None = None
    arrive_time: str | None = None
    duration_min: int | None = None
    walk_min_total: int | None = None
    total_fare: float | None = None
    total_cost_estimate: str | None = None


class TripPlan(BaseModel):
    contract_version: str = CONTRACT_VERSION
    engine: str = "external"
    origin: str
    destination: str
    day: str | None = None
    options: list[TripOption] = Field(default_factory=list)
    caveat: str = (
        "Guidance only — confirm departure times and fares with each agency before "
        "travelling."
    )
    warnings: list[str] = Field(default_factory=list)
    data_stale: bool | None = None
