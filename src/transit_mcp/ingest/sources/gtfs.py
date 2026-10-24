"""``type = "gtfs"`` — load an agency's GTFS static feed.

TOML options::

    [[sources]]
    type = "gtfs"
    url = "https://agency.example/google_transit.zip"

    # or, when the operator blocks scripted downloads:
    [[sources]]
    type = "gtfs"
    manual = true      # expects feeds/<slug>/google_transit.zip

A local ``feeds/<slug>/google_transit.zip`` always takes precedence over ``url``.
After loading, the feed is summarized (route/stop service windows), turned into
searchable documents, and archived raw to ``data/gtfs/<slug>.zip`` for scheduling
engines.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, model_validator

from transit_mcp.agencies.models import AgencyConfig, SourceConfig
from transit_mcp.ingest.gtfs.loader import GtfsFeed, load_feed
from transit_mcp.ingest.gtfs.summarize import summarize_feed, synthesize_docs
from transit_mcp.ingest.sources import register_source
from transit_mcp.ingest.sources.base import IngestContext


class GtfsOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    manual: bool = False

    @model_validator(mode="after")
    def _url_or_manual(self) -> GtfsOptions:
        if not self.url and not self.manual:
            raise ValueError("a gtfs source needs either `url = \"...\"` or `manual = true`")
        return self


@register_source
class GtfsSource:
    type = "gtfs"
    scope = "gtfs"
    updates_network = True

    def run(self, ctx: IngestContext, agency: AgencyConfig, source: SourceConfig) -> dict:
        opts = GtfsOptions.model_validate(source.options())
        feed = GtfsFeed(agency.slug, agency.name, agency.mode, opts.url)
        result = load_feed(
            ctx.db,
            ctx.api_http,
            feed,
            feeds_dir=ctx.settings.feeds_dir,
            load_stop_times=ctx.options.load_stop_times,
            archive_dir=ctx.settings.gtfs_archive_dir,
        )
        if result.get("status") in ("loaded", "unchanged"):
            result["summary"] = summarize_feed(ctx.db, agency.slug)
            result["documents"] = synthesize_docs(ctx.db, agency.slug)
        elif result.get("status") == "pending":
            # Not an error: a manual feed simply hasn't been dropped in yet.
            result["hint"] = (
                f"Save the agency's GTFS zip as "
                f"{ctx.settings.feeds_dir / agency.slug / 'google_transit.zip'}"
            )
        return result
