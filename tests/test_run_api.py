"""Exercise the run_api orchestration with a fake WMATA client (no network)."""

from __future__ import annotations

import json

import pytest

from transit_mcp.config import Settings
from transit_mcp.db.database import Database


class _FakeRaw:
    def __init__(self, data: dict):
        self.text = json.dumps(data)


class _FakeClient:
    def __init__(self, fixtures):
        self._f = fixtures

    def _pair(self, name):
        data = self._f(name)
        return _FakeRaw(data), data

    def lines(self):
        return self._pair("lines")

    def station_parking(self, station_code=None):
        return self._pair("station_parking")

    def station_entrances(self):
        return self._pair("station_entrances")

    def stations(self):
        return self._pair("stations")

    def rail_incidents(self):
        return self._pair("rail_incidents")

    def bus_incidents(self):
        return self._pair("bus_incidents")

    def elevator_incidents(self):
        return self._pair("elevator_incidents")

    def station_to_station(self, from_code, to_code=None):
        return self._pair("station_to_station")


@pytest.fixture
def api_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("WMATA_API_KEY", "test-key")
    monkeypatch.setenv("TRANSIT_MCP_DB", str(tmp_path / "api.db"))
    monkeypatch.setenv("AGENCIES_DIR", str(tmp_path / "no-local-agencies"))
    monkeypatch.setenv("GTFS_ARCHIVE_DIR", str(tmp_path / "archive"))
    return Settings(_env_file=None)


def test_run_api_populates_all_tables(api_settings, load_api_fixture, monkeypatch):
    from transit_mcp.db.database import init_db
    from transit_mcp.ingest import run as run_mod
    from transit_mcp.ingest.sources import wmata_api as plugin

    init_db(api_settings.db_path)
    monkeypatch.setattr(
        plugin, "WmataApiClient", lambda http, key: _FakeClient(load_api_fixture)
    )

    result = run_mod.run_api(api_settings, include_fares=True)
    stats = result["agencies"]["wmata"]["wmata_api"]

    assert stats["lines"] == 3
    assert stats["stations"] == 3
    assert stats["rail_alerts"] == 1
    assert stats["bus_alerts"] == 1
    assert stats["units_out"] == 2
    assert stats["fare_rows"] >= 2

    with Database(api_settings.db_path) as db:
        counts = db.table_counts()
        assert counts["lines"] == 3
        assert counts["stations"] == 3
        assert counts["alerts_active"] >= 3  # rail + bus + elevator roll-up
        assert counts["unit_status_active"] == 2
        assert counts["fares"] >= 2
        # Synthesized station/line docs are searchable.
        row = db.conn.execute(
            "SELECT count(*) n FROM documents WHERE category = 'station'"
        ).fetchone()
        assert row["n"] == 3


def test_run_api_reconciles_resolved_alerts(api_settings, load_api_fixture, monkeypatch):
    from transit_mcp.db.database import init_db
    from transit_mcp.ingest import run as run_mod
    from transit_mcp.ingest.sources import wmata_api as plugin

    init_db(api_settings.db_path)

    monkeypatch.setattr(
        plugin, "WmataApiClient", lambda http, key: _FakeClient(load_api_fixture)
    )
    run_mod.run_api(api_settings, include_fares=False)

    # Second run where rail incidents have cleared.
    def empty_rail(fixtures):
        c = _FakeClient(fixtures)
        c.rail_incidents = lambda: (_FakeRaw({"Incidents": []}), {"Incidents": []})
        return c

    monkeypatch.setattr(
        plugin, "WmataApiClient", lambda http, key: empty_rail(load_api_fixture)
    )
    run_mod.run_api(api_settings, include_fares=False)

    with Database(api_settings.db_path) as db:
        active_rail = db.conn.execute(
            "SELECT count(*) n FROM alerts WHERE mode='rail' AND resolved_at IS NULL"
        ).fetchone()["n"]
        resolved_rail = db.conn.execute(
            "SELECT count(*) n FROM alerts WHERE mode='rail' AND resolved_at IS NOT NULL"
        ).fetchone()["n"]
    assert active_rail == 0
    assert resolved_rail == 1
