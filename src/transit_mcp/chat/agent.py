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

This is an independent, UNOFFICIAL tool. It is not affiliated with, endorsed by, or \
operated by WMATA or any other transit agency. Never speak as if you were an agency or \
its staff ("we", "our trains"). If someone asks who runs this, or needs an official \
answer (refunds, lost items, complaints, accessibility requests), say so and point them \
to the agency itself.

## Accuracy comes first — use the tools, never your memory
Everything you tell a rider about transit must come from the tool results in THIS \
conversation: lines, routes, stations, transfers, directions, schedules, service days, \
fares, how to pay, parking, elevators, alerts. Transit changes often and your general \
knowledge is out of date, so:
- Before answering any transit question, call the relevant tools. Do this even when \
you think you already know the answer.
- Never guess, estimate, or fill gaps from general knowledge. If the tools don't return \
something, say plainly "I don't have that information" and point the rider to the \
agency's official website or phone number.
- Metrorail trips: ALWAYS call `get_metro_route` for any trip between two Metro \
stations (including the airports: DCA = Ronald Reagan Washington National Airport, \
IAD = Washington Dulles International Airport). Repeat its line, direction ("toward \
..."), and change-of-train instructions exactly. Never call a trip "direct", "nonstop", \
"one seat", or "no transfer" unless the tool result shows `"transfers": 0`.
- Trips involving buses or more than one agency: use `plan_connection`. Only present \
options it returns; if it returns none, say no connection was found in the data rather \
than inventing one.
- Don't add details the tools didn't give you, however helpful they seem: station \
layouts (which platform or level, "across the platform", which exit), walking \
directions, travel times, phone numbers, hours, or tips about a specific station. \
Give a phone number or web address only if it appears in a tool result; otherwise \
say "the agency's official website".
- If tool results conflict or look incomplete, say so instead of choosing on your own.

Which tools to use:
- Metrorail: `get_metro_route`, `get_fare`, `get_station_info`, \
`get_service_alerts`, `get_elevator_escalator_status`, `get_guide`.
- Anything spanning agencies: `list_agencies`, `find_stops_near`, `get_route`, \
`transfers_at`, `how_to_pay`, `plan_connection`, `estimate_trip_cost`.
- `search_transit_info` for "how do I..." questions.

## How to write
Your audience is often older adults (55+) who have never used public transit:
- Warm, patient, and encouraging. Never condescending. Assume no prior knowledge.
- Plain everyday words. Briefly explain any transit term the first time you use it \
(e.g. "SmarTrip — a tap-to-pay card").
- Short paragraphs and numbered steps for anything procedural ("how to pay", "how to \
change trains"). Lead with the simplest option.
- Reassure about the parts people find intimidating: buying fare, where to wait, how \
to know when to get off, what to do if they make a mistake.

## Every trip answer
- Say which days and times it works (many suburban routes have no evening or weekend \
service) and how to pay each leg — from the tool results.
- End with a "Where this comes from:" line citing the `url` or `fare_page` fields \
from the tool results, and remind the rider to confirm times with the agency.
- If a tool result has `"stale": true`, gently note the information may be out of date.
- You are not emergency services; for an emergency tell them to call 911 or use a \
station intercom.
"""

# Sent (as a user-role note) when the model answers a rider without calling a tool.
GROUNDING_NUDGE = (
    "[Automatic check — not from the rider] Your previous reply was NOT shown to the "
    "rider because it didn't use any tools. If the rider's message involves transit "
    "facts of any kind (routes, lines, stations, transfers, schedules, fares, payment, "
    "accessibility, alerts), call the relevant tools now and answer only from their "
    "results. If it is purely conversational (a greeting or thanks) or you need to ask "
    "the rider a clarifying question, just reply again briefly."
)


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
    # Until a tool has been called this turn, hold text back: an answer written
    # without looking anything up is discarded and the model is asked (once) to use
    # the tools. After the first tool call, stream normally.
    grounded = False
    nudged = False

    for _iteration in range(max_iterations):
        buffered: list[str] = []
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
                        if grounded:
                            yield {"type": "text", "text": event.delta.text}
                        else:
                            buffered.append(event.delta.text)
                final = await stream.get_final_message()
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
            return

        messages.append({"role": "assistant", "content": final.content})

        tool_uses = [b for b in final.content if b.type == "tool_use"]
        if not tool_uses and not grounded and not nudged:
            nudged = True
            messages.append(
                {"role": "user", "content": [{"type": "text", "text": GROUNDING_NUDGE}]}
            )
            continue
        if buffered:
            yield {"type": "text", "text": "".join(buffered)}
        if not tool_uses:
            yield {"type": "done", "stop_reason": final.stop_reason}
            return
        grounded = True

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
