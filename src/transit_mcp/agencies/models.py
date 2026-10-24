"""Typed schema for an agency definition file (``agencies/<slug>.toml``).

One file fully describes one transit agency: who it is (profile), how riders pay
(fares), and where its data comes from (sources). Adding an agency never requires
code changes unless it needs a brand-new *kind* of data source.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

FrequencyTier = Literal["frequent", "peak", "infrequent", "intercity"]

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ProfileConfig(BaseModel):
    """What the system is for — drives the connection advisor's reasoning."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = None
    role: str = ""
    strengths: str = ""
    weaknesses: str = ""
    service_area: str = ""
    connects_to: list[str] = Field(default_factory=list)
    frequency_tier: FrequencyTier = "infrequent"
    park_and_ride: bool = False


class FareConfig(BaseModel):
    """How a rider actually pays — the fare-vending barrier."""

    model_config = ConfigDict(extra="forbid")

    base_fare: float | None = None
    reduced_fare: float | None = None
    currency: str = "USD"
    payment_methods: list[str] = Field(default_factory=list)
    where_to_buy: list[str] = Field(default_factory=list)
    mobile_app_name: str | None = None
    app_ios_url: str | None = None
    app_android_url: str | None = None
    accepts_smartrip: bool = False
    accepts_contactless: bool = False
    cash_onboard: bool = False
    exact_change_required: bool = False
    transfer_policy: str = ""
    fare_page_url: str | None = None
    notes: str = ""


class SourceConfig(BaseModel):
    """One data source for an agency. ``type`` selects the ingest plugin; every
    other key is passed through to that plugin, which validates its own options."""

    model_config = ConfigDict(extra="allow")

    type: str
    enabled: bool = True

    def options(self) -> dict:
        return {k: v for k, v in self.model_dump().items() if k not in ("type", "enabled")}


class AgencyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str
    mode: str = "bus"  # free text: bus | rail+bus | commuter_rail | intercity_bus | ...
    enabled: bool = True
    profile: ProfileConfig = Field(default_factory=ProfileConfig)
    fares: FareConfig = Field(default_factory=FareConfig)
    sources: list[SourceConfig] = Field(default_factory=list)
    # Names riders use for a stop/station that its official name doesn't match,
    # e.g. {"DCA" = "C10"}. Values are GTFS stop_ids (or WMATA station codes).
    aliases: dict[str, str] = Field(default_factory=dict)

    # Set by the registry: where this definition was loaded from.
    origin: str | None = Field(default=None, exclude=True)

    @field_validator("slug")
    @classmethod
    def _slug_ok(cls, v: str) -> str:
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug must be lowercase letters, digits and dashes (e.g. 'fairfax-connector')"
            )
        return v

    @property
    def display_name(self) -> str:
        return self.profile.display_name or self.name

    def sources_of(self, source_type: str) -> list[SourceConfig]:
        return [s for s in self.sources if s.type == source_type and s.enabled]
