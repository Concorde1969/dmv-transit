"""Normalized records produced by ingestors and written to the database.

These are storage-agnostic: ingestors build them from raw API JSON or scraped
HTML, and ``db.database`` upserts them into SQLite.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

SourceType = Literal["api", "web"]

Category = Literal[
    "alerts",
    "fares",
    "payment",
    "accessibility",
    "safety",
    "rules",
    "maps",
    "new_rider",
    "visitor",
    "station",
    "line",
    "policy",
    "agency",
    "route",
    "connections",
]

AlertMode = Literal["rail", "bus", "elevator_escalator"]
UnitType = Literal["elevator", "escalator"]


class FetchResult(BaseModel):
    """Outcome of a single HTTP fetch, before normalization."""

    url: str
    source_type: SourceType
    category: Category
    status_code: int
    content_hash: str
    etag: str | None = None
    last_modified: str | None = None
    # Decoded body: text for web pages, canonical JSON string for API responses.
    body: str
    from_cache: bool = False
    not_modified: bool = False


class Document(BaseModel):
    """A unit of narrative content (a scraped page, or a synthesized summary)."""

    url: str
    category: Category
    title: str
    summary: str | None = None
    body_markdown: str
    feed_id: str | None = None  # agency slug; None = WMATA / region-wide
    effective_date: str | None = None
    expires_at: str | None = None
    raw_hash: str
    retrieved_at: datetime


class Chunk(BaseModel):
    """A heading-scoped slice of a Document, used for full-text search."""

    chunk_index: int
    heading_path: str
    text: str

    @property
    def char_len(self) -> int:
        return len(self.text)


class DocumentWithChunks(BaseModel):
    document: Document
    chunks: list[Chunk] = Field(default_factory=list)


class Alert(BaseModel):
    """A real-time service disruption from the WMATA API."""

    provider_alert_id: str
    mode: AlertMode
    headline: str
    description: str | None = None
    affected_lines: list[str] = Field(default_factory=list)
    affected_stations: list[str] = Field(default_factory=list)
    severity: str | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    updated_at: datetime | None = None
    raw_json: str


class Station(BaseModel):
    code: str
    name: str
    lat: float | None = None
    lon: float | None = None
    line_codes: list[str] = Field(default_factory=list)
    address: dict[str, str] = Field(default_factory=dict)
    has_parking: bool = False
    together_codes: list[str] = Field(default_factory=list)
    raw_json: str


class Line(BaseModel):
    code: str
    display_name: str
    color_hex: str | None = None
    start_station_code: str | None = None
    end_station_code: str | None = None
    raw_json: str


class UnitStatus(BaseModel):
    """Elevator / escalator status for a single unit."""

    station_code: str
    unit_type: UnitType
    unit_name: str
    location_description: str | None = None
    symptom: str | None = None
    out_of_service: bool = True
    date_out_of_service: datetime | None = None
    estimated_return: str | None = None
    updated_at: datetime | None = None
    raw_json: str


class Fare(BaseModel):
    """Station-to-station rail fare."""

    origin_code: str
    dest_code: str
    peak_fare: float | None = None
    offpeak_fare: float | None = None
    senior_disabled_fare: float | None = None
    rail_time_min: int | None = None
    raw_json: str
