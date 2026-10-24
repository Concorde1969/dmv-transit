"""The contract every ingest source plugin implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from transit_mcp.agencies.models import AgencyConfig, SourceConfig
    from transit_mcp.config import Settings
    from transit_mcp.db.database import Database
    from transit_mcp.ingest.http import HttpClient

# Which `transit-mcp ingest <scope>` runs a source. Sources run api -> gtfs -> web.
Scope = Literal["api", "gtfs", "web"]


@dataclass
class RunOptions:
    """Knobs from the command line that any source may honour."""

    force: bool = False
    max_pages: int | None = None
    load_stop_times: bool = True
    include_fares: bool = True
    # Rebuild the cross-agency transfer graph afterwards if stops may have changed.
    # Frequent alerts-only refreshes turn this off; stations don't move every 15 min.
    rebuild_links: bool = True


@dataclass
class IngestContext:
    db: Database
    settings: Settings
    web_http: HttpClient  # polite crawler client (crawl delay, disk cache)
    api_http: HttpClient  # API/feed client (rate-limited, no text cache)
    options: RunOptions = field(default_factory=RunOptions)


@runtime_checkable
class Source(Protocol):
    """An ingest plugin. Register it with ``@register_source``.

    Attributes:
        type: the ``type = "..."`` value used in agency TOML files.
        scope: which ingest scope runs it (``api`` | ``gtfs`` | ``web``).
        updates_network: True if it writes stops/stations, so the cross-agency
            transfer graph must be rebuilt afterwards.
    """

    type: str
    scope: Scope
    updates_network: bool

    def run(
        self, ctx: IngestContext, agency: AgencyConfig, source: SourceConfig
    ) -> dict[str, Any]:
        """Fetch, normalize and store this agency's data. Return a stats dict.

        Raise to signal failure; the pipeline records it and moves on to the next
        source, so one broken feed never stops the rest of the run.
        """
        ...
