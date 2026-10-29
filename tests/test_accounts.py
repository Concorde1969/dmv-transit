"""Accounts: email-code sign-in, sessions and saved places (accounts.py, account_api.py)."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from transit_mcp.accounts import AccountStore, PlaceError, PlaceInput, normalize_email
from transit_mcp.mailer import code_email


def _raw(path):
    """A SQLite file's bytes, with its write-ahead log."""
    wal = path.with_name(path.name + "-wal")
    return path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")


HOTEL = {"name": "My hotel", "kind": "hotel", "lat": 38.9026, "lon": -77.0365,
         "address": "Capital Hilton, 1001 16th St NW, Washington",
         "start_date": "2026-11-01", "end_date": "2026-11-05"}


class Clock:
    def __init__(self):
        self.t = 1_800_000_000.0

    def __call__(self):
        return self.t


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    s = AccountStore(tmp_path / "accounts.db", clock=clock, max_places=3)
    s.clock = clock
    yield s
    s.close()


def test_email_addresses_are_checked_and_lower_cased():
    assert normalize_email("  Visitor@Example.COM ") == "visitor@example.com"
    for bad in ("", "no-at-sign", "a@b", "two@@example.com", "x" * 250 + "@example.com",
                "a b@example.com"):
        assert normalize_email(bad) is None


def test_code_works_once_and_expires(store):
    code = store.issue_code("v@example.com")
    assert len(code) == 6 and code.isdigit()
    assert not store.check_code("v@example.com", "000000" if code != "000000" else "111111")
    assert store.check_code("v@example.com", f"{code[:3]} {code[3:]}")   # spaces are fine
    assert not store.check_code("v@example.com", code)                  # used up
    code = store.issue_code("v@example.com")
    store.clock.t += 11 * 60
    assert not store.check_code("v@example.com", code)                  # expired


def test_five_wrong_tries_end_a_code(store):
    code = store.issue_code("v@example.com")
    wrong = "123456" if code != "123456" else "654321"
    for _ in range(5):
        assert not store.check_code("v@example.com", wrong)
    assert not store.check_code("v@example.com", code)
    new = store.issue_code("v@example.com")      # a new code starts over
    assert store.check_code("v@example.com", new)


def test_only_hashes_of_codes_and_tokens_are_stored(store):
    code = store.issue_code("v@example.com")
    _, token, created = store.sign_in("v@example.com")
    assert created
    raw = _raw(store.path)
    assert token.encode() not in raw and f"'{code}'".encode() not in raw


def test_sessions_last_90_days_and_end_on_sign_out(store):
    user, token, _ = store.sign_in("v@example.com")
    assert store.user_for_token(token) == user
    assert store.user_for_token("not-a-token") is None
    _, again, created = store.sign_in("v@example.com")
    assert not created                                   # same account, new session
    store.sign_out(token)
    assert store.user_for_token(token) is None and store.user_for_token(again) == user
    store.clock.t += 91 * 86400
    assert store.user_for_token(again) is None


def test_places_belong_to_their_owner(store):
    a, _, _ = store.sign_in("a@example.com")
    b, _, _ = store.sign_in("b@example.com")
    p = store.add_place(a["id"], PlaceInput(**{**HOTEL, "start_date": date(2026, 11, 1),
                                                "end_date": date(2026, 11, 5)}))
    assert p["start_date"] == "2026-11-01" and p["end_date"] == "2026-11-05"
    assert store.places(b["id"]) == []
    assert store.update_place(b["id"], p["id"], PlaceInput("x", "other", 38.9, -77.0)) is None
    assert not store.delete_place(b["id"], p["id"])
    changed = store.update_place(a["id"], p["id"], PlaceInput("Museum", "attraction", 38.89,
                                                              -77.02, None, date(2026, 11, 2),
                                                              date(2026, 11, 9)))
    assert changed["kind"] == "attraction" and changed["end_date"] is None   # visits: one day
    assert store.delete_place(a["id"], p["id"]) and store.places(a["id"]) == []


def test_place_rules(store):
    u, _, _ = store.sign_in("a@example.com")
    with pytest.raises(PlaceError):
        store.add_place(u["id"], PlaceInput("  ", "hotel", 38.9, -77.0))
    with pytest.raises(PlaceError):
        store.add_place(u["id"], PlaceInput("X", "castle", 38.9, -77.0))
    with pytest.raises(PlaceError, match="check-out"):
        store.add_place(u["id"], PlaceInput("X", "hotel", 38.9, -77.0, None,
                                            date(2026, 11, 5), date(2026, 11, 1)))
    for i in range(3):
        store.add_place(u["id"], PlaceInput(f"P{i}", "other", 38.9, -77.0))
    with pytest.raises(PlaceError, match="up to 3"):
        store.add_place(u["id"], PlaceInput("P4", "other", 38.9, -77.0))


def test_deleting_an_account_removes_everything(store):
    u, token, _ = store.sign_in("a@example.com")
    store.add_place(u["id"], PlaceInput("Hotel", "hotel", 38.9, -77.0))
    store.delete_user(u["id"])
    assert store.user_for_token(token) is None and store.places(u["id"]) == []
    assert store.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0


def test_prune_removes_unused_accounts(store):
    old, _, _ = store.sign_in("old@example.com")
    store.add_place(old["id"], PlaceInput("Hotel", "hotel", 38.9, -77.0))
    store.conn.execute("UPDATE users SET last_seen = '2020-01-01' WHERE id = ?", (old["id"],))
    store.conn.commit()
    store.sign_in("new@example.com")
    assert store.prune(730) == 1
    assert [r[0] for r in store.conn.execute("SELECT email FROM users")] == ["new@example.com"]
    assert store.places(old["id"]) == []


def test_code_email_has_the_code():
    subject, text, html = code_email("042917", "dmvtransit.com")
    assert "042917" in subject and "042917" in text and "042917" in html


# ----------------------------------------------------------------------------- API
class FakeMailer:
    available = True

    def __init__(self):
        self.sent = []

    def send(self, to, subject, text, html=None):
        self.sent.append((to, subject, text))

    def last_code(self):
        return self.sent[-1][1].rsplit(" ", 1)[-1]


@pytest.fixture
def client(isolated_settings, monkeypatch, tmp_path):
    from transit_mcp.chat import app as app_mod

    settings = isolated_settings.model_copy(update={
        "anthropic_api_key": "test-key", "app_env": "production",
        "accounts_db": tmp_path / "accounts.db", "smtp_host": "smtp.test"})
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    async def no_start(self):
        self._tools = []

    async def no_stop(self):
        pass

    monkeypatch.setattr(app_mod.McpBridge, "start", no_start)
    monkeypatch.setattr(app_mod.McpBridge, "stop", no_stop)
    with TestClient(app_mod.app, base_url="https://testserver") as c:
        c.mailer = c.app.state.accounts.mailer = FakeMailer()
        yield c


SITE = {"Origin": "https://testserver"}


def _sign_in(client, email="Visitor@Example.com"):
    r = client.post("/api/account/code", json={"email": email}, headers=SITE)
    assert r.status_code == 200, r.text
    code = client.mailer.last_code()
    return client.post("/api/account/verify", json={"email": email, "code": code},
                       headers=SITE)


def test_sign_in_flow_sets_a_safe_cookie(client):
    assert client.get("/api/account").json() == {"signed_in": False, "enabled": True}
    r = _sign_in(client)
    assert r.status_code == 200 and r.json()["created"] is True
    assert r.json()["email"] == "visitor@example.com"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    me = client.get("/api/account").json()
    assert me["signed_in"] and me["places"] == []
    assert client.mailer.sent[0][0] == "visitor@example.com"


def test_wrong_code_and_other_sites_are_refused(client):
    client.post("/api/account/code", json={"email": "v@example.com"}, headers=SITE)
    code = client.mailer.last_code()
    wrong = "000000" if code != "000000" else "111111"
    r = client.post("/api/account/verify", json={"email": "v@example.com", "code": wrong},
                    headers=SITE)
    assert r.status_code == 400 and "expired" in r.json()["message"]
    r = client.post("/api/account/verify", json={"email": "v@example.com", "code": code},
                    headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert client.post("/api/account/code", json={"email": "nope"},
                       headers=SITE).status_code == 422


def test_code_emails_are_rate_limited(client):
    for _ in range(2):
        assert client.post("/api/account/code", json={"email": "v@example.com"},
                           headers=SITE).status_code == 200
    r = client.post("/api/account/code", json={"email": "v@example.com"}, headers=SITE)
    assert r.status_code == 429 and len(client.mailer.sent) == 2


def test_saving_changing_and_deleting_places(client):
    _sign_in(client)
    r = client.post("/api/account/places", json=HOTEL, headers=SITE)
    assert r.status_code == 201, r.text
    place = r.json()["place"]
    assert place["name"] == "My hotel" and place["end_date"] == "2026-11-05"
    r = client.put(f"/api/account/places/{place['id']}",
                   json={**HOTEL, "name": "Hilton"}, headers=SITE)
    assert r.json()["place"]["name"] == "Hilton"
    far = client.post("/api/account/places", json={**HOTEL, "lat": 40.71, "lon": -74.0},
                      headers=SITE)
    assert far.status_code == 422 and "outside" in far.json()["message"]
    bad = client.post("/api/account/places", json={**HOTEL, "end_date": "2026-10-01"},
                      headers=SITE)
    assert bad.status_code == 422
    r = client.delete(f"/api/account/places/{place['id']}", headers=SITE)
    assert r.json()["places"] == []


def test_places_need_a_signed_in_visitor(client):
    assert client.post("/api/account/places", json=HOTEL, headers=SITE).status_code == 401
    _sign_in(client)
    assert client.post("/api/account/places", json=HOTEL,
                       headers={"Origin": "https://evil.example"}).status_code == 401


def test_sign_out_and_delete_account(client):
    client.app.state.accounts.email_limiter.check = lambda key: None   # three sign-ins
    _sign_in(client)
    client.post("/api/account/places", json=HOTEL, headers=SITE)
    assert client.post("/api/account/signout", headers=SITE).json()["signed_in"] is False
    assert client.get("/api/account").json()["signed_in"] is False
    _sign_in(client)
    assert client.get("/api/account").json()["places"][0]["name"] == "My hotel"  # kept
    r = client.delete("/api/account", headers=SITE)
    assert r.status_code == 200
    assert client.get("/api/account").json()["signed_in"] is False
    _sign_in(client)
    assert client.get("/api/account").json()["places"] == []                    # gone


def test_usage_log_never_holds_the_email(client):
    _sign_in(client, "private.person@example.com")
    client.post("/api/account/places", json=HOTEL, headers=SITE)
    log = client.app.state.chatlog
    events = [r["event"] for r in log.table_rows("events")]
    assert {"account_code_sent", "account_signed_in", "place_saved"} <= set(events)
    raw = _raw(log.path)
    assert b"private.person" not in raw and b"Capital Hilton" not in raw


def test_accounts_unavailable_without_mail_in_production(client):
    client.app.state.accounts.mailer.available = False
    r = client.post("/api/account/code", json={"email": "v@example.com"}, headers=SITE)
    assert r.status_code == 503


def test_mailer_needs_its_password_before_accounts_show():
    from transit_mcp.mailer import Mailer

    kw = {"host": "email-smtp.us-east-2.amazonaws.com", "sender": "DMV <a@dmvtransit.com>"}
    assert not Mailer(**kw).available                          # host only (template default)
    assert not Mailer(**kw, username="AKIA").available         # no password
    assert Mailer(**kw, username="AKIA", password="pw").available
    assert Mailer(host=None, log_only=True).available          # development: code in the log
    assert not Mailer(host=None).available
