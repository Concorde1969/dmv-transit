"""Rider reports: storing, checking with the model (faked), matching trips, the API."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from transit_mcp.reports import (
    ParsedReport,
    ReportInput,
    ReportStore,
    matches_option,
    process_pending,
)

TODAY = date.today()


def _report(text="Red Line single-tracking at Union Station, 20 minute waits. "
                 "Call me at 202-555-0101", **over):
    base = dict(travel_date=TODAY, station="Union Station", line="Red Line",
                origin="Dupont Circle", destination="Union Station", text=text)
    base.update(over)
    return ReportInput(**base)


def _parsed(**over):
    base = dict(publish=True, reason="", category="delay", severity="major",
                summary="Red Line single-tracking at Union Station; about 20-minute waits.",
                agencies=["wmata"], lines=["Red Line"], stations=["Union Station"])
    base.update(over)
    return ParsedReport(**base)


class FakeClient:
    """Stands in for AsyncAnthropic: messages.parse returns a prepared ParsedReport."""

    def __init__(self, result=None, error=None):
        self.calls = []
        self.messages = SimpleNamespace(parse=self._parse)
        self.result, self.error = result, error

    async def _parse(self, **kw):
        self.calls.append(kw)
        if self.error:
            raise self.error
        return SimpleNamespace(parsed_output=self.result, stop_reason="end_turn")


@pytest.fixture
def store(tmp_path):
    return ReportStore(tmp_path / "t.db")


def test_add_removes_contact_details_and_waits_for_review(store):
    rid = store.add("visitor-1", _report())
    (row,) = store.pending()
    assert row["id"] == rid and "555" not in row["text"] and "[phone]" in row["text"]
    assert store.recent() == []                      # not shown until it has been checked


def test_checked_report_is_published_and_searchable(store):
    store.add("v", _report())
    client = FakeClient(_parsed(agencies=["wmata", "made-up"]))
    n = asyncio.run(process_pending(store, client, "claude-haiku-5-5",
                                    {"wmata": "Metrorail (WMATA)"}))
    assert n == 1 and store.pending() == []
    (r,) = store.recent()
    assert r["category"] == "delay" and r["agencies"] == ["wmata"]   # unknown slug dropped
    assert store.search(station="union stn")[0]["id"] == r["id"]
    assert store.search(line="Red")[0]["id"] == r["id"]
    assert store.search(line="Blue Line") == []
    # the report text reaches the model only as data, with the agency list
    content = client.calls[0]["messages"][0]["content"]
    assert "<report_text>" in content and "wmata: Metrorail (WMATA)" in content
    assert client.calls[0]["model"] == "claude-haiku-5-5"


def test_rejected_report_is_hidden(store):
    store.add("v", _report(text="Buy cheap watches at example dot com, best prices!!"))
    asyncio.run(process_pending(store, FakeClient(_parsed(publish=False, reason="spam")),
                                "m", {}))
    assert store.recent() == [] and store.list_all()[0]["status"] == "hidden"


def test_failed_check_stays_pending_for_retry(store):
    store.add("v", _report())
    n = asyncio.run(process_pending(store, FakeClient(error=RuntimeError("API down")), "m", {}))
    assert n == 0 and len(store.pending()) == 1


def test_old_travel_dates_are_not_shown_as_current(store):
    store.add("v", _report(travel_date=TODAY - timedelta(days=3)))
    asyncio.run(process_pending(store, FakeClient(_parsed()), "m", {}))
    assert store.recent() == []                      # still searchable by the assistant
    assert len(store.search(days=7)) == 1


def test_prune(store):
    store.add("v", _report())
    with store._connect() as conn:
        conn.execute("UPDATE rider_reports SET created_at = '2020-01-01T00:00:00'")
    assert store.prune(365) == 1


def _option(route_short, route_long, board, alight, agency="wmata"):
    return {"legs": [{"mode": "walk"},
                     {"mode": "ride", "agency": agency, "route_short_name": route_short,
                      "route_long_name": route_long, "board": {"name": board},
                      "alight": {"name": alight}},
                     {"mode": "walk"}]}


def test_matching_reports_to_trip_options():
    r = {"agencies": ["wmata"], "lines": ["Red Line"], "stations": ["Union Station"]}
    assert matches_option(r, _option("R", "Red", "Dupont Circle", "Farragut North"))
    assert matches_option(r, _option("B", "Blue", "Union Station", "Rosslyn",
                                     agency="vre"))      # the station alone is enough
    assert not matches_option(r, _option("B", "Blue", "Rosslyn", "Pentagon"))
    assert not matches_option(r, _option("B", "Blue", "Union Market", "Rosslyn"))
    bus = {"agencies": [], "lines": ["Metrobus D80"], "stations": []}
    assert matches_option(bus, _option("D80", None, "A", "B", agency="metrobus"))


def test_attach_adds_reports_to_matching_options(store):
    store.add("v", _report())
    asyncio.run(process_pending(store, FakeClient(_parsed()), "m", {}))
    options = [_option("R", "Red", "Dupont Circle", "Union Station"),
               _option("OR", "Orange", "Rosslyn", "Ballston")]
    store.attach(options)
    assert len(options[0]["reports"]) == 1 and options[1]["reports"] == []
    assert set(options[0]["reports"][0]) == {"id", "minutes_ago", "category", "severity",
                                             "summary", "station", "line"}


# -- the web API ------------------------------------------------------------------------
@pytest.fixture
def client(isolated_settings, monkeypatch):
    from transit_mcp.chat import app as app_mod
    from transit_mcp.chat import reports_api

    settings = isolated_settings.model_copy(update={
        "anthropic_api_key": "test-key", "app_env": "production",
        "reports_rate_per_minute": 3})
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    async def no_start(self):
        self._tools = []

    async def no_stop(self):
        pass

    monkeypatch.setattr(app_mod.McpBridge, "start", no_start)
    monkeypatch.setattr(app_mod.McpBridge, "stop", no_stop)
    fake = FakeClient(_parsed())

    async def fake_process(app):
        await process_pending(app.state.reports, fake, "m", app.state.report_agencies)

    monkeypatch.setattr(reports_api, "process_reports", fake_process)
    monkeypatch.setattr(app_mod, "process_reports", fake_process)
    with TestClient(app_mod.app) as c:
        yield c


def _form(**over):
    body = {"station": "Union Station", "line": "Red Line", "origin": "Dupont Circle",
            "destination": "Union Station", "travel_date": TODAY.isoformat(),
            "text": "Red Line single-tracking at Union Station, 20 minute waits."}
    body.update(over)
    return body


def test_submit_then_see_it_in_recent(client):
    r = client.post("/api/reports", json=_form())
    assert r.status_code == 202 and r.json()["ok"]
    recent = client.get("/api/reports/recent").json()
    (item,) = recent["reports"]
    assert item["category"] == "delay" and "visitor_id" not in item and "text" not in item


def test_submit_validation(client):
    future = (TODAY + timedelta(days=2)).isoformat()
    assert client.post("/api/reports", json=_form(travel_date=future)).status_code == 422
    assert client.post("/api/reports", json=_form(text="late")).status_code == 422
    assert client.post("/api/reports", json=_form(origin="")).status_code == 422
    station_optional = _form()
    del station_optional["station"], station_optional["line"]
    assert client.post("/api/reports", json=station_optional).status_code == 202


def test_submit_rate_limit(client):
    for _ in range(3):
        client.post("/api/reports", json=_form())
    r = client.post("/api/reports", json=_form())
    assert r.status_code == 429 and "several reports" in r.json()["message"]
