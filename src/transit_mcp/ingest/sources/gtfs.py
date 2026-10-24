"""``type = "gtfs"`` — load an agency's GTFS static feed (its routes, stops and
schedules: the primary source for every routing and timing answer).

TOML options::

    [[sources]]
    type = "gtfs"
    transitland_id = "f-dqc-virginiarailwayexpress"   # preferred: found via Transitland
    url = "https://agency.example/google_transit.zip"  # fallback if Transitland fails

    # agencies not listed in Transitland:
    [[sources]]
    type = "gtfs"
    url = "https://agency.example/google_transit.zip"  # or `manual = true`

    # optional
    transitland_atlas = "prtctransit.org.dmfr.json"  # atlas file, if not the url's domain
    auth_env = "WMATA_API_KEY"     # secret sent where the Transitland record asks for one
    index_stops = false            # stops not used for place search / walking links
    metrorail_line_order = true    # also derive Metrorail station order (WMATA only)

Download order: Transitland's copy → the URL Transitland lists → ``url`` → a manually
saved ``feeds/<slug>/google_transit.zip`` (see ``ingest/transitland.py``). After
loading, the feed is summarized (route/stop service windows), turned into searchable
documents, and archived raw to ``data/gtfs/<slug>.zip`` for scheduling engines.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ConfigDict, model_validator

from transit_mcp.agencies.models import AgencyConfig, SourceConfig
from transit_mcp.ingest.gtfs.loader import GtfsFeed, load_feed
from transit_mcp.ingest.gtfs.summarize import summarize_feed, synthesize_docs
from transit_mcp.ingest.sources import register_source
from transit_mcp.ingest.sources.base import IngestContext
from transit_mcp.ingest.transitland import fetch_gtfs


class GtfsOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    manual: bool = False
    transitland_id: str | None = None
    transitland_atlas: str | None = None
    auth_env: str | None = None
    index_stops: bool = True
    metrorail_line_order: bool = False

    @model_validator(mode="after")
    def _has_a_source(self) -> GtfsOptions:
        if not (self.url or self.manual or self.transitland_id):
            raise ValueError(
                "a gtfs source needs `transitland_id`, `url`, or `manual = true`"
            )
        return self


def _metrorail_line_order(ctx: IngestContext, slug: str) -> dict:
    """Station order along each Metrorail line, from the rail GTFS just loaded."""
    from transit_mcp.ingest.sources.wmata_api import store_rail_line_order
    from transit_mcp.ingest.wmata_api import rail_line_order_from_gtfs

    archived = ctx.settings.gtfs_archive_dir / f"{slug}.zip"
    station_lines = {
        r["code"]: set(json.loads(r["line_codes"] or "[]"))
        for r in ctx.db.conn.execute("SELECT code, line_codes FROM stations")
    }
    if not archived.is_file() or not station_lines:
        return {"skipped": "needs the archived rail GTFS and the WMATA stations list"}
    return store_rail_line_order(
        ctx.db, rail_line_order_from_gtfs(archived.read_bytes(), station_lines)
    )


@register_source
class GtfsSource:
    type = "gtfs"
    scope = "gtfs"
    updates_network = True

    def run(self, ctx: IngestContext, agency: AgencyConfig, source: SourceConfig) -> dict:
        opts = GtfsOptions.model_validate(source.options())
        feed = GtfsFeed(agency.slug, agency.name, agency.mode, opts.url)
        notes: list[str] = []

        def fetcher():
            result, skipped = fetch_gtfs(
                slug=agency.slug, transitland_id=opts.transitland_id,
                agency_url=opts.url, auth_env=opts.auth_env,
                atlas_file=opts.transitland_atlas,
                settings=ctx.settings, http=ctx.api_http,
            )
            notes.extend(skipped)
            return result

        result = load_feed(
            ctx.db,
            ctx.api_http,
            feed,
            feeds_dir=ctx.settings.feeds_dir,
            load_stop_times=ctx.options.load_stop_times,
            archive_dir=ctx.settings.gtfs_archive_dir,
            fetcher=fetcher,
            index_stops=opts.index_stops,
        )
        if notes:
            result["notes"] = notes
        if result.get("status") in ("loaded", "unchanged"):
            result["summary"] = summarize_feed(ctx.db, agency.slug)
            result["documents"] = synthesize_docs(ctx.db, agency.slug)
            if opts.metrorail_line_order:
                try:
                    result["rail_line_order"] = _metrorail_line_order(ctx, agency.slug)
                except Exception as exc:  # noqa: BLE001
                    result["rail_line_order"] = {"error": str(exc)}
        elif result.get("status") == "pending":
            # Not an error: e.g. a manual feed simply hasn't been dropped in yet.
            result["hint"] = (
                f"Save the agency's GTFS zip as "
                f"{ctx.settings.feeds_dir / agency.slug / 'google_transit.zip'}"
            )
        return result
