"""SQLite connection management and upsert helpers.

All writes flow through :class:`Database` so change-detection, FTS sync, and
active-set reconciliation stay in one place.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import Any

from transit_mcp.models import (
    Alert,
    Chunk,
    Document,
    Fare,
    Line,
    Station,
    UnitStatus,
)

SCHEMA_PACKAGE = "transit_mcp.db"
SCHEMA_RESOURCE = "schema.sql"


def _iso(value: datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    return str(value)


def _load_schema_sql() -> str:
    return resources.files(SCHEMA_PACKAGE).joinpath(SCHEMA_RESOURCE).read_text("utf-8")


def connect(db_path: str | Path) -> sqlite3.Connection:
    """Open a connection with sane pragmas and Row access."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def _has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Bring a pre-existing database up to the current schema version.

    Runs BEFORE schema.sql so that ALTERs land before schema.sql creates indexes
    that reference the new columns. A brand-new database has no ``schema_meta``
    yet; schema.sql builds it at the current version, so there is nothing to do.
    """
    if not _table_exists(conn, "schema_meta"):
        return
    row = conn.execute("SELECT value FROM schema_meta WHERE key = 'version'").fetchone()
    version = int(row[0]) if row else 1

    if version < 2:
        # v2: agency namespacing + regional GTFS.
        if _table_exists(conn, "sources") and not _has_column(conn, "sources", "feed_id"):
            conn.execute("ALTER TABLE sources ADD COLUMN feed_id TEXT")
        if _table_exists(conn, "documents") and not _has_column(
            conn, "documents", "feed_id"
        ):
            conn.execute("ALTER TABLE documents ADD COLUMN feed_id TEXT")
        if _table_exists(conn, "ingest_runs"):
            # Relax the ingest_runs.scope CHECK (log table, no inbound FKs).
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("ALTER TABLE ingest_runs RENAME TO ingest_runs_v1")
            conn.execute(
                """
                CREATE TABLE ingest_runs (
                    id          INTEGER PRIMARY KEY,
                    started_at  TEXT NOT NULL DEFAULT (datetime('now')),
                    finished_at TEXT,
                    scope       TEXT NOT NULL
                                CHECK (scope IN ('api', 'web', 'gtfs', 'all')),
                    status      TEXT NOT NULL DEFAULT 'error'
                                CHECK (status IN ('ok', 'partial', 'error')),
                    stats_json  TEXT NOT NULL DEFAULT '{}',
                    error_text  TEXT
                )
                """
            )
            conn.execute(
                "INSERT INTO ingest_runs "
                "(id, started_at, finished_at, scope, status, stats_json, error_text) "
                "SELECT id, started_at, finished_at, scope, status, stats_json, "
                "error_text FROM ingest_runs_v1"
            )
            conn.execute("DROP TABLE ingest_runs_v1")
            conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("UPDATE schema_meta SET value = '2' WHERE key = 'version'")
        version = 2

    if version < 3:
        # v3: agencies are data-driven; WMATA becomes an ordinary agency slug.
        if _table_exists(conn, "agency_profiles") and not _has_column(
            conn, "agency_profiles", "mode"
        ):
            conn.execute("ALTER TABLE agency_profiles ADD COLUMN mode TEXT")
        if _table_exists(conn, "sources"):
            conn.execute("UPDATE sources SET feed_id = 'wmata' WHERE feed_id IS NULL")
        if _table_exists(conn, "documents"):
            conn.execute("UPDATE documents SET feed_id = 'wmata' WHERE feed_id IS NULL")
        conn.execute("UPDATE schema_meta SET value = '3' WHERE key = 'version'")


def init_db(db_path: str | Path) -> None:
    """Create (or upgrade) the schema. Idempotent."""
    conn = connect(db_path)
    try:
        _apply_migrations(conn)
        conn.executescript(_load_schema_sql())
        conn.commit()
    finally:
        conn.close()


class Database:
    """Thin wrapper around a sqlite3 connection with domain-specific writers."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.conn = connect(db_path)

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def commit(self) -> None:
        self.conn.commit()

    # -- provenance -------------------------------------------------------
    def get_source(self, url: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM sources WHERE url = ?", (url,)).fetchone()

    def upsert_source(
        self,
        *,
        url: str,
        source_type: str,
        category: str,
        http_status: int | None,
        content_hash: str | None,
        etag: str | None = None,
        last_modified: str | None = None,
        feed_id: str | None = None,
    ) -> tuple[int, bool]:
        """Insert or update a source row.

        Returns ``(source_id, changed)`` where ``changed`` is True when the
        content hash is new or differs from what we last stored.
        """
        now = _iso(datetime.now())
        existing = self.get_source(url)
        if existing is None:
            cur = self.conn.execute(
                """
                INSERT INTO sources
                    (url, source_type, category, feed_id, http_status, etag,
                     last_modified, content_hash, first_seen_at, fetched_at,
                     last_changed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (url, source_type, category, feed_id, http_status, etag,
                 last_modified, content_hash, now, now, now),
            )
            return int(cur.lastrowid), True

        changed = content_hash is not None and content_hash != existing["content_hash"]
        self.conn.execute(
            """
            UPDATE sources
               SET source_type = ?, category = ?, feed_id = ?, http_status = ?,
                   etag = ?, last_modified = ?,
                   content_hash = COALESCE(?, content_hash),
                   fetched_at = ?,
                   last_changed_at = CASE WHEN ? THEN ? ELSE last_changed_at END
             WHERE id = ?
            """,
            (source_type, category, feed_id, http_status, etag, last_modified,
             content_hash, now, 1 if changed else 0, now, existing["id"]),
        )
        return int(existing["id"]), changed

    # -- documents + chunks -------------------------------------------------
    def replace_document(
        self, *, source_id: int, document: Document, chunks: Iterable[Chunk]
    ) -> int:
        """Replace all documents/chunks for a source with a fresh set."""
        self.conn.execute("DELETE FROM documents WHERE source_id = ?", (source_id,))
        cur = self.conn.execute(
            """
            INSERT INTO documents
                (source_id, category, feed_id, title, summary, body_markdown, url,
                 effective_date, expires_at, retrieved_at, raw_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_id,
                document.category,
                getattr(document, "feed_id", None),
                document.title,
                document.summary,
                document.body_markdown,
                document.url,
                document.effective_date,
                document.expires_at,
                _iso(document.retrieved_at),
                document.raw_hash,
            ),
        )
        document_id = int(cur.lastrowid)
        self.conn.executemany(
            """
            INSERT INTO document_chunks
                (document_id, chunk_index, heading_path, text, char_len)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (document_id, c.chunk_index, c.heading_path, c.text, c.char_len)
                for c in chunks
            ],
        )
        return document_id

    # -- alerts ----------------------------------------------------------
    def upsert_alert(self, *, source_id: int | None, alert: Alert) -> None:
        self.conn.execute(
            """
            INSERT INTO alerts
                (source_id, provider_alert_id, mode, headline, description,
                 affected_lines, affected_stations, severity, starts_at, ends_at,
                 updated_at, raw_json, seen_at, resolved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(provider_alert_id) DO UPDATE SET
                source_id = excluded.source_id,
                mode = excluded.mode,
                headline = excluded.headline,
                description = excluded.description,
                affected_lines = excluded.affected_lines,
                affected_stations = excluded.affected_stations,
                severity = excluded.severity,
                starts_at = excluded.starts_at,
                ends_at = excluded.ends_at,
                updated_at = excluded.updated_at,
                raw_json = excluded.raw_json,
                seen_at = excluded.seen_at,
                resolved_at = NULL
            """,
            (
                source_id,
                alert.provider_alert_id,
                alert.mode,
                alert.headline,
                alert.description,
                json.dumps(alert.affected_lines),
                json.dumps(alert.affected_stations),
                alert.severity,
                _iso(alert.starts_at),
                _iso(alert.ends_at),
                _iso(alert.updated_at),
                alert.raw_json,
                _iso(datetime.now()),
            ),
        )

    def reconcile_alerts(self, *, mode: str, seen_ids: Iterable[str]) -> int:
        """Mark alerts of ``mode`` not in ``seen_ids`` as resolved. Returns count."""
        seen = list(seen_ids)
        now = _iso(datetime.now())
        if not seen:
            cur = self.conn.execute(
                "UPDATE alerts SET resolved_at = ? "
                "WHERE mode = ? AND resolved_at IS NULL",
                (now, mode),
            )
            return cur.rowcount
        placeholders = ",".join("?" for _ in seen)
        cur = self.conn.execute(
            f"""
            UPDATE alerts
               SET resolved_at = ?
             WHERE mode = ? AND resolved_at IS NULL
               AND provider_alert_id NOT IN ({placeholders})
            """,
            (now, mode, *seen),
        )
        return cur.rowcount

    # -- stations / lines -----------------------------------------------
    def upsert_station(self, *, source_id: int | None, station: Station) -> None:
        self.conn.execute(
            """
            INSERT INTO stations
                (code, name, lat, lon, line_codes, address_json, has_parking,
                 together_codes, source_id, raw_json, retrieved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                lat = excluded.lat,
                lon = excluded.lon,
                line_codes = excluded.line_codes,
                address_json = excluded.address_json,
                has_parking = excluded.has_parking,
                together_codes = excluded.together_codes,
                source_id = excluded.source_id,
                raw_json = excluded.raw_json,
                retrieved_at = excluded.retrieved_at
            """,
            (
                station.code,
                station.name,
                station.lat,
                station.lon,
                json.dumps(station.line_codes),
                json.dumps(station.address),
                1 if station.has_parking else 0,
                json.dumps(station.together_codes),
                source_id,
                station.raw_json,
                _iso(datetime.now()),
            ),
        )

    def upsert_line(self, *, source_id: int | None, line: Line) -> None:
        self.conn.execute(
            """
            INSERT INTO lines
                (code, display_name, color_hex, start_station_code,
                 end_station_code, source_id, raw_json, retrieved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                display_name = excluded.display_name,
                color_hex = excluded.color_hex,
                start_station_code = excluded.start_station_code,
                end_station_code = excluded.end_station_code,
                source_id = excluded.source_id,
                raw_json = excluded.raw_json,
                retrieved_at = excluded.retrieved_at
            """,
            (
                line.code,
                line.display_name,
                line.color_hex,
                line.start_station_code,
                line.end_station_code,
                source_id,
                line.raw_json,
                _iso(datetime.now()),
            ),
        )

    # -- unit status ---------------------------------------------------
    def upsert_unit_status(self, *, source_id: int | None, unit: UnitStatus) -> None:
        self.conn.execute(
            """
            INSERT INTO unit_status
                (station_code, unit_type, unit_name, location_description, symptom,
                 out_of_service, date_out_of_service, estimated_return, updated_at,
                 source_id, raw_json, seen_at, cleared_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ON CONFLICT(station_code, unit_name) DO UPDATE SET
                unit_type = excluded.unit_type,
                location_description = excluded.location_description,
                symptom = excluded.symptom,
                out_of_service = excluded.out_of_service,
                date_out_of_service = excluded.date_out_of_service,
                estimated_return = excluded.estimated_return,
                updated_at = excluded.updated_at,
                source_id = excluded.source_id,
                raw_json = excluded.raw_json,
                seen_at = excluded.seen_at,
                cleared_at = NULL
            """,
            (
                unit.station_code,
                unit.unit_type,
                unit.unit_name,
                unit.location_description,
                unit.symptom,
                1 if unit.out_of_service else 0,
                _iso(unit.date_out_of_service),
                unit.estimated_return,
                _iso(unit.updated_at),
                source_id,
                unit.raw_json,
                _iso(datetime.now()),
            ),
        )

    def reconcile_unit_status(self, *, seen_keys: Iterable[tuple[str, str]]) -> int:
        """Mark units not seen in the latest run as cleared. Returns count."""
        seen = list(seen_keys)
        cur = self.conn.execute(
            "SELECT id, station_code, unit_name FROM unit_status WHERE cleared_at IS NULL"
        )
        seen_set = set(seen)
        to_clear = [
            row["id"]
            for row in cur.fetchall()
            if (row["station_code"], row["unit_name"]) not in seen_set
        ]
        if to_clear:
            now = _iso(datetime.now())
            self.conn.executemany(
                "UPDATE unit_status SET cleared_at = ? WHERE id = ?",
                [(now, i) for i in to_clear],
            )
        return len(to_clear)

    # -- fares ---------------------------------------------------------
    def upsert_fare(self, *, source_id: int | None, fare: Fare) -> None:
        self.conn.execute(
            """
            INSERT INTO fares
                (origin_code, dest_code, peak_fare, offpeak_fare,
                 senior_disabled_fare, rail_time_min, source_id, raw_json, retrieved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(origin_code, dest_code) DO UPDATE SET
                peak_fare = excluded.peak_fare,
                offpeak_fare = excluded.offpeak_fare,
                senior_disabled_fare = excluded.senior_disabled_fare,
                rail_time_min = excluded.rail_time_min,
                source_id = excluded.source_id,
                raw_json = excluded.raw_json,
                retrieved_at = excluded.retrieved_at
            """,
            (
                fare.origin_code,
                fare.dest_code,
                fare.peak_fare,
                fare.offpeak_fare,
                fare.senior_disabled_fare,
                fare.rail_time_min,
                source_id,
                fare.raw_json,
                _iso(datetime.now()),
            ),
        )

    # -- ingest run log ----------------------------------------------
    def start_run(self, scope: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO ingest_runs (scope, started_at) VALUES (?, ?)",
            (scope, _iso(datetime.now())),
        )
        self.commit()
        return int(cur.lastrowid)

    def finish_run(
        self, run_id: int, *, status: str, stats: dict[str, Any], error: str | None = None
    ) -> None:
        self.conn.execute(
            """
            UPDATE ingest_runs
               SET finished_at = ?, status = ?, stats_json = ?, error_text = ?
             WHERE id = ?
            """,
            (_iso(datetime.now()), status, json.dumps(stats, default=str), error, run_id),
        )
        self.commit()

    # -- introspection ---------------------------------------------
    def table_counts(self) -> dict[str, int]:
        tables = [
            "sources", "documents", "document_chunks", "alerts", "stations",
            "lines", "unit_status", "fares", "ingest_runs",
            "gtfs_feeds", "gtfs_routes", "gtfs_stops", "gtfs_trips",
            "gtfs_stop_times", "route_service_summary", "stop_service_summary",
            "stop_links", "agency_profiles", "fare_vending",
        ]
        out: dict[str, int] = {}
        for t in tables:
            row = self.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()
            out[t] = int(row["n"])
        out["alerts_active"] = int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM alerts WHERE resolved_at IS NULL"
            ).fetchone()["n"]
        )
        out["unit_status_active"] = int(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM unit_status WHERE cleared_at IS NULL"
            ).fetchone()["n"]
        )
        return out

    def freshness(self) -> dict[str, Any]:
        by_category = self.conn.execute(
            """
            SELECT category,
                   COUNT(*) AS sources,
                   MIN(fetched_at) AS oldest,
                   MAX(fetched_at) AS newest
              FROM sources
             GROUP BY category
             ORDER BY category
            """
        ).fetchall()
        last_runs = self.conn.execute(
            """
            SELECT scope, MAX(finished_at) AS last_finished, status
              FROM ingest_runs
             WHERE finished_at IS NOT NULL
             GROUP BY scope
            """
        ).fetchall()
        return {
            "by_category": [dict(r) for r in by_category],
            "last_runs": [dict(r) for r in last_runs],
        }
