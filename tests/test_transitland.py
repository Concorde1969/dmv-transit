"""GTFS feeds come through Transitland: resolution order, auth, and key hygiene."""

from __future__ import annotations

import json

import httpx
import pytest

from transit_mcp.ingest.http import RawResponse
from transit_mcp.ingest.transitland import atlas_file_for, feed_record, fetch_gtfs

ATLAS = {
    "feeds": [
        {"id": "f-dqc-wmata~rail", "spec": "gtfs",
         "urls": {"static_current": "https://api.wmata.com/gtfs/rail-gtfs-static.zip"},
         "authorization": {"type": "header", "param_name": "api_key"}},
        {"id": "f-dqc-virginiarailwayexpress", "spec": "gtfs",
         "urls": {"static_current": "https://gtfs.vre.org/google_transit.zip"}},
        {"id": "f-fairfaxconnector~rt", "spec": "gtfs-rt",
         "urls": {"realtime_alerts": "https://www.fairfaxcounty.gov/gtfsrt/alerts"}},
    ]
}


class FakeHttp:
    """Serves canned responses by URL and records every request's headers."""

    def __init__(self, routes: dict[str, bytes | int]):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    def _answer(self, url: str, headers: dict | None) -> bytes:
        self.calls.append((url, dict(headers or {})))
        body = self.routes.get(url, 404)
        if isinstance(body, int):
            req = httpx.Request("GET", url)
            raise httpx.HTTPStatusError("x", request=req,
                                        response=httpx.Response(body, request=req))
        return body

    def get_bytes(self, url, *, headers=None):
        return 200, self._answer(url, headers), {}

    def get(self, url, *, params=None, conditional=True, headers=None):
        body = self._answer(url, headers)
        return RawResponse(url=url, status_code=200, text=body.decode(), headers={})


def _atlas_url(name: str) -> str:
    return f"https://raw.githubusercontent.com/transitland/transitland-atlas/master/feeds/{name}"


@pytest.fixture
def settings(isolated_settings):
    return isolated_settings.model_copy(update={"wmata_api_key": "WMATA-SECRET",
                                                "transitland_api_key": None})


def test_atlas_file_defaults_to_registrable_domain():
    assert atlas_file_for("https://api.wmata.com/gtfs/x.zip") == "wmata.com.dmfr.json"
    assert atlas_file_for("https://gtfs.vre.org/a.zip") == "vre.org.dmfr.json"
    assert atlas_file_for(None) is None


def test_listed_url_is_used_with_the_auth_transitland_names(settings):
    http = FakeHttp({
        _atlas_url("wmata.com.dmfr.json"): json.dumps(ATLAS).encode(),
        "https://api.wmata.com/gtfs/rail-gtfs-static.zip": b"RAIL-ZIP",
    })
    result, notes = fetch_gtfs(
        slug="wmata", transitland_id="f-dqc-wmata~rail",
        agency_url="https://api.wmata.com/gtfs/rail-gtfs-static.zip",
        auth_env="WMATA_API_KEY", atlas_file=None, settings=settings, http=http,
    )
    assert (result.content, result.via) == (b"RAIL-ZIP", "transitland-listed-url")
    assert result.transitland_id == "f-dqc-wmata~rail"
    feed_call = [h for u, h in http.calls if u.endswith("rail-gtfs-static.zip")][0]
    assert feed_call == {"api_key": "WMATA-SECRET"}
    # the WMATA key never travels to any other host
    assert all("api_key" not in h for u, h in http.calls if "wmata.com" not in u)


TL_VRE = "https://transit.land/api/v2/rest/feeds/f-dqc-virginiarailwayexpress"


def test_listed_url_first_spends_no_api_quota(settings, tmp_db):
    """With a key set and the feed in the atlas, no Transitland REST call is made."""
    from transit_mcp.api_budget import ApiBudget

    s = settings.model_copy(update={"transitland_api_key": "TL-KEY"})
    budget = ApiBudget.transitland(tmp_db, s)
    http = FakeHttp({
        _atlas_url("vre.org.dmfr.json"): json.dumps(ATLAS).encode(),
        "https://gtfs.vre.org/google_transit.zip": b"VRE-ZIP",
        f"{TL_VRE}/download_latest_feed_version": b"TL-COPY",
    })
    result, _ = fetch_gtfs(
        slug="vre", transitland_id="f-dqc-virginiarailwayexpress", agency_url=None,
        auth_env=None, atlas_file="vre.org.dmfr.json", settings=s, http=http, budget=budget,
    )
    assert (result.content, result.via) == (b"VRE-ZIP", "transitland-listed-url")
    assert not any("transit.land" in u for u, _ in http.calls)
    assert budget.used() == 0


def test_hosted_copy_is_the_metered_fallback(settings, tmp_db):
    from transit_mcp.api_budget import ApiBudget

    s = settings.model_copy(update={"transitland_api_key": "TL-KEY"})
    budget = ApiBudget.transitland(tmp_db, s)
    http = FakeHttp({
        _atlas_url("vre.org.dmfr.json"): json.dumps(ATLAS).encode(),
        "https://gtfs.vre.org/google_transit.zip": 503,
        f"{TL_VRE}/download_latest_feed_version": b"TL-COPY",
    })
    result, notes = fetch_gtfs(
        slug="vre", transitland_id="f-dqc-virginiarailwayexpress", agency_url=None,
        auth_env=None, atlas_file="vre.org.dmfr.json", settings=s, http=http, budget=budget,
    )
    assert (result.content, result.via) == (b"TL-COPY", "transitland")
    assert http.calls[-1][1] == {"apikey": "TL-KEY"}
    assert any("HTTP 503" in n for n in notes)
    assert budget.used() == 1


def test_no_metered_calls_once_the_budget_is_spent(settings, tmp_db):
    from transit_mcp.api_budget import ApiBudget

    s = settings.model_copy(update={"transitland_api_key": "TL-KEY",
                                    "transitland_monthly_budget": 0})
    http = FakeHttp({
        _atlas_url("vre.org.dmfr.json"): json.dumps(ATLAS).encode(),
        "https://gtfs.vre.org/google_transit.zip": 503,
        f"{TL_VRE}/download_latest_feed_version": b"TL-COPY",
    })
    with pytest.raises(FileNotFoundError):
        fetch_gtfs(slug="vre", transitland_id="f-dqc-virginiarailwayexpress",
                   agency_url=None, auth_env=None, atlas_file="vre.org.dmfr.json",
                   settings=s, http=http, budget=ApiBudget.transitland(tmp_db, s))
    assert not any("transit.land" in u for u, _ in http.calls)


def test_rest_record_is_only_a_fallback_for_missing_atlas_entries(settings, tmp_db):
    from transit_mcp.api_budget import ApiBudget

    s = settings.model_copy(update={"transitland_api_key": "TL-KEY"})
    budget = ApiBudget.transitland(tmp_db, s)
    http = FakeHttp({
        _atlas_url("example.org.dmfr.json"): b'{"feeds": []}',
        TL_VRE: json.dumps({"feeds": [ATLAS["feeds"][1]]}).encode(),
        "https://gtfs.vre.org/google_transit.zip": b"VRE-ZIP",
    })
    result, _ = fetch_gtfs(
        slug="vre", transitland_id="f-dqc-virginiarailwayexpress", agency_url=None,
        auth_env=None, atlas_file="example.org.dmfr.json", settings=s, http=http,
        budget=budget,
    )
    assert result.via == "transitland-listed-url" and budget.used() == 1


def test_agency_url_then_local_file_are_last_resorts(settings, tmp_path):
    http = FakeHttp({_atlas_url("example.org.dmfr.json"): b'{"feeds": []}',
                     "https://example.org/gtfs.zip": 503})
    local = settings.feeds_dir / "ex"
    local.mkdir(parents=True)
    (local / "google_transit.zip").write_bytes(b"LOCAL")
    result, notes = fetch_gtfs(
        slug="ex", transitland_id="f-missing", agency_url="https://example.org/gtfs.zip",
        auth_env=None, atlas_file=None, settings=settings, http=http,
    )
    assert (result.content, result.via) == (b"LOCAL", "local file")
    assert any("not found" in n for n in notes)


def test_missing_key_for_an_authorized_feed_is_reported(settings):
    s = settings.model_copy(update={"wmata_api_key": None})
    http = FakeHttp({_atlas_url("wmata.com.dmfr.json"): json.dumps(ATLAS).encode()})
    with pytest.raises(FileNotFoundError, match="WMATA_API_KEY"):
        fetch_gtfs(slug="wmata-x", transitland_id="f-dqc-wmata~rail", agency_url=None,
                   auth_env="WMATA_API_KEY", atlas_file="wmata.com.dmfr.json",
                   settings=s, http=http)


def test_feed_record_reads_realtime_alert_urls(settings):
    http = FakeHttp({_atlas_url("fairfaxcounty.gov.dmfr.json"): json.dumps(ATLAS).encode()})
    rec = feed_record("f-fairfaxconnector~rt", settings=settings, http=http,
                      atlas_file="fairfaxcounty.gov.dmfr.json")
    assert rec.realtime_alerts == "https://www.fairfaxcounty.gov/gtfsrt/alerts"


def test_wmata_api_key_is_sent_per_request_not_on_the_shared_client():
    from transit_mcp.ingest.wmata_api import WmataApiClient

    class Shared(FakeHttp):
        def __init__(self):
            super().__init__({})
            self._client = httpx.Client()

        def get(self, url, *, params=None, conditional=True, headers=None):
            self.calls.append((url, dict(headers or {})))
            return RawResponse(url=url, status_code=200, text='{"Lines": []}', headers={})

    http = Shared()
    WmataApiClient(http, "WMATA-SECRET").lines()
    assert http.calls[0][1] == {"api_key": "WMATA-SECRET"}
    assert "api_key" not in http._client.headers
