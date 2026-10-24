from __future__ import annotations

import hashlib
import json

from transit_mcp.db.database import Database, init_db
from transit_mcp.ingest.gtfs.loader import GtfsFeed, load_feed

MINI_A = GtfsFeed("mini-a", "Mini A Transit", "bus", None)


def test_loaded_feed_is_archived_with_manifest(tmp_db, gtfs_feeds_dir, tmp_path):
    archive = tmp_path / "archive"
    stats = load_feed(tmp_db, http=None, feed=MINI_A, feeds_dir=gtfs_feeds_dir,
                      archive_dir=archive)
    src = (gtfs_feeds_dir / "mini-a" / "google_transit.zip").read_bytes()
    assert (archive / "mini-a.zip").read_bytes() == src
    assert stats["archived"].endswith("mini-a.zip")

    manifest = json.loads((archive / "manifest.json").read_text("utf-8"))
    entry = manifest["feeds"]["mini-a"]
    assert manifest["contract"] == "gtfs-archive/1"
    assert entry["sha256"] == hashlib.sha256(src).hexdigest()
    assert entry["feed_start_date"] == "20260101" and entry["source"] == "local file"

    # Unchanged reload keeps the archive and its feed_info dates.
    again = load_feed(tmp_db, http=None, feed=MINI_A, feeds_dir=gtfs_feeds_dir,
                      archive_dir=archive)
    assert again["status"] == "unchanged"
    entry2 = json.loads((archive / "manifest.json").read_text("utf-8"))["feeds"]["mini-a"]
    assert entry2["feed_end_date"] == "20261231"


def test_v2_database_migrates_to_current(tmp_path):
    path = tmp_path / "old.db"
    init_db(path)
    with Database(path) as db:  # rewind to a v2-shaped database
        db.conn.execute("ALTER TABLE agency_profiles DROP COLUMN mode")
        db.conn.execute("UPDATE schema_meta SET value='2' WHERE key='version'")
        sid, _ = db.upsert_source(url="https://www.wmata.com/pay.html", source_type="web",
                                  category="fares", http_status=200, content_hash="h")
        db.conn.execute(
            "INSERT INTO documents (source_id, category, title, body_markdown, url, raw_hash)"
            " VALUES (?, 'fares', 'Pay', 'body', 'https://www.wmata.com/pay.html', 'h')",
            (sid,),
        )
        db.commit()

    init_db(path)

    with Database(path) as db:
        cols = [r[1] for r in db.conn.execute("PRAGMA table_info(agency_profiles)")]
        assert "mode" in cols
        assert db.conn.execute("SELECT value FROM schema_meta").fetchone()[0] == "4"
        assert db.conn.execute("SELECT feed_id FROM documents").fetchone()[0] == "wmata"
        assert db.conn.execute("SELECT feed_id FROM sources").fetchone()[0] == "wmata"
    init_db(path)  # idempotent


def test_v3_database_gains_transitland_and_alert_columns(tmp_path):
    path = tmp_path / "v3.db"
    init_db(path)
    with Database(path) as db:  # rewind to a v3-shaped database
        for table, col in (("gtfs_feeds", "transitland_id"), ("gtfs_feeds", "fetched_via"),
                           ("gtfs_feeds", "index_stops"), ("alerts", "feed_id"),
                           ("alerts", "affected_routes"), ("alerts", "affected_stops"),
                           ("alerts", "url")):
            if table == "alerts" and col == "feed_id":
                db.conn.execute("DROP INDEX IF EXISTS idx_alerts_feed")
            db.conn.execute(f"ALTER TABLE {table} DROP COLUMN {col}")
        db.conn.execute("UPDATE schema_meta SET value='3' WHERE key='version'")
        db.conn.execute(
            "INSERT INTO alerts (provider_alert_id, mode, headline, raw_json) "
            "VALUES ('rail:1', 'rail', 'Delays', '{}')"
        )
        db.commit()

    init_db(path)

    with Database(path) as db:
        feed_cols = {r[1] for r in db.conn.execute("PRAGMA table_info(gtfs_feeds)")}
        assert {"transitland_id", "fetched_via", "index_stops"} <= feed_cols
        row = db.conn.execute("SELECT feed_id, affected_routes FROM alerts").fetchone()
        assert (row[0], row[1]) == ("wmata", "[]")
        assert db.conn.execute("SELECT value FROM schema_meta").fetchone()[0] == "4"
