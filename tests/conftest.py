from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def _zip_gtfs_dir(dirname: str) -> bytes:
    src = FIXTURES / "gtfs" / dirname
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.glob("*.txt")):
            z.write(p, p.name)
    return buf.getvalue()


@pytest.fixture
def gtfs_zip_bytes():
    """Build an in-memory GTFS zip from tests/fixtures/gtfs/<dirname>/."""
    return _zip_gtfs_dir


@pytest.fixture
def gtfs_feeds_dir(tmp_path):
    """A feeds/ dir containing mini-a and mini-b as local google_transit.zip files."""
    root = tmp_path / "feeds"
    for dirname, slug in (("mini_a", "mini-a"), ("mini_b", "mini-b")):
        d = root / slug
        d.mkdir(parents=True)
        (d / "google_transit.zip").write_bytes(_zip_gtfs_dir(dirname))
    return root


@pytest.fixture
def isolated_settings(tmp_path, monkeypatch):
    """Settings that only see the built-in agency definitions and write all data
    under tmp_path — never the developer's ./agencies, ./data or transit.db."""
    from transit_mcp.config import Settings

    for var, sub in (
        ("AGENCIES_DIR", "no-local-agencies"),
        ("TRANSIT_MCP_DB", "isolated.db"),
        ("GTFS_ARCHIVE_DIR", "archive"),
        ("TRANSIT_MCP_CACHE_DIR", "cache"),
    ):
        monkeypatch.setenv(var, str(tmp_path / sub))
    return Settings(_env_file=None)


@pytest.fixture
def region_db(tmp_db, gtfs_feeds_dir, isolated_settings):
    """mini-a + mini-b loaded, summarized, linked, with curated rows and a Metro
    station ~17 m from stop A1. Shared by the regional / connection-advisor tests."""
    from transit_mcp.agencies import load_agencies, seed_curated_tables
    from transit_mcp.ingest.gtfs.links import build_stop_links
    from transit_mcp.ingest.gtfs.loader import GtfsFeed, load_feed
    from transit_mcp.ingest.gtfs.summarize import summarize_feed

    seed_curated_tables(tmp_db, load_agencies(isolated_settings))
    for slug in ("mini-a", "mini-b"):
        feed = GtfsFeed(slug, f"{slug} Transit", "bus", None)
        load_feed(tmp_db, http=None, feed=feed, feeds_dir=gtfs_feeds_dir)
        summarize_feed(tmp_db, slug)
    tmp_db.conn.execute(
        "INSERT INTO agency_profiles (feed_id, display_name, role, frequency_tier) "
        "VALUES ('mini-a','Mini A Transit','Local grid','frequent'),"
        "       ('mini-b','Mini B Transit','Weekday commuter','peak')"
    )
    tmp_db.conn.execute(
        "INSERT INTO fare_vending (feed_id, base_fare, payment_methods, where_to_buy, "
        "transfer_policy, fare_page_url) VALUES "
        "('mini-a',2.00,'[\"SmarTrip\"]','[\"station\"]','free',"
        "'https://mini-a.example/fares'),"
        "('mini-b',3.50,'[\"Mobile app\"]','[\"app\"]','none',"
        "'https://mini-b.example/fares')"
    )
    tmp_db.conn.execute(
        "INSERT INTO stations (code, name, lat, lon, line_codes, raw_json) "
        "VALUES ('X01','Downtown', 38.9000, -77.0002, '[\"RD\"]', '{}')"
    )
    tmp_db.commit()
    build_stop_links(tmp_db, radius_m=250)
    return tmp_db


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def load_api_fixture():
    def _load(name: str) -> dict:
        return json.loads((FIXTURES / "wmata_api" / f"{name}.json").read_text("utf-8"))

    return _load


@pytest.fixture
def tmp_db(tmp_path):
    from transit_mcp.db.database import Database, init_db

    path = tmp_path / "test.db"
    init_db(path)
    db = Database(path)
    yield db
    db.close()
