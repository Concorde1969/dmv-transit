from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from transit_mcp.agencies import (
    AgencyConfigError,
    discover,
    load_agencies,
    seed_curated_tables,
)
from transit_mcp.config import Settings

TEMPLATE = Path(__file__).resolve().parents[1] / "agencies" / "_template.toml"
BUILTIN = {"wmata", "metrobus", "vre", "fairfax-connector", "art", "dash", "cue",
           "loudoun", "omniride", "virginia-breeze",
           # Maryland and intercity feeds from the Transitland feed list
           "mta-md-bus", "mta-md-commuter-bus", "mta-md-metro", "mta-md-light-rail",
           "marc", "annapolis-transit", "harbor-connector", "potomac-valley",
           "intercity-bus", "flixbus", "amtrak"}


@pytest.fixture
def local_dir(tmp_path, monkeypatch):
    d = tmp_path / "agencies"
    d.mkdir()
    monkeypatch.setenv("AGENCIES_DIR", str(d))
    return d


def _settings():
    return Settings(_env_file=None)


def test_builtin_definitions_load(isolated_settings):
    agencies = load_agencies(isolated_settings)
    assert {a.slug for a in agencies} == BUILTIN
    wmata = next(a for a in agencies if a.slug == "wmata")
    assert [s.type for s in wmata.sources] == ["gtfs", "wmata_api", "web_pages"]
    assert wmata.fares.accepts_smartrip is True
    vre = next(a for a in agencies if a.slug == "vre")
    assert vre.sources_of("gtfs")[0].options()["transitland_id"] == (
        "f-dqc-virginiarailwayexpress")
    # every agency Transitland lists gets its schedules through Transitland
    listed = {a.slug for a in agencies
              if a.sources_of("gtfs") and a.sources_of("gtfs")[0].options().get("transitland_id")}
    assert listed == BUILTIN


def test_template_is_a_valid_agency_when_copied(local_dir):
    shutil.copy(TEMPLATE, local_dir / "example-transit.toml")
    agencies, errors = discover(_settings())
    assert errors == []
    ex = next(a for a in agencies if a.slug == "example-transit")
    assert ex.origin.endswith("example-transit.toml")
    assert {s.type for s in ex.sources} == {"gtfs", "web_pages"}


def test_underscore_files_are_ignored(local_dir):
    shutil.copy(TEMPLATE, local_dir / "_draft.toml")
    assert {a.slug for a in load_agencies(_settings())} == BUILTIN


def test_local_file_overrides_builtin_by_slug(local_dir):
    (local_dir / "dash.toml").write_text(
        'slug = "dash"\nname = "DASH (local copy)"\n'
        '[[sources]]\ntype = "gtfs"\nurl = "https://example.org/dash.zip"\n',
        "utf-8",
    )
    dash = next(a for a in load_agencies(_settings()) if a.slug == "dash")
    assert dash.name == "DASH (local copy)"
    assert dash.origin == str(local_dir / "dash.toml")


def test_disabled_agency_is_skipped(local_dir):
    (local_dir / "cue.toml").write_text(
        'slug = "cue"\nname = "CUE"\nenabled = false\n', "utf-8"
    )
    assert "cue" not in {a.slug for a in load_agencies(_settings())}
    assert "cue" in {a.slug for a in load_agencies(_settings(), include_disabled=True)}


def test_bad_file_reports_file_and_field(local_dir):
    (local_dir / "broken.toml").write_text(
        'slug = "Not A Slug"\nname = "Broken"\nfavorite_color = "red"\n', "utf-8"
    )
    agencies, errors = discover(_settings())
    assert {a.slug for a in agencies} == BUILTIN  # good files still load
    assert len(errors) == 1
    assert "broken.toml" in errors[0]
    assert "slug" in errors[0] and "favorite_color" in errors[0]
    with pytest.raises(AgencyConfigError):
        load_agencies(_settings())


def test_invalid_toml_syntax_is_reported(local_dir):
    (local_dir / "typo.toml").write_text('slug = "typo\n', "utf-8")
    _, errors = discover(_settings())
    assert "typo.toml" in errors[0] and "not valid TOML" in errors[0]


def test_seed_writes_profiles_and_prunes_removed(tmp_db, isolated_settings):
    agencies = load_agencies(isolated_settings)
    tmp_db.conn.execute(
        "INSERT INTO agency_profiles (feed_id, display_name) VALUES ('gone', 'Gone')"
    )
    seed_curated_tables(tmp_db, agencies)
    rows = dict(tmp_db.conn.execute("SELECT feed_id, mode FROM agency_profiles").fetchall())
    assert set(rows) == BUILTIN
    assert rows["vre"] == "commuter_rail"
    fare = tmp_db.conn.execute(
        "SELECT base_fare, fare_page_url FROM fare_vending WHERE feed_id='dash'"
    ).fetchone()
    assert fare["base_fare"] == 0
