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


def test_transitland_copy_is_preferred_when_a_key_is_set(settings):
    s = settings.model_copy(update={"transitland_api_key": "TL-KEY"})
    tl = "https://transit.land/api/v2/rest/feeds/f-dqc-virginiarailwayexpress"
    http = FakeHttp({
        tl: json.dumps({"feeds": [ATLAS["feeds"][1]]}).encode(),
        f"{tl}/download_latest_feed_version": b"TL-COPY",
    })
    result, _ = fetch_gtfs(
        slug="vre", transitland_id="f-dqc-virginiarailwayexpress", agency_url=None,
        auth_env=None, atlas_file=None, settings=s, http=http,
    )
    assert (result.content, result.via) == (b"TL-COPY", "transitland")
    assert http.calls[-1][1] == {"apikey": "TL-KEY"}


def test_falls_back_to_listed_url_when_redistribution_is_refused(settings):
    s = settings.model_copy(update={"transitland_api_key": "TL-KEY"})
    tl = "https://transit.land/api/v2/rest/feeds/f-dqc-virginiarailwayexpress"
    http = FakeHttp({
        tl: json.dumps({"feeds": [ATLAS["feeds"][1]]}).encode(),
        f"{tl}/download_latest_feed_version": 401,
        "https://gtfs.vre.org/google_transit.zip": b"VRE-ZIP",
    })
    result, notes = fetch_gtfs(
        slug="vre", transitland_id="f-dqc-virginiarailwayexpress", agency_url=None,
        auth_env=None, atlas_file="vre.org.dmfr.json", settings=s, http=http,
    )
    assert result.via == "transitland-listed-url"
    assert any("HTTP 401" in n for n in notes)


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
