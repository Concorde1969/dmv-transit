from __future__ import annotations

import pytest

from transit_mcp.ingest.gtfs.loader import GtfsFeed, load_feed
from transit_mcp.ingest.gtfs.summarize import summarize_feed, synthesize_docs
from transit_mcp.retrieval.search import search

MINI_A = GtfsFeed("mini-a", "Mini A Transit", "bus", None)
MINI_B = GtfsFeed("mini-b", "Mini B Transit", "bus", None)


@pytest.fixture
def loaded(tmp_db, gtfs_feeds_dir):
    for feed in (MINI_A, MINI_B):
        load_feed(tmp_db, http=None, feed=feed, feeds_dir=gtfs_feeds_dir)
        summarize_feed(tmp_db, feed.slug)
    return tmp_db


def _stop(db, feed, stop_id):
    return db.conn.execute(
        "SELECT * FROM stop_service_summary WHERE feed_id=? AND stop_id=?",
        (feed, stop_id),
    ).fetchone()


def test_daily_stop_serves_weekend_and_evening(loaded):
    a2 = _stop(loaded, "mini-a", "A2")
    assert (a2["runs_mon"], a2["runs_sat"], a2["runs_sun"]) == (1, 1, 1)
    assert a2["serves_weekend"] == 1
    assert a2["evening_service"] == 1  # 21:45 departure
    assert a2["weekday_first"] == "06:15:00"
    assert a2["weekday_last"] == "21:45:00"


def test_weekday_only_stop_flags(loaded):
    b1 = _stop(loaded, "mini-b", "B1")
    assert (b1["runs_fri"], b1["runs_sat"], b1["runs_sun"]) == (1, 0, 0)
    assert b1["serves_weekend"] == 0
    assert b1["evening_service"] == 0
    assert b1["saturday_first"] is None


def test_route_summary_labels_and_span(loaded):
    a = loaded.conn.execute(
        "SELECT * FROM route_service_summary WHERE feed_id='mini-a' AND route_id='A-1'"
    ).fetchone()
    assert a["service_days"] == "Every day"
    assert (a["runs_weekday"], a["runs_saturday"], a["runs_sunday"]) == (1, 1, 1)
    assert a["first_departure"] == "06:00:00"
    assert a["last_departure"] == "21:45:00"
    assert a["first_stop_name"] in {"Downtown A", "Hub Stop"}

    b = loaded.conn.execute(
        "SELECT * FROM route_service_summary WHERE feed_id='mini-b' AND route_id='B-9'"
    ).fetchone()
    assert b["service_days"] == "Mon–Fri"
    assert (b["runs_saturday"], b["runs_sunday"]) == (0, 0)


def test_synthesize_docs_are_searchable(loaded):
    synthesize_docs(loaded, "mini-a")
    synthesize_docs(loaded, "mini-b")
    hits = search(loaded, "Commuter Express")
    assert hits and any(h.category == "route" for h in hits)

    agency_doc = loaded.conn.execute(
        "SELECT url, title FROM documents WHERE category='agency' AND feed_id='mini-a'"
    ).fetchone()
    assert agency_doc is not None
    assert agency_doc["url"] == "gtfs://mini-a/agency"
