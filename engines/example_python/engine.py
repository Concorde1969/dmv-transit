"""Example scheduling engine in Python — a template, not a real scheduler.

Use it two ways (pick one in .env):

  In-process (simplest, no extra runtime):
      PLANNER_ENGINE=python
      PLANNER_PYTHON=engines/example_python/engine.py:create

  As a separate process speaking JSON lines over stdin/stdout:
      PLANNER_ENGINE=process
      PLANNER_COMMAND=python engines/example_python/engine.py

Replace the body of ``ExampleEngine.plan`` with your scheduler. It receives a
TripRequest (as a dict when run as a process, or a pydantic object in-process) and
must return a TripPlan. The full contract is in docs/planner-schema/.
"""

from __future__ import annotations

import json
import os
import sys

NAME = "example-python"
VERSION = "0.1.0"


class ExampleEngine:
    name = NAME

    def describe(self) -> dict:
        return {
            "name": NAME,
            "version": VERSION,
            "language": "python",
            "capabilities": [],
            "notes": "Template engine: echoes the nearest stops; computes no schedules.",
        }

    def plan(self, request) -> dict:
        req = request.model_dump() if hasattr(request, "model_dump") else request
        origin, dest = req["origin"], req["destination"]

        # What a real engine would load: the raw GTFS archive for each agency.
        feeds = req["context"]["feeds"]
        readable = [f for f in feeds if f.get("path") and os.path.isfile(f["path"])]

        o_stop = (origin.get("nearby_stops") or [None])[0]
        d_stop = (dest.get("nearby_stops") or [None])[0]
        legs = []
        if o_stop and d_stop:
            legs.append({
                "mode": "ride",
                "agency": o_stop["agency"],
                "board": o_stop["name"],
                "board_stop_id": o_stop["stop_id"],
                "alight": d_stop["name"] if d_stop["agency"] == o_stop["agency"] else None,
                "note": "Placeholder leg — a real engine chooses routes and times here.",
            })

        return {
            "engine": NAME,
            "origin": origin["label"],
            "destination": dest["label"],
            "day": req.get("day"),
            "options": [{
                "summary": f"Example plan from {origin['label']} to {dest['label']}",
                "legs": legs,
                "feasible": None,
                "feasibility": "unknown — example engine does not read schedules",
            }],
            "warnings": [
                f"Example engine: received {len(readable)} GTFS archive(s) "
                f"({', '.join(f['agency'] for f in readable) or 'none'}) but computed no "
                "schedule. Replace engines/example_python/engine.py with your scheduler."
            ],
        }


def create(settings=None) -> ExampleEngine:
    """Factory used by PLANNER_PYTHON=...:create."""
    return ExampleEngine()


def _serve_stdio() -> None:
    """The process protocol: one JSON request per line in, one reply per line out."""
    engine = ExampleEngine()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        msg_id, method = msg.get("id"), msg.get("method")
        try:
            if method == "describe":
                reply = {"id": msg_id, "result": engine.describe()}
            elif method == "plan":
                reply = {"id": msg_id, "result": engine.plan(msg.get("params") or {})}
            else:
                reply = {"id": msg_id, "error": {"message": f"unknown method {method!r}"}}
        except Exception as exc:  # noqa: BLE001
            reply = {"id": msg_id, "error": {"message": str(exc)}}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    _serve_stdio()
