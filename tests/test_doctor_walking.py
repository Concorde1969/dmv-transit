"""The health report's walking-directions check, against a fake openrouteservice."""

from __future__ import annotations

import httpx

from transit_mcp.config import Settings
from transit_mcp.doctor import FAIL, OK, WARN, check_walking

ROUTE = {"features": [{
    "geometry": {"coordinates": [[-77.02815, 38.89838], [-77.0256, 38.8982],
                                 [-77.02297, 38.89784]]},
    "properties": {"summary": {"distance": 452.3},
                   "segments": [{"steps": [
                       {"instruction": "Head east on G Street Northwest", "distance": 452.3,
                        "type": 11, "name": "G Street Northwest", "way_points": [0, 2]},
                       {"instruction": "Arrive", "distance": 0, "type": 10, "name": "-",
                        "way_points": [2, 2]}]}]},
}]}


def _settings(key="test-key"):
    return Settings(_env_file=None, ORS_API_KEY=key)


def _client(status, body=None, headers=None, raise_exc=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if raise_exc:
            raise raise_exc
        assert request.headers["Authorization"] == "test-key"
        return httpx.Response(status, json=body if body is not None else {},
                              headers=headers or {})
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_working_walks_report_ok_with_the_quota_left():
    c = check_walking(_settings(), _client(200, ROUTE, {"x-ratelimit-remaining": "1873"}))
    assert c.status == OK and "452 m" in c.message and "1873 requests left" in c.message


def test_no_key_is_a_warning():
    assert check_walking(_settings(key=None)).status == WARN


def test_a_moved_service_fails_and_says_so():
    c = check_walking(_settings(), _client(410))
    assert c.status == FAIL and "moved" in c.message and "ORS_URL" in c.fix


def test_a_rejected_key_fails():
    c = check_walking(_settings(), _client(403))
    assert c.status == FAIL and "rejected the key" in c.message


def test_a_spent_quota_and_an_outage_are_warnings():
    assert check_walking(_settings(), _client(429)).status == WARN
    down = _client(0, raise_exc=httpx.ConnectError("no route to host"))
    assert check_walking(_settings(), down).status == WARN


def test_an_unexpected_answer_fails():
    assert check_walking(_settings(), _client(200, {"nope": 1})).status == FAIL
