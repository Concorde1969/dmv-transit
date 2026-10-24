"""Unit tests for source extraction from MCP tool output (chat trace panel)."""

from __future__ import annotations

import json

from transit_mcp.chat.agent import extract_sources


def test_extracts_from_search_style_list():
    payload = json.dumps(
        [
            {"title": "Smart Trip", "url": "https://www.wmata.com/pay/smart-trip.html",
             "retrieved_at": "2026-08-29 15:33:28", "stale": False},
            {"title": "New Riders", "url": "https://www.wmata.com/ride/guides/new-riders.html",
             "retrieved_at": "2026-08-29 15:33:36", "stale": False},
        ]
    )
    srcs = extract_sources(payload)
    urls = {s["url"] for s in srcs}
    assert urls == {
        "https://www.wmata.com/pay/smart-trip.html",
        "https://www.wmata.com/ride/guides/new-riders.html",
    }
    assert all(s["retrieved_at"] for s in srcs)


def test_extracts_from_nested_context_and_dedupes():
    payload = json.dumps(
        {
            "origin": "Shady Grove",
            "destination": "Metro Center",
            "peak_fare": 6.75,
            "stale": True,
            "context": [
                {"title": "Fares", "url": "https://www.wmata.com/pay.html",
                 "retrieved_at": "2026-08-29 15:33:27"},
                {"title": "Fares again", "url": "https://www.wmata.com/pay.html",
                 "retrieved_at": "2026-08-29 15:33:27"},
            ],
        }
    )
    srcs = extract_sources(payload)
    assert len(srcs) == 1
    assert srcs[0]["url"] == "https://www.wmata.com/pay.html"


def test_stale_flag_is_captured():
    payload = json.dumps({"url": "wmata://station/A01", "stale": True})
    srcs = extract_sources(payload)
    assert srcs == [{"url": "wmata://station/A01", "retrieved_at": None, "stale": True}]


def test_non_json_returns_empty():
    assert extract_sources("not json at all") == []
    assert extract_sources("") == []
