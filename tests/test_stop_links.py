from __future__ import annotations

import pytest

from transit_mcp.ingest.gtfs.links import build_stop_links
from transit_mcp.ingest.gtfs.loader import GtfsFeed, load_feed
from transit_mcp.ingest.gtfs.summarize import summarize_feed

MINI_A = GtfsFeed("mini-a", "Mini A Transit", "bus", None)
MINI_B = GtfsFeed("mini-b", "Mini B Transit", "bus", None)


@pytest.fixture
def linked(tmp_db, gtfs_feeds_dir):
    for feed in (MINI_A, MINI_B):
        load_feed(tmp_db, http=None, feed=feed, feeds_dir=gtfs_feeds_dir)
        summarize_feed(tmp_db, feed.slug)
    return tmp_db


def _link(db, a_id, b_id):
    return db.conn.execute(
        "SELECT * FROM stop_links WHERE (a_id=? AND b_id=?) OR (a_id=? AND b_id=?)",
        (a_id, b_id, b_id, a_id),
    ).fetchone()


def test_near_pair_linked_far_pair_not(linked):
    stats = build_stop_links(linked, radius_m=250)
    assert stats["links"] >= 1

    # A2 (38.9100,-77.0500) and B1 (38.9101,-77.0501) are ~14 m apart.
    near = _link(linked, "A2", "B1")
    assert near is not None
    assert near["distance_m"] <= 30
    assert near["walk_min"] >= 1

    # A3 (38.9180,-77.0500) is ~880 m from B1 — must not link.
    assert _link(linked, "A3", "B1") is None


def test_link_carries_service_overlap(linked):
    build_stop_links(linked, radius_m=250)
    near = _link(linked, "A2", "B1")
    # A2 runs every day, B1 runs Mon-Fri -> overlap is the weekdays, no weekend.
    assert near["overlap_days"] == "Mon–Fri"
    assert near["both_serve_weekend"] == 0


def test_wmata_station_links_to_nearby_gtfs_stop(linked):
    linked.conn.execute(
        """
        INSERT INTO stations (code, name, lat, lon, line_codes, raw_json)
        VALUES ('X01', 'Test Station', 38.9000, -77.0001, '["RD"]', '{}')
        """
    )
    linked.commit()
    build_stop_links(linked, radius_m=250)

    row = linked.conn.execute(
        "SELECT * FROM stop_links WHERE a_id='X01' OR b_id='X01'"
    ).fetchone()
    assert row is not None
    assert "wmata" in (row["a_kind"], row["b_kind"])
    # Metro is treated as running every day; A1 runs every day -> full overlap.
    assert row["overlap_days"] == "Every day"


def test_in_feed_transfers_imported(linked):
    stats = build_stop_links(linked, radius_m=10)  # tiny radius: proximity finds nothing
    assert stats["links"] == 0
    assert stats["gtfs_transfers"] == 1  # mini-a transfers.txt has A1 -> A2
    row = linked.conn.execute(
        "SELECT * FROM stop_links WHERE link_source='gtfs_transfers'"
    ).fetchone()
    assert {row["a_id"], row["b_id"]} == {"A1", "A2"}
