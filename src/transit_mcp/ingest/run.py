"""Convenience entry points over :func:`transit_mcp.ingest.pipeline.run_ingest`.

``web`` / ``api`` / ``gtfs`` select sources by their plugin's scope across *all*
agencies; ``only`` narrows to specific agency slugs.
"""

from __future__ import annotations

from collections.abc import Iterable

from transit_mcp.config import Settings, get_settings
from transit_mcp.ingest.pipeline import run_ingest
from transit_mcp.ingest.sources import RunOptions


def _opts(settings: Settings, **kw) -> RunOptions:
    lst = kw.get("load_stop_times")
    return RunOptions(
        force=kw.get("force", False),
        max_pages=kw.get("max_pages"),
        load_stop_times=settings.gtfs_load_stop_times if lst is None else lst,
        include_fares=kw.get("include_fares", True),
    )


def run_web(
    settings: Settings | None = None, *, max_pages: int | None = None,
    force: bool = False, only: Iterable[str] | None = None,
) -> dict:
    settings = settings or get_settings()
    return run_ingest(settings, scope="web", only=only,
                      options=_opts(settings, max_pages=max_pages, force=force))


def run_api(
    settings: Settings | None = None, *, include_fares: bool = True,
    only: Iterable[str] | None = None,
) -> dict:
    settings = settings or get_settings()
    return run_ingest(settings, scope="api", only=only,
                      options=_opts(settings, include_fares=include_fares))


def run_gtfs(
    settings: Settings | None = None, *, only: Iterable[str] | None = None,
    load_stop_times: bool | None = None,
) -> dict:
    settings = settings or get_settings()
    return run_ingest(settings, scope="gtfs", only=only,
                      options=_opts(settings, load_stop_times=load_stop_times))


def run_all(settings: Settings | None = None, **kwargs) -> dict:
    settings = settings or get_settings()
    return run_ingest(settings, scope="all", only=kwargs.get("only"),
                      options=_opts(settings, **kwargs))
