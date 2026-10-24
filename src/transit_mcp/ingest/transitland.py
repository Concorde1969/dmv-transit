"""Find and download an agency's GTFS feed through Transitland.

Transitland (https://www.transit.land) is the registry of record for which GTFS feed
an agency publishes and where it lives. For each feed we look up its Transitland
record, then fetch the GTFS zip in this order:

1. Transitland's own hosted copy of the latest version — needs ``TRANSITLAND_API_KEY``
   and is only served when the feed's license allows redistribution;
2. the official download URL that Transitland lists for the feed, with whatever
   authorization the record says it needs (e.g. WMATA's ``api_key`` header);
3. the URL in the agency's TOML, then a manually saved ``feeds/<slug>/`` zip.

Records come from the Transitland REST API when a key is set, otherwise from the
public ``transitland-atlas`` repository (the same data, published on GitHub).
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from transit_mcp.config import Settings
from transit_mcp.ingest.http import HttpClient

REST_BASE = "https://transit.land/api/v2/rest"
ATLAS_RAW = "https://raw.githubusercontent.com/transitland/transitland-atlas/master/feeds"
ATLAS_MAX_AGE_S = 24 * 3600


@dataclass
class FeedRecord:
    onestop_id: str
    static_current: str | None = None
    realtime_alerts: str | None = None
    auth_type: str | None = None        # e.g. "header"
    auth_param: str | None = None       # e.g. "api_key"
    license: dict = field(default_factory=dict)


@dataclass
class FetchResult:
    content: bytes
    via: str               # transitland | transitland-listed-url | agency-url | local file
    url: str | None
    transitland_id: str | None


def _secret(settings: Settings, env_name: str | None) -> str | None:
    if not env_name:
        return None
    attr = env_name.lower()
    return getattr(settings, attr, None) or os.environ.get(env_name) or None


def atlas_file_for(url: str | None) -> str | None:
    """Default transitland-atlas file name for a feed URL: its registrable domain
    (``api.wmata.com`` -> ``wmata.com.dmfr.json``)."""
    host = urlparse(url or "").hostname or ""
    parts = host.split(".")
    return f"{'.'.join(parts[-2:])}.dmfr.json" if len(parts) >= 2 else None


def _parse_feed(feed: dict) -> FeedRecord:
    urls = feed.get("urls") or {}
    auth = feed.get("authorization") or {}
    return FeedRecord(
        onestop_id=feed.get("onestop_id") or feed.get("id") or "",
        static_current=urls.get("static_current"),
        realtime_alerts=urls.get("realtime_alerts"),
        auth_type=auth.get("type"),
        auth_param=auth.get("param_name"),
        license=feed.get("license") or {},
    )


def _atlas_json(http: HttpClient, cache_dir: Path, filename: str) -> dict:
    path = cache_dir / "transitland" / filename
    if path.is_file() and time.time() - path.stat().st_mtime < ATLAS_MAX_AGE_S:
        return json.loads(path.read_text("utf-8"))
    _status, content, _ = http.get_bytes(f"{ATLAS_RAW}/{filename}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return json.loads(content)


def feed_record(
    onestop_id: str, *, settings: Settings, http: HttpClient, atlas_file: str | None
) -> FeedRecord | None:
    """Transitland's record for a feed, or None if it can't be found."""
    if settings.transitland_api_key:
        try:
            raw = http.get(f"{REST_BASE}/feeds/{onestop_id}", conditional=False,
                           headers={"apikey": settings.transitland_api_key})
            feeds = json.loads(raw.text).get("feeds") or []
            if feeds:
                return _parse_feed(feeds[0])
        except (httpx.HTTPError, ValueError):
            pass  # fall back to the public atlas
    if atlas_file:
        try:
            data = _atlas_json(http, settings.cache_dir, atlas_file)
        except (httpx.HTTPError, ValueError, OSError):
            return None
        for feed in data.get("feeds") or []:
            if feed.get("id") == onestop_id:
                return _parse_feed(feed)
    return None


def _local_zip(feeds_dir: Path, slug: str) -> Path | None:
    for name in ("google_transit.zip", f"{slug}.zip", "gtfs.zip"):
        if (feeds_dir / slug / name).is_file():
            return feeds_dir / slug / name
    return None


def fetch_gtfs(
    *,
    slug: str,
    transitland_id: str | None,
    agency_url: str | None,
    auth_env: str | None,
    atlas_file: str | None,
    settings: Settings,
    http: HttpClient,
) -> tuple[FetchResult, list[str]]:
    """Download a feed, trying Transitland first. Returns the result plus notes on
    any sources that were tried and skipped. Raises FileNotFoundError if every
    source fails."""
    notes: list[str] = []
    secret = _secret(settings, auth_env)
    auth_headers = {}

    record = None
    if transitland_id:
        record = feed_record(transitland_id, settings=settings, http=http,
                             atlas_file=atlas_file or atlas_file_for(agency_url))
        if record is None:
            notes.append(f"Transitland record {transitland_id} not found")
        elif record.auth_type == "header" and record.auth_param:
            auth_headers = {record.auth_param: secret} if secret else {}

        # 1. Transitland's hosted copy.
        if settings.transitland_api_key:
            try:
                _s, content, _ = http.get_bytes(
                    f"{REST_BASE}/feeds/{transitland_id}/download_latest_feed_version",
                    headers={"apikey": settings.transitland_api_key},
                )
                return FetchResult(content, "transitland", f"transit.land:{transitland_id}",
                                   transitland_id), notes
            except httpx.HTTPError as exc:
                notes.append(f"Transitland copy unavailable ({_short(exc)})")

        # 2. The official URL Transitland lists.
        if record and record.static_current:
            if record.auth_type and not auth_headers:
                notes.append(f"{record.static_current} needs {auth_env or 'a key'}")
            else:
                try:
                    _s, content, _ = http.get_bytes(record.static_current,
                                                    headers=auth_headers)
                    return FetchResult(content, "transitland-listed-url",
                                       record.static_current, transitland_id), notes
                except httpx.HTTPError as exc:
                    notes.append(f"{record.static_current} failed ({_short(exc)})")

    # 3. The agency URL from the TOML, then a manually saved file.
    if agency_url and (not record or agency_url != record.static_current):
        try:
            headers = auth_headers or ({"api_key": secret} if secret else {})
            _s, content, _ = http.get_bytes(agency_url, headers=headers)
            return FetchResult(content, "agency-url", agency_url, transitland_id), notes
        except httpx.HTTPError as exc:
            notes.append(f"{agency_url} failed ({_short(exc)})")
    local = _local_zip(settings.feeds_dir, slug)
    if local:
        return FetchResult(local.read_bytes(), "local file", str(local), transitland_id), notes
    raise FileNotFoundError(
        "; ".join(notes) or f"No feed source for '{slug}' and no local file at "
        f"{settings.feeds_dir / slug / 'google_transit.zip'}"
    )


def _short(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__
