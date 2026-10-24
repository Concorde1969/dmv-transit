"""Rate limits, daily cap, bounded sessions, and the hardened /api/chat endpoint."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from transit_mcp.chat.guard import (
    MESSAGES,
    DailyCap,
    RateLimiter,
    SessionStore,
    trim_history,
)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


# -- units --------------------------------------------------------------------
def test_rate_limiter_minute_and_day_windows():
    clock = Clock()
    rl = RateLimiter(per_minute=3, per_day=5, clock=clock)
    assert [rl.check("a") for _ in range(3)] == [None, None, None]
    assert rl.check("a") == "minute"
    assert rl.check("b") is None  # other visitors unaffected
    clock.t += 61
    assert rl.check("a") is None and rl.check("a") is None
    assert rl.check("a") == "day"  # 5 accepted today
    clock.t += 86_401
    assert rl.check("a") is None


def test_daily_cap_resets_at_midnight():
    day = [datetime(2026, 10, 24, 23, 0)]
    cap = DailyCap(2, now=lambda: day[0])
    assert cap.try_take() and cap.try_take() and not cap.try_take()
    day[0] += timedelta(hours=2)
    assert cap.try_take()


def test_trim_history_never_splits_tool_pairs():
    msgs = []
    for i in range(4):
        msgs += [
            {"role": "user", "content": f"q{i}"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}"}]},
            {"role": "assistant", "content": [{"type": "text", "text": f"a{i}"}]},
        ]
    kept = trim_history(msgs, 2)
    assert kept[0] == {"role": "user", "content": "q2"}
    assert len(kept) == 8
    assert trim_history(msgs, 10) == msgs


def test_session_store_expires_and_caps():
    clock = Clock()
    store = SessionStore(ttl_seconds=60, max_sessions=2, clock=clock)
    a = store.get("a")
    a.messages.append({"role": "user", "content": "hi"})
    assert store.get("a") is a
    store.get("b")
    store.get("c")  # over the cap -> least recently used ("a") dropped
    assert len(store) == 2 and store.get("a").messages == []
    clock.t += 61
    store.get("z")  # everything else idle > ttl
    assert len(store) == 1


# -- endpoint -------------------------------------------------------------------
def _events(resp) -> list[dict]:
    return [json.loads(line[5:]) for line in resp.text.splitlines() if line.startswith("data:")]


@pytest.fixture
def client(isolated_settings, monkeypatch):
    from transit_mcp.chat import app as app_mod

    settings = isolated_settings.model_copy(update={
        "anthropic_api_key": "test-key", "app_env": "production",
        "chat_rate_per_minute": 3, "chat_rate_per_day": 100, "chat_max_turns": 2,
        "chat_daily_limit": 100,
    })
    monkeypatch.setattr(app_mod, "get_settings", lambda: settings)

    async def no_start(self):
        self._tools = []

    async def no_stop(self):
        pass

    monkeypatch.setattr(app_mod.McpBridge, "start", no_start)
    monkeypatch.setattr(app_mod.McpBridge, "stop", no_stop)

    behavior = {"mode": "ok"}

    async def fake_run_chat(*, messages, **kw):
        if behavior["mode"] == "ok":
            messages.append({"role": "assistant", "content": "answer"})
            yield {"type": "text", "text": "answer"}
            yield {"type": "done"}
        elif behavior["mode"] == "error_event":
            messages.append({"role": "assistant",
                             "content": [{"type": "tool_use", "id": "x"}]})
            yield {"type": "error", "message": "APIStatusError: secret internals"}
        else:
            messages.append({"role": "assistant", "content": [{"type": "tool_use"}]})
            raise RuntimeError("database path /data/transit.db exploded")
            yield  # pragma: no cover

    monkeypatch.setattr(app_mod, "run_chat", fake_run_chat)
    with TestClient(app_mod.app) as c:
        c.behavior = behavior
        yield c


def _ask(client, text="hello", sid="s1"):
    return _events(client.post("/api/chat", json={"session_id": sid, "message": text}))


def test_production_health_is_minimal(client):
    assert client.get("/api/health").json() == {"ok": True}


def test_successful_turns_then_turn_cap(client):
    assert any(e.get("text") == "answer" for e in _ask(client))
    assert any(e.get("text") == "answer" for e in _ask(client))
    third = _ask(client)
    assert third[0] == {"type": "error", "message": MESSAGES["max_turns"]}
    assert len(client.app.state.sessions.get("s1").messages) == 4


def test_rate_limit_per_visitor(client):
    for i in range(3):
        _ask(client, sid=f"s{i}")
    assert _ask(client, sid="s9")[0]["message"] == MESSAGES["minute"]


def test_too_long_message_rejected(client):
    ev = _ask(client, text="x" * 1001)
    assert "under 1000 characters" in ev[0]["message"]


@pytest.mark.parametrize("mode", ["error_event", "raise"])
def test_failed_turn_rolls_back_and_hides_internals(client, mode):
    client.behavior["mode"] = mode
    ev = _ask(client, sid="r1")
    errors = [e for e in ev if e["type"] == "error"]
    assert errors and errors[0]["message"] == MESSAGES["failed"]
    assert "secret" not in json.dumps(ev) and "/data" not in json.dumps(ev)
    sess = client.app.state.sessions.get("r1")
    assert sess.messages == [] and sess.turns == 0  # nothing dangling left behind
    client.behavior["mode"] = "ok"
    assert any(e.get("text") == "answer" for e in _ask(client, sid="r1"))


def test_site_wide_daily_cap(client):
    client.app.state.daily_cap.limit = 1
    _ask(client, sid="d1")
    assert _ask(client, sid="d2")[0]["message"] == MESSAGES["site_busy"]


def test_oversized_body_rejected(client):
    resp = client.post("/api/chat", content=b"{" + b" " * 9000 + b"}",
                       headers={"Content-Type": "application/json"})
    assert _events(resp)[0]["type"] == "error"
