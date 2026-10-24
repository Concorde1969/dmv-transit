"""Streaming agent loop: Claude answers using the transit MCP tools."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from anthropic import AsyncAnthropic

from transit_mcp.chat.mcp_bridge import McpBridge

SYSTEM_PROMPT = """\
You are the DMV Regional Transportation Assistant. You help people get around the \
Washington DC / Maryland / Virginia ("DMV") region by public transit: WMATA \
(Metrorail trains and Metrobus buses) plus the connecting systems — VRE commuter \
rail, Virginia Breeze intercity coach, and the Fairfax Connector, ART, DASH, CUE, \
Loudoun, and OmniRide bus networks.

Your audience is often older adults (55+) who have never used public transit. Write \
for them:
- Warm, patient, and encouraging. Never condescending. Assume no prior knowledge.
- Plain everyday words. Spell out or briefly explain any transit term the first time \
you use it (e.g. "SmarTrip — a tap-to-pay card", "headway — how often a bus comes").
- Short paragraphs and numbered steps for anything procedural ("how to pay", "how to \
transfer"). Lead with the simplest option.
- Reassure about the parts people find intimidating: buying fare, where to wait, how \
to know when to get off, what to do if they make a mistake.

Ground every answer in the tools:
- WMATA specifics: `get_fare`, `get_service_alerts`, `get_elevator_escalator_status`, \
`get_station_info`, `get_guide`.
- Anything spanning agencies: `list_agencies`, `find_stops_near`, `get_route`, \
`transfers_at`, `how_to_pay`, `plan_connection`, `estimate_trip_cost`.
- `search_transit_info` for "how do I..." questions.

Rules:
- Only state things supported by tool results. If the tools don't cover it, say so \
plainly and point them to the agency's website or phone number.
- For any trip or connection, ALWAYS say which days and times it works (many suburban \
routes have no evening or weekend service) and how to pay each leg.
- Cite your sources at the end under a "Where this comes from:" line, using the `url` \
or `fare_page` fields from the tool results.
- If a tool result has `"stale": true`, gently note the information may be out of date \
and suggest confirming with the agency.
- Don't invent station names, fares, route numbers, or line colors.
- You are not emergency services; for an emergency tell them to call 911 or use a \
station intercom.
"""


def _walk_for_sources(obj: Any, out: dict[str, dict]) -> None:
    if isinstance(obj, dict):
        url = obj.get("url")
        if isinstance(url, str) and url:
            out.setdefault(
                url,
                {
                    "url": url,
                    "retrieved_at": obj.get("retrieved_at"),
                    "stale": bool(obj.get("stale")),
                },
            )
        for v in obj.values():
            _walk_for_sources(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_for_sources(v, out)


def extract_sources(tool_text: str) -> list[dict]:
    try:
        data = json.loads(tool_text)
    except (ValueError, TypeError):
        return []
    found: dict[str, dict] = {}
    _walk_for_sources(data, found)
    return list(found.values())


async def run_chat(
    *,
    client: AsyncAnthropic,
    bridge: McpBridge,
    model: str,
    messages: list[dict],
    max_tokens: int,
    max_iterations: int,
) -> AsyncIterator[dict]:
    """Drive one user turn to completion, yielding SSE-ready event dicts.

    ``messages`` is mutated in place with the assistant + tool_result turns so the
    caller can persist the updated transcript.
    """
    tools = bridge.anthropic_tools

    for _iteration in range(max_iterations):
        try:
            async with client.messages.stream(
                model=model,
                max_tokens=max_tokens,
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=tools,
            ) as stream:
                async for event in stream:
                    if (
                        event.type == "content_block_delta"
                        and event.delta.type == "text_delta"
                    ):
                        yield {"type": "text", "text": event.delta.text}
                final = await stream.get_final_message()
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
            return

        messages.append({"role": "assistant", "content": final.content})

        tool_uses = [b for b in final.content if b.type == "tool_use"]
        if not tool_uses:
            yield {"type": "done", "stop_reason": final.stop_reason}
            return

        tool_result_blocks: list[dict] = []
        for tu in tool_uses:
            yield {
                "type": "tool_call",
                "id": tu.id,
                "name": tu.name,
                "input": tu.input,
            }
            text, is_error = await bridge.call_tool(tu.name, tu.input or {})
            tool_result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": text,
                    "is_error": is_error,
                }
            )
            preview = text if len(text) <= 600 else text[:600] + " ..."
            yield {
                "type": "tool_result",
                "name": tu.name,
                "is_error": is_error,
                "sources": extract_sources(text),
                "preview": preview,
            }

        messages.append({"role": "user", "content": tool_result_blocks})

    yield {
        "type": "error",
        "message": f"Stopped after {max_iterations} tool rounds without a final answer.",
    }
