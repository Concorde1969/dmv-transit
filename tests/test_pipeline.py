"""The generic ingest pipeline, driven by fake agencies and fake source plugins."""

from __future__ import annotations

import json

import pytest

from transit_mcp.agencies import AgencyConfig
from transit_mcp.db.database import Database, init_db
from transit_mcp.ingest.pipeline import run_ingest
from transit_mcp.ingest.sources import get_source, register_source

CALLS: list[tuple[str, str]] = []


@register_source
class _FakeApi:
    type = "test_fake_api"
    scope = "api"
    updates_network = False

    def run(self, ctx, agency, source):
        CALLS.append((agency.slug, self.type))
        return {"status": "ok", "echo": source.options().get("echo")}


@register_source
class _FakeWeb:
    type = "test_fake_web"
    scope = "web"
    updates_network = False

    def run(self, ctx, agency, source):
        CALLS.append((agency.slug, self.type))
        return {"fetched": 1}


@register_source
class _Exploding:
    type = "test_exploding"
    scope = "api"
    updates_network = False

    def run(self, ctx, agency, source):
        CALLS.append((agency.slug, self.type))
        raise RuntimeError("feed server on fire")


def _agency(slug, *sources):
    return AgencyConfig.model_validate(
        {"slug": slug, "name": slug.title(), "sources": list(sources)}
    )


@pytest.fixture
def setup(isolated_settings):
    CALLS.clear()
    init_db(isolated_settings.db_path)
    agencies = [
        _agency("alpha", {"type": "test_fake_web"}, {"type": "test_fake_api", "echo": 7}),
        _agency("beta", {"type": "test_exploding"}, {"type": "test_fake_web"}),
        _agency("gamma", {"type": "no_such_plugin"},
                {"type": "test_fake_api", "enabled": False}),
    ]
    return isolated_settings, agencies


def test_plugins_are_registered():
    assert get_source("test_fake_api") is not None
    assert get_source("gtfs") is not None and get_source("wmata_api") is not None


def test_all_scope_runs_api_before_web_and_isolates_failures(setup):
    settings, agencies = setup
    result = run_ingest(settings, scope="all", agencies=agencies)

    assert CALLS == [  # api scope first (agency order kept), then web
        ("alpha", "test_fake_api"), ("beta", "test_exploding"),
        ("alpha", "test_fake_web"), ("beta", "test_fake_web"),
    ]
    assert result["agencies"]["alpha"]["test_fake_api"]["echo"] == 7
    assert result["agencies"]["beta"]["test_exploding"]["status"] == "error"
    assert result["agencies"]["beta"]["test_fake_web"] == {"fetched": 1}  # still ran
    errors = " | ".join(result["errors"])
    assert "feed server on fire" in errors
    assert "unknown source type 'no_such_plugin'" in errors

    with Database(settings.db_path) as db:
        run = db.conn.execute("SELECT * FROM ingest_runs ORDER BY id DESC").fetchone()
    assert run["scope"] == "all" and run["status"] == "partial"
    assert json.loads(run["stats_json"])["agencies"]["alpha"]


def test_scope_filter(setup):
    settings, agencies = setup
    run_ingest(settings, scope="web", agencies=agencies)
    assert CALLS == [("alpha", "test_fake_web"), ("beta", "test_fake_web")]


def test_only_filter_and_unknown_slug(setup):
    settings, agencies = setup
    result = run_ingest(settings, scope="all", only=["alpha", "nope"], agencies=agencies)
    assert {slug for slug, _ in CALLS} == {"alpha"}
    assert any("nope: no agency" in e for e in result["errors"])


def test_seeds_profiles_for_all_agencies_even_with_only(setup):
    settings, agencies = setup
    run_ingest(settings, scope="web", only=["alpha"], agencies=agencies)
    with Database(settings.db_path) as db:
        slugs = {r[0] for r in db.conn.execute("SELECT feed_id FROM agency_profiles")}
    assert slugs == {"alpha", "beta", "gamma"}
