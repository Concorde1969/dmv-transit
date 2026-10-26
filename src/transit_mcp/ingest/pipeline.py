"""Generic ingest pipeline: every enabled agency × every source it declares.

Nothing in here knows about specific agencies. Agencies come from TOML files
(:mod:`transit_mcp.agencies`); what each ``[[sources]]`` entry does comes from the
plugin registered for its ``type`` (:mod:`transit_mcp.ingest.sources`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from transit_mcp.agencies import AgencyConfig, load_agencies, seed_curated_tables
from transit_mcp.config import Settings, get_settings
from transit_mcp.db.database import Database
from transit_mcp.ingest.gtfs.clusters import build_clusters
from transit_mcp.ingest.gtfs.links import build_stop_links
from transit_mcp.ingest.http import HttpClient
from transit_mcp.ingest.sources import IngestContext, RunOptions, get_source

IngestScope = Literal["all", "api", "gtfs", "web"]
# Stations/stops must exist before web docs reference them and before the
# transfer graph is rebuilt, so sources run in this order.
_SCOPE_ORDER = {"api": 0, "gtfs": 1, "web": 2}


def web_client(settings: Settings) -> HttpClient:
    return HttpClient(
        user_agent=settings.http_user_agent,
        timeout=settings.http_timeout_seconds,
        min_interval_seconds=settings.crawl_delay_seconds,
        cache_dir=settings.cache_dir,
    )


def api_client(settings: Settings) -> HttpClient:
    return HttpClient(
        user_agent=settings.http_user_agent,
        timeout=settings.http_timeout_seconds,
        min_interval_seconds=1.0 / max(settings.api_requests_per_second, 0.1),
        cache_dir=None,
    )


def run_ingest(
    settings: Settings | None = None,
    *,
    scope: IngestScope = "all",
    only: Iterable[str] | None = None,
    options: RunOptions | None = None,
    agencies: list[AgencyConfig] | None = None,
) -> dict:
    """Run ingestion and return ``{scope, agencies: {slug: {type: stats}}, post,
    notices, errors}``. One failing source never stops the others."""
    settings = settings or get_settings()
    options = options or RunOptions(load_stop_times=settings.gtfs_load_stop_times)
    agencies = agencies if agencies is not None else load_agencies(settings)
    only_set = {s.strip() for s in only or [] if s.strip()}

    result: dict = {"scope": scope, "agencies": {}, "post": {}, "notices": [], "errors": []}
    known = {a.slug for a in agencies}
    for slug in sorted(only_set - known):
        result["errors"].append(f"{slug}: no agency with that slug is defined")

    work = []
    for agency in agencies:
        if only_set and agency.slug not in only_set:
            continue
        for src in agency.sources:
            if not src.enabled:
                continue
            plugin = get_source(src.type)
            if plugin is None:
                result["errors"].append(
                    f"{agency.slug}: unknown source type '{src.type}' "
                    "(is its plugin installed?)"
                )
                continue
            if scope != "all" and plugin.scope != scope:
                continue
            work.append((_SCOPE_ORDER.get(plugin.scope, 9), agency, src, plugin))
    work.sort(key=lambda w: w[0])

    db = Database(settings.db_path)
    run_id = db.start_run(scope)
    try:
        seed_curated_tables(db, agencies)
        network_changed = False
        with web_client(settings) as web_http, api_client(settings) as api_http:
            ctx = IngestContext(db, settings, web_http, api_http, options)
            for _, agency, src, plugin in work:
                bucket = result["agencies"].setdefault(agency.slug, {})
                key = src.type if src.type not in bucket else f"{src.type}#{len(bucket) + 1}"
                try:
                    stats = plugin.run(ctx, agency, src)
                    bucket[key] = stats
                    if plugin.updates_network:
                        network_changed = True
                    if isinstance(stats, dict):
                        if stats.get("status") == "pending":
                            result["notices"].append(
                                f"{agency.slug}: {stats.get('hint') or stats.get('error')}"
                            )
                        elif stats.get("errors"):
                            result["errors"].append(
                                f"{agency.slug}/{src.type}: {len(stats['errors'])} "
                                "partial failure(s)"
                            )
                except Exception as exc:  # noqa: BLE001 - isolate per source
                    db.conn.rollback()
                    bucket[key] = {"status": "error", "error": str(exc)}
                    result["errors"].append(f"{agency.slug}/{src.type}: {exc}")

        if network_changed and options.rebuild_links:
            result["post"]["links"] = build_stop_links(
                db,
                radius_m=settings.transfer_link_radius_m,
                walk_speed_m_per_min=settings.walk_speed_m_per_min,
            )
            result["post"]["clusters"] = build_clusters(db)

        status = "partial" if result["errors"] else "ok"
        result["errors"] = result["errors"] or None
        result["notices"] = result["notices"] or None
        db.finish_run(run_id, status=status, stats=result)
        return result
    except Exception as exc:  # noqa: BLE001
        db.finish_run(run_id, status="error", stats=result, error=str(exc))
        raise
    finally:
        db.close()
