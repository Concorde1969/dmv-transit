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


def test_ungrounded_answer_is_discarded_and_tools_are_used():
    asyncio.run(_ungrounded_answer_is_discarded_and_tools_are_used())


async def _ungrounded_answer_is_discarded_and_tools_are_used():
    guess = SimpleNamespace(
        content=[_text_block("Take the Silver Line nonstop.")], stop_reason="end_turn"
    )
    lookup = SimpleNamespace(
        content=[_tool_use_block("tu_1", "get_fare",
                                 {"origin": "Shady Grove", "destination": "Metro Center"})],
        stop_reason="tool_use",
    )
    answer = SimpleNamespace(content=[_text_block("Peak fare is $6.75.")],
                             stop_reason="end_turn")
    client = _FakeClient([
        _FakeStream(["Take the Silver Line ", "nonstop."], guess),
        _FakeStream([], lookup),
        _FakeStream(["Peak fare is $6.75."], answer),
    ])
    bridge = _FakeBridge()
    messages = [{"role": "user", "content": "fare from shady grove to metro center?"}]

    events = [
        e
        async for e in run_chat(
            client=client, bridge=bridge, model="m",
            messages=messages, max_tokens=256, max_iterations=6,
        )
    ]
    text = "".join(e["text"] for e in events if e["type"] == "text")
    assert "nonstop" not in text  # the guess never reaches the rider
    assert "6.75" in text
    assert bridge.calls
    # the nudge is a list-content user turn, so history trimming never treats it as
    # a rider message
    nudge = messages[2]
    assert nudge["role"] == "user" and isinstance(nudge["content"], list)
    assert "Automatic check" in nudge["content"][0]["text"]


def test_run_chat_plain_answer_after_one_nudge():
    asyncio.run(_run_chat_plain_answer_after_one_nudge())


async def _run_chat_plain_answer_after_one_nudge():
    draft = SimpleNamespace(content=[_text_block("Hello there!")], stop_reason="end_turn")
    final = SimpleNamespace(
        content=[_text_block("Hello! Ask me about any trip.")], stop_reason="end_turn"
    )
    client = _FakeClient([
        _FakeStream(["Hello there!"], draft),
        _FakeStream(["Hello! ", "Ask me about any trip."], final),
    ])
    messages = [{"role": "user", "content": "hi"}]

    events = [
        e
        async for e in run_chat(
            client=client, bridge=_FakeBridge(), model="m",
            messages=messages, max_tokens=256, max_iterations=6,
        )
    ]
    assert [e["type"] for e in events][-1] == "done"
    assert not any(e["type"] == "tool_call" for e in events)
    text = "".join(e["text"] for e in events if e["type"] == "text")
    assert text == "Hello! Ask me about any trip."
    assert len(client.messages.calls) == 2  # nudged once, never twice
    assert len(messages) == 4
