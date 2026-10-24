"""Offline test of the run_chat agent loop with a fake Anthropic client + bridge."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from transit_mcp.chat.agent import run_chat


class _FakeStream:
    def __init__(self, text_chunks, final_message):
        self._chunks = text_chunks
        self._final = final_message

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def __aiter__(self):
        for t in self._chunks:
            yield SimpleNamespace(
                type="content_block_delta",
                delta=SimpleNamespace(type="text_delta", text=t),
            )

    async def get_final_message(self):
        return self._final


class _FakeMessages:
    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls = []

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return self._scripted.pop(0)


class _FakeClient:
    def __init__(self, scripted):
        self.messages = _FakeMessages(scripted)


class _FakeBridge:
    anthropic_tools = [{"name": "get_fare", "description": "", "input_schema": {}}]

    def __init__(self):
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return (
            json.dumps(
                {
                    "origin": "Shady Grove",
                    "destination": "Metro Center",
                    "peak_fare": 6.75,
                    "stale": False,
                    "context": [{"url": "https://www.wmata.com/pay.html",
                                 "retrieved_at": "2026-08-29 15:33:27"}],
                }
            ),
            False,
        )


def _tool_use_block(tid, name, inp):
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=inp)


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def test_run_chat_executes_tool_then_answers():
    asyncio.run(_run_chat_executes_tool_then_answers())


async def _run_chat_executes_tool_then_answers():
    round1_final = SimpleNamespace(
        content=[
            _text_block("Let me check that. "),
            _tool_use_block("tu_1", "get_fare",
                            {"origin": "Shady Grove", "destination": "Metro Center"}),
        ],
        stop_reason="tool_use",
    )
    round2_final = SimpleNamespace(
        content=[_text_block("Peak fare is $6.75.")],
        stop_reason="end_turn",
    )
    client = _FakeClient(
        [
            _FakeStream(["Let me check that. "], round1_final),
            _FakeStream(["Peak fare ", "is $6.75."], round2_final),
        ]
    )
    bridge = _FakeBridge()
    messages = [{"role": "user", "content": "fare from shady grove to metro center?"}]

    events = [
        e
        async for e in run_chat(
            client=client, bridge=bridge, model="claude-sonnet-5",
            messages=messages, max_tokens=512, max_iterations=6,
        )
    ]
    kinds = [e["type"] for e in events]

    assert bridge.calls == [
        ("get_fare", {"origin": "Shady Grove", "destination": "Metro Center"})
    ]
    assert "tool_call" in kinds and "tool_result" in kinds
    assert kinds[-1] == "done"

    tool_result_evt = next(e for e in events if e["type"] == "tool_result")
    assert tool_result_evt["sources"][0]["url"] == "https://www.wmata.com/pay.html"

    text = "".join(e["text"] for e in events if e["type"] == "text")
    assert "6.75" in text

    # assistant + tool_result user turn + assistant  ->  +3 messages
    assert len(messages) == 4
    assert messages[1]["role"] == "assistant"
    assert messages[2]["role"] == "user"
    assert messages[2]["content"][0]["type"] == "tool_result"


def test_run_chat_plain_answer_no_tools():
    asyncio.run(_run_chat_plain_answer_no_tools())


async def _run_chat_plain_answer_no_tools():
    final = SimpleNamespace(
        content=[_text_block("Trains run roughly 5am to midnight.")],
        stop_reason="end_turn",
    )
    client = _FakeClient([_FakeStream(["Trains run ", "roughly 5am to midnight."], final)])
    messages = [{"role": "user", "content": "when does metro open?"}]

    events = [
        e
        async for e in run_chat(
            client=client, bridge=_FakeBridge(), model="m",
            messages=messages, max_tokens=256, max_iterations=6,
        )
    ]
    assert [e["type"] for e in events][-1] == "done"
    assert not any(e["type"] == "tool_call" for e in events)
    assert len(messages) == 2
