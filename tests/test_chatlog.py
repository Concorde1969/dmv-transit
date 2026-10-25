"""The chat log: anonymous visitor IDs, redaction, retention, export and deletion."""

from __future__ import annotations

import csv
import io
import json

import pytest

from transit_mcp.chatlog import ChatLog, TurnRecord, device_class, redact


@pytest.fixture
def chatlog(tmp_path):
    log = ChatLog(tmp_path / "chatlog.db")
    yield log
    log.close()


def _turn(log, conv="c1", ip="1.2.3.4", question="DCA to IAD?", **kw):
    log.record_turn(TurnRecord(conversation_id=conv, visitor_id=log.visitor_id(ip),
                               question=question, answer=kw.pop("answer", "Blue then Silver"),
                               **kw))


def test_visitor_id_is_stable_keyed_and_not_the_ip(tmp_path, chatlog):
    vid = chatlog.visitor_id("203.0.113.9")
    assert vid == chatlog.visitor_id("203.0.113.9") and len(vid) == 16
    assert vid != chatlog.visitor_id("203.0.113.10")
    assert "203" not in vid
    # the random key is stored once and reused after a restart ...
    again = ChatLog(chatlog.path)
    assert again.visitor_id("203.0.113.9") == vid
    again.close()
    # ... and a different key gives unrelated IDs
    other = ChatLog(tmp_path / "other.db", salt="another-key")
    assert other.visitor_id("203.0.113.9") != vid
    other.close()


@pytest.mark.parametrize("text, expected", [
    ("mail me at jane.doe+bus@example.org", "mail me at [email]"),
    ("call 202-555-0143 or (703) 555 0188", "call [phone] or [phone]"),
    ("my card 4111 1111 1111 1111", "my card [number]"),
    ("SmarTrip 0167 8345 6789 2", "SmarTrip [number]"),
    ("route 38B at 5:30 on 10/24", "route 38B at 5:30 on 10/24"),   # left alone
    ("from Reston to Old Town", "from Reston to Old Town"),
])
def test_redaction(text, expected):
    assert redact(text) == expected


def test_redaction_can_be_turned_off(tmp_path):
    log = ChatLog(tmp_path / "raw.db", redact_text=False)
    _turn(log, question="email a@b.co")
    assert next(log.rows())["question"] == "email a@b.co"
    log.close()


@pytest.mark.parametrize("ua, kind", [
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) Mobile/15E148", "mobile"),
    ("Mozilla/5.0 (Linux; Android 15; Pixel 9) Mobile Safari/537.36", "mobile"),
    ("Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X)", "tablet"),
    ("Mozilla/5.0 (Linux; Android 14; SM-X710) Safari/537.36", "tablet"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/141.0", "desktop"),
    (None, "unknown"),
])
def test_device_class(ua, kind):
    assert device_class(ua) == kind


def test_conversations_count_answered_turns(chatlog):
    _turn(chatlog)
    _turn(chatlog, outcome="minute", answer="")
    _turn(chatlog)
    row = chatlog.conn.execute("SELECT turns FROM conversations WHERE id='c1'").fetchone()
    assert row["turns"] == 2


def test_prune_removes_only_old_turns(chatlog):
    _turn(chatlog, conv="old")
    _turn(chatlog, conv="new")
    chatlog.conn.execute("UPDATE turns SET ts = '2025-01-01 00:00:00' "
                         "WHERE conversation_id = 'old'")
    chatlog.conn.commit()
    assert chatlog.prune(365) == 1
    assert [r["conversation_id"] for r in chatlog.rows()] == ["new"]
    convs = [r[0] for r in chatlog.conn.execute("SELECT id FROM conversations")]
    assert convs == ["new"]
    assert chatlog.prune(0) == 0  # 0 = keep forever


def test_export_csv_and_jsonl(chatlog):
    _turn(chatlog, tools=[{"name": "get_metro_route", "input": {"origin": "DCA"},
                           "is_error": False, "duration_ms": 9}],
          input_tokens=500, output_tokens=80, latency_ms=4200)
    rows = list(csv.DictReader(io.StringIO(chatlog.export_csv())))
    assert len(rows) == 1
    r = rows[0]
    assert r["question"] == "DCA to IAD?" and r["answer"] == "Blue then Silver"
    assert r["tools_used"] == "get_metro_route" and r["seconds"] == "4.2"
    assert r["outcome"] == "ok" and r["input_tokens"] == "500"
    detail = json.loads(chatlog.export_jsonl().splitlines()[0])
    assert detail["tools"][0]["input"] == {"origin": "DCA"}
    assert chatlog.export_csv(since="2999-01-01").strip().count("\n") == 0  # header only


def test_delete_by_conversation_and_visitor(chatlog):
    _turn(chatlog, conv="a", ip="1.1.1.1")
    _turn(chatlog, conv="b", ip="1.1.1.1")
    _turn(chatlog, conv="c", ip="2.2.2.2")
    assert chatlog.delete(conversation="c") == 1
    assert chatlog.delete(visitor=chatlog.visitor_id("1.1.1.1")) == 2
    assert list(chatlog.rows()) == []
    with pytest.raises(ValueError):
        chatlog.delete()


def test_stats(chatlog):
    _turn(chatlog, tools=[{"name": "get_metro_route"}], input_tokens=100, latency_ms=3000)
    _turn(chatlog, conv="c2", ip="9.9.9.9", outcome="site_busy", answer="")
    _turn(chatlog, conv="c3", outcome="error", error="boom", answer="", nudged=True)
    s = chatlog.stats(days=7)
    assert (s["questions"], s["answered"], s["errors"]) == (3, 1, 1)
    assert s["refused"] == {"site_busy": 1}
    assert (s["conversations"], s["visitors"]) == (3, 2)
    assert s["top_tools"] == {"get_metro_route": 1} and s["median_seconds"] == 3.0
    assert s["grounding_nudges"] == 1
