from __future__ import annotations

from transit_mcp.ingest.gtfs.loader import GtfsFeed, load_feed

MINI_A = GtfsFeed("mini-a", "Mini A Transit", "bus", None)
MINI_B = GtfsFeed("mini-b", "Mini B Transit", "bus", None)


def _count(db, table, feed_id):
    return db.conn.execute(
        f"SELECT COUNT(*) n FROM {table} WHERE feed_id = ?", (feed_id,)
    ).fetchone()["n"]


def test_load_local_feed_populates_tables(tmp_db, gtfs_feeds_dir):
    stats = load_feed(tmp_db, http=None, feed=MINI_A, feeds_dir=gtfs_feeds_dir)
    assert stats["status"] == "loaded"
    assert stats["is_local"] is True

    assert _count(tmp_db, "gtfs_agencies", "mini-a") == 1
    assert _count(tmp_db, "gtfs_routes", "mini-a") == 1
    assert _count(tmp_db, "gtfs_stops", "mini-a") == 3
    assert _count(tmp_db, "gtfs_trips", "mini-a") == 3
    assert _count(tmp_db, "gtfs_stop_times", "mini-a") == 6
    assert _count(tmp_db, "gtfs_fare_attributes", "mini-a") == 1
    assert _count(tmp_db, "gtfs_transfers", "mini-a") == 1

    feed = tmp_db.conn.execute(
        "SELECT * FROM gtfs_feeds WHERE feed_id = 'mini-a'"
    ).fetchone()
    assert feed["status"] == "loaded"
    assert feed["publisher_name"] == "Mini A Transit"
    assert feed["feed_start_date"] == "20260101"
    assert feed["feed_hash"]


def test_shapes_txt_is_skipped(tmp_db, gtfs_feeds_dir):
    load_feed(tmp_db, http=None, feed=MINI_A, feeds_dir=gtfs_feeds_dir)
    tables = {
        r["name"]
        for r in tmp_db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "gtfs_shapes" not in tables


def test_no_stop_times_flag(tmp_db, gtfs_feeds_dir):
    load_feed(
        tmp_db, http=None, feed=MINI_A, feeds_dir=gtfs_feeds_dir, load_stop_times=False
    )
    assert _count(tmp_db, "gtfs_stop_times", "mini-a") == 0
    assert _count(tmp_db, "gtfs_stops", "mini-a") == 3


def test_reload_is_idempotent_and_hash_short_circuits(tmp_db, gtfs_feeds_dir):
    load_feed(tmp_db, http=None, feed=MINI_A, feeds_dir=gtfs_feeds_dir)
    first_hash = tmp_db.conn.execute(
        "SELECT feed_hash FROM gtfs_feeds WHERE feed_id='mini-a'"
    ).fetchone()["feed_hash"]

    stats2 = load_feed(tmp_db, http=None, feed=MINI_A, feeds_dir=gtfs_feeds_dir)
    assert stats2["status"] == "unchanged"
    assert _count(tmp_db, "gtfs_stop_times", "mini-a") == 6  # not doubled

    # Force a reload by clearing the stored hash; row counts must stay stable.
    tmp_db.conn.execute("UPDATE gtfs_feeds SET feed_hash='x' WHERE feed_id='mini-a'")
    tmp_db.commit()
    load_feed(tmp_db, http=None, feed=MINI_A, feeds_dir=gtfs_feeds_dir)
    assert _count(tmp_db, "gtfs_stop_times", "mini-a") == 6
    assert (
        tmp_db.conn.execute(
            "SELECT feed_hash FROM gtfs_feeds WHERE feed_id='mini-a'"
        ).fetchone()["feed_hash"]
        == first_hash
    )


def test_missing_feed_marked_pending(tmp_db, tmp_path):
    empty = tmp_path / "feeds"
    empty.mkdir()
    stats = load_feed(tmp_db, http=None, feed=MINI_B, feeds_dir=empty)
    assert stats["status"] == "pending"
    row = tmp_db.conn.execute(
        "SELECT status FROM gtfs_feeds WHERE feed_id='mini-b'"
    ).fetchone()
    assert row["status"] == "pending"
