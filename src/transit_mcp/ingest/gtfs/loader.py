"""Load one GTFS zip into the namespaced ``gtfs_*`` tables.

Deliberately dependency-free: stdlib ``zipfile`` + ``csv`` streamed into SQLite.
``shapes.txt`` (90-95% of a feed) is not loaded row by row; ``shapes.py`` stores each
shape simplified and encoded, for drawing routes on the map. Trips given as a headway
(``frequencies.txt``, e.g. "every 17 minutes, all day") become one trip per run.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from transit_mcp.db.database import Database
from transit_mcp.ingest.gtfs.shapes import load_shapes
from transit_mcp.ingest.http import HttpClient
from transit_mcp.ingest.transitland import FetchResult

BATCH = 5000
ARCHIVE_MANIFEST = "manifest.json"
ARCHIVE_CONTRACT = "gtfs-archive/1"


@dataclass(frozen=True)
class GtfsFeed:
    """What the loader needs to know about one feed."""

    slug: str
    agency_name: str
    mode: str
    url: str | None  # None => manual: expects feeds/<slug>/google_transit.zip

# gtfs file -> (table, {gtfs_column: db_column})
FILE_MAP: dict[str, tuple[str, dict[str, str]]] = {
    "agency.txt": ("gtfs_agencies", {
        "agency_id": "agency_id", "agency_name": "name", "agency_url": "url",
        "agency_timezone": "timezone", "agency_lang": "lang",
        "agency_phone": "phone", "agency_fare_url": "fare_url",
    }),
    "routes.txt": ("gtfs_routes", {
        "route_id": "route_id", "agency_id": "agency_id",
        "route_short_name": "short_name", "route_long_name": "long_name",
        "route_desc": "description", "route_type": "route_type",
        "route_url": "route_url", "route_color": "color",
        "route_text_color": "text_color",
    }),
    "stops.txt": ("gtfs_stops", {
        "stop_id": "stop_id", "stop_code": "code", "stop_name": "name",
        "stop_desc": "description", "stop_lat": "lat", "stop_lon": "lon",
        "zone_id": "zone_id", "location_type": "location_type",
        "parent_station": "parent_station",
        "wheelchair_boarding": "wheelchair_boarding",
    }),
    "trips.txt": ("gtfs_trips", {
        "trip_id": "trip_id", "route_id": "route_id", "service_id": "service_id",
        "trip_headsign": "headsign", "direction_id": "direction_id",
        "block_id": "block_id", "shape_id": "shape_id",
        "wheelchair_accessible": "wheelchair_accessible",
    }),
    "stop_times.txt": ("gtfs_stop_times", {
        "trip_id": "trip_id", "stop_id": "stop_id",
        "stop_sequence": "stop_sequence", "arrival_time": "arrival_time",
        "departure_time": "departure_time", "pickup_type": "pickup_type",
        "drop_off_type": "drop_off_type", "stop_headsign": "stop_headsign",
    }),
    "calendar.txt": ("gtfs_calendar", {
        "service_id": "service_id", "monday": "monday", "tuesday": "tuesday",
        "wednesday": "wednesday", "thursday": "thursday", "friday": "friday",
        "saturday": "saturday", "sunday": "sunday",
        "start_date": "start_date", "end_date": "end_date",
    }),
    "calendar_dates.txt": ("gtfs_calendar_dates", {
        "service_id": "service_id", "date": "date",
        "exception_type": "exception_type",
    }),
    "frequencies.txt": ("gtfs_frequencies", {
        "trip_id": "trip_id", "start_time": "start_time", "end_time": "end_time",
        "headway_secs": "headway_secs", "exact_times": "exact_times",
    }),
    "transfers.txt": ("gtfs_transfers", {
        "from_stop_id": "from_stop_id", "to_stop_id": "to_stop_id",
        "transfer_type": "transfer_type", "min_transfer_time": "min_transfer_time",
    }),
    "fare_attributes.txt": ("gtfs_fare_attributes", {
        "fare_id": "fare_id", "price": "price", "currency_type": "currency_type",
        "payment_method": "payment_method", "transfers": "transfers",
        "agency_id": "agency_id", "transfer_duration": "transfer_duration",
    }),
    "fare_rules.txt": ("gtfs_fare_rules", {
        "fare_id": "fare_id", "route_id": "route_id", "origin_id": "origin_id",
        "destination_id": "destination_id", "contains_id": "contains_id",
    }),
    "fare_products.txt": ("gtfs_fare_products", {
        "fare_product_id": "fare_product_id",
        "fare_product_name": "fare_product_name",
        "fare_media_id": "fare_media_id", "amount": "amount",
        "currency": "currency",
    }),
    "fare_media.txt": ("gtfs_fare_media", {
        "fare_media_id": "fare_media_id", "fare_media_name": "fare_media_name",
        "fare_media_type": "fare_media_type",
    }),
    "rider_categories.txt": ("gtfs_rider_categories", {
        "rider_category_id": "rider_category_id",
        "rider_category_name": "rider_category_name",
        "is_default_fare_category": "is_default_fare_category",
        "eligibility_url": "eligibility_url",
    }),
    "ticketing_deep_links.txt": ("gtfs_ticketing_deep_links", {
        "ticketing_deep_link_id": "ticketing_deep_link_id",
        "web_url": "web_url", "android_intent_uri": "android_intent_uri",
        "ios_universal_link_url": "ios_universal_link_url",
    }),
    "ticketing_identifiers.txt": ("gtfs_ticketing_identifiers", {
        "stop_id": "stop_id", "ticketing_stop_id": "ticketing_stop_id",
        "ticketing_deep_link_id": "ticketing_deep_link_id",
    }),
}

SKIP_FILES = {"shapes.txt"}  # loaded simplified by shapes.py instead
_STOP_TIMES = "stop_times.txt"

GTFS_TABLES = sorted({tbl for tbl, _ in FILE_MAP.values()})


def _feed_bytes(feed: GtfsFeed, feeds_dir: Path, http: HttpClient) -> FetchResult:
    """Simple fetch without Transitland: a local override file, else ``feed.url``."""
    for name in ("google_transit.zip", f"{feed.slug}.zip", "gtfs.zip"):
        local = feeds_dir / feed.slug / name
        if local.is_file():
            return FetchResult(local.read_bytes(), "local file", str(local), None)
    if not feed.url:
        raise FileNotFoundError(
            f"No feed URL for '{feed.slug}' and no local file at "
            f"{feeds_dir / feed.slug / 'google_transit.zip'}"
        )
    _status, content, _headers = http.get_bytes(feed.url)
    return FetchResult(content, "agency-url", feed.url, None)


def _iter_rows(zf: zipfile.ZipFile, member: str):
    """Yield dict rows from a GTFS member with header whitespace/BOM stripped."""
    with zf.open(member) as raw:
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        reader = csv.reader(text)
        try:
            header = [h.strip() for h in next(reader)]
        except StopIteration:
            return
        for row in reader:
            if not row:
                continue
            yield dict(zip(header, (c.strip() for c in row)))


FREQ_SEP = "@"   # run trip IDs: "<template trip>@HH:MM:SS"


def _secs(t: str | None) -> int | None:
    try:
        h, m, s = (t or "").strip().split(":")
        return int(h) * 3600 + int(m) * 60 + int(s)
    except ValueError:
        return None


def _hms(s: int) -> str:
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def expand_frequencies(db: Database, feed_id: str) -> int:
    """Turn each headway trip (``frequencies.txt``) into one trip per run, so code that
    reads ``gtfs_stop_times`` sees every departure. The template trip's times only give
    the time between its stops; it is replaced by its runs ("<trip>@HH:MM:SS"). Safe to
    run again: templates already expanded have no stop times left."""
    conn = db.conn
    freqs = conn.execute(
        "SELECT trip_id, start_time, end_time, headway_secs FROM gtfs_frequencies "
        "WHERE feed_id = ? ORDER BY trip_id, start_time", (feed_id,)).fetchall()
    by_trip: dict[str, list] = {}
    for f in freqs:
        by_trip.setdefault(f["trip_id"], []).append(f)
    runs = 0
    for trip_id, windows in by_trip.items():
        trip = conn.execute("SELECT * FROM gtfs_trips WHERE feed_id = ? AND trip_id = ?",
                            (feed_id, trip_id)).fetchone()
        calls = conn.execute(
            "SELECT * FROM gtfs_stop_times WHERE feed_id = ? AND trip_id = ? "
            "ORDER BY stop_sequence", (feed_id, trip_id)).fetchall()
        if trip is None or not calls:
            continue
        base = _secs(calls[0]["departure_time"]) or _secs(calls[0]["arrival_time"]) or 0
        trip_rows, time_rows = [], []
        for w in windows:
            start, end, step = _secs(w["start_time"]), _secs(w["end_time"]), w["headway_secs"]
            if start is None or end is None or not step or step <= 0:
                continue
            for t in range(start, end, int(step)):
                run_id = f"{trip_id}{FREQ_SEP}{_hms(t)}"
                trip_rows.append((feed_id, run_id, trip["route_id"], trip["service_id"],
                                  trip["headsign"], trip["direction_id"], trip["block_id"],
                                  trip["shape_id"], trip["wheelchair_accessible"]))
                for c in calls:
                    arr, dep = _secs(c["arrival_time"]), _secs(c["departure_time"])
                    time_rows.append((
                        feed_id, run_id, c["stop_id"], c["stop_sequence"],
                        _hms(arr - base + t) if arr is not None else None,
                        _hms(dep - base + t) if dep is not None else None,
                        c["pickup_type"], c["drop_off_type"], c["stop_headsign"]))
        if not trip_rows:
            continue
        conn.execute("DELETE FROM gtfs_stop_times WHERE feed_id = ? AND trip_id = ?",
                     (feed_id, trip_id))
        conn.executemany(
            "INSERT OR REPLACE INTO gtfs_trips (feed_id, trip_id, route_id, service_id, "
            "headsign, direction_id, block_id, shape_id, wheelchair_accessible) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", trip_rows)
        conn.executemany(
            "INSERT INTO gtfs_stop_times (feed_id, trip_id, stop_id, stop_sequence, "
            "arrival_time, departure_time, pickup_type, drop_off_type, stop_headsign) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", time_rows)
        runs += len(trip_rows)
    db.commit()
    return runs


def _load_member(
    db: Database, feed_id: str, zf: zipfile.ZipFile, member: str
) -> int:
    table, colmap = FILE_MAP[member]
    gtfs_cols = list(colmap.keys())
    db_cols = ["feed_id", *[colmap[c] for c in gtfs_cols]]
    placeholders = ", ".join("?" for _ in db_cols)
    sql = f"INSERT INTO {table} ({', '.join(db_cols)}) VALUES ({placeholders})"

    db.conn.execute(f"DELETE FROM {table} WHERE feed_id = ?", (feed_id,))
    batch: list[tuple] = []
    n = 0
    for row in _iter_rows(zf, member):
        values = [feed_id] + [(row.get(c) or None) for c in gtfs_cols]
        batch.append(tuple(values))
        if len(batch) >= BATCH:
            db.conn.executemany(sql, batch)
            n += len(batch)
            batch.clear()
    if batch:
        db.conn.executemany(sql, batch)
        n += len(batch)
    return n


def _feed_info(zf: zipfile.ZipFile) -> dict[str, str]:
    if "feed_info.txt" in zf.namelist():
        for row in _iter_rows(zf, "feed_info.txt"):
            return row
    return {}


def archive_feed(
    archive_dir: Path,
    feed: GtfsFeed,
    content: bytes,
    *,
    is_local: bool,
    info: dict[str, str] | None = None,
    source: str | None = None,
) -> Path:
    """Keep a copy of the raw GTFS zip at ``<archive_dir>/<slug>.zip`` and record it
    in ``manifest.json`` — the hand-off point for external scheduling engines."""
    archive_dir.mkdir(parents=True, exist_ok=True)
    target = archive_dir / f"{feed.slug}.zip"
    sha = hashlib.sha256(content).hexdigest()
    if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != sha:
        target.write_bytes(content)

    manifest_path = archive_dir / ARCHIVE_MANIFEST
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
    except (OSError, ValueError):
        manifest = {}
    feeds = manifest.get("feeds", {})
    info = info or feeds.get(feed.slug, {})
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    feeds[feed.slug] = {
        "agency": feed.slug,
        "name": feed.agency_name,
        "mode": feed.mode,
        "file": target.name,
        "sha256": sha,
        "bytes": len(content),
        "source": "local file" if is_local else (source or feed.url),
        "fetched_at": now,
        "feed_start_date": info.get("feed_start_date"),
        "feed_end_date": info.get("feed_end_date"),
    }
    manifest_path.write_text(
        json.dumps(
            {"contract": ARCHIVE_CONTRACT, "updated_at": now, "feeds": feeds}, indent=2
        ),
        "utf-8",
    )
    return target


def load_feed(
    db: Database,
    http: HttpClient,
    feed: GtfsFeed,
    *,
    feeds_dir: Path,
    load_stop_times: bool = True,
    archive_dir: Path | None = None,
    fetcher: Callable[[], FetchResult] | None = None,
    index_stops: bool = True,
) -> dict:
    """Download/read a GTFS zip and load it into ``gtfs_*``. Idempotent per feed.

    If ``archive_dir`` is given, the raw zip is also kept there (see
    :func:`archive_feed`) whether or not its contents changed. ``fetcher`` replaces
    the plain URL download (the GTFS source passes the Transitland resolver).
    """
    now = datetime.now().isoformat(sep=" ", timespec="seconds")
    stats: dict = {"feed_id": feed.slug, "rows": {}}

    try:
        fetched = fetcher() if fetcher else _feed_bytes(feed, feeds_dir, http)
    except Exception as exc:  # noqa: BLE001
        db.conn.execute(
            """
            INSERT INTO gtfs_feeds (feed_id, agency_name, mode, source_url, status,
                                    error_text, fetched_at)
            VALUES (?, ?, ?, ?, 'pending', ?, ?)
            ON CONFLICT(feed_id) DO UPDATE SET
                status = 'pending', error_text = excluded.error_text,
                fetched_at = excluded.fetched_at
            """,
            (feed.slug, feed.agency_name, feed.mode, feed.url, str(exc), now),
        )
        db.commit()
        stats["status"] = "pending"
        stats["error"] = str(exc)
        return stats

    content, is_local = fetched.content, fetched.via == "local file"
    stats.update(fetched_via=fetched.via, source_url=fetched.url)
    if fetched.transitland_id:
        stats["transitland_id"] = fetched.transitland_id
    provenance = (fetched.url, fetched.via, fetched.transitland_id, int(index_stops))
    feed_hash = hashlib.sha256(content).hexdigest()
    existing = db.conn.execute(
        "SELECT feed_hash, status FROM gtfs_feeds WHERE feed_id = ?", (feed.slug,)
    ).fetchone()
    if existing and existing["feed_hash"] == feed_hash and existing["status"] == "loaded":
        db.conn.execute(
            "UPDATE gtfs_feeds SET fetched_at = ?, source_url = ?, fetched_via = ?, "
            "transitland_id = ?, index_stops = ? WHERE feed_id = ?",
            (now, *provenance, feed.slug),
        )
        # Feeds loaded before shapes were kept get them on their next refresh.
        if not db.conn.execute("SELECT 1 FROM gtfs_shapes WHERE feed_id = ? LIMIT 1",
                               (feed.slug,)).fetchone():
            stats["shapes"] = load_shapes(db, feed.slug, zipfile.ZipFile(io.BytesIO(content)))
        # ... and headway trips loaded before they were expanded.
        if load_stop_times:
            stats["frequency_trips"] = expand_frequencies(db, feed.slug)
        db.commit()
        if archive_dir is not None:
            stats["archived"] = str(
                archive_feed(archive_dir, feed, content, is_local=is_local,
                             source=fetched.url)
            )
        stats["status"] = "unchanged"
        return stats

    zf = zipfile.ZipFile(io.BytesIO(content))
    present = set(zf.namelist())
    for member in FILE_MAP:
        if member in SKIP_FILES or member not in present:
            continue
        if member == _STOP_TIMES and not load_stop_times:
            continue
        stats["rows"][member] = _load_member(db, feed.slug, zf, member)
    stats["shapes"] = load_shapes(db, feed.slug, zf)  # simplified, for maps
    if load_stop_times:
        stats["frequency_trips"] = expand_frequencies(db, feed.slug)

    info = _feed_info(zf)
    db.conn.execute(
        """
        INSERT INTO gtfs_feeds
            (feed_id, agency_name, mode, source_url, is_local, feed_hash,
             feed_start_date, feed_end_date, publisher_name, publisher_url,
             status, error_text, fetched_at, last_changed_at,
             fetched_via, transitland_id, index_stops)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'loaded', NULL, ?, ?, ?, ?, ?)
        ON CONFLICT(feed_id) DO UPDATE SET
            agency_name = excluded.agency_name,
            mode = excluded.mode,
            source_url = excluded.source_url,
            is_local = excluded.is_local,
            feed_hash = excluded.feed_hash,
            feed_start_date = excluded.feed_start_date,
            feed_end_date = excluded.feed_end_date,
            publisher_name = excluded.publisher_name,
            publisher_url = excluded.publisher_url,
            status = 'loaded',
            error_text = NULL,
            fetched_at = excluded.fetched_at,
            last_changed_at = excluded.fetched_at,
            fetched_via = excluded.fetched_via,
            transitland_id = excluded.transitland_id,
            index_stops = excluded.index_stops
        """,
        (
            feed.slug, feed.agency_name, feed.mode, provenance[0], int(is_local),
            feed_hash,
            info.get("feed_start_date"), info.get("feed_end_date"),
            info.get("feed_publisher_name"), info.get("feed_publisher_url"),
            now, now, *provenance[1:],
        ),
    )
    db.commit()
    if archive_dir is not None:
        stats["archived"] = str(
            archive_feed(archive_dir, feed, content, is_local=is_local, info=info,
                         source=fetched.url)
        )
    stats["status"] = "loaded"
    stats["is_local"] = is_local
    return stats
