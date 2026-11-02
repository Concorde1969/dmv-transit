"""Trip options in a few lines each, for an LLM to read (MCP tools and the chatbot)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo


def clock(epoch: float | None, tz: ZoneInfo) -> str | None:
    # "%I" (not the Linux-only "%-I"), then drop the leading zero: "5:04 PM"
    return datetime.fromtimestamp(epoch, tz).strftime("%I:%M %p").lstrip("0") if epoch else None


def brief_option(opt: dict, tz: ZoneInfo) -> dict:
    """One planner option: when to leave and arrive, and each step in words."""
    steps = []
    for leg in opt["legs"]:
        if leg.get("airport"):
            a = leg["airport"]
            steps.append(f"{'free airport shuttle' if a['mode'] == 'shuttle' else 'walk'} "
                         f"about {round(leg['duration_s'] / 60)} min between the "
                         f"{a['name']} terminal and {a['station']}: {a['text']} "
                         f"(directions from {a['source']})")
        elif leg["mode"] == "walk":
            steps.append(f"walk {round(leg['duration_s'] / 60)} min"
                         + (f" (wait {round(leg['wait_s'] / 60)} min)"
                            if leg.get("transfer") else ""))
        else:
            name = leg["route_short_name"] or leg["route_long_name"] or leg["route_id"]
            steps.append(
                f"{leg['agency_name']} {name} toward {leg['headsign'] or '?'}: board "
                f"{leg['board']['name']} at {clock(leg['board']['expected'], tz)}"
                f"{' (live)' if leg['live'] else ' (scheduled)'}, ride {leg['stops']} "
                f"stops to {leg['alight']['name']}, arrive {clock(leg['alight']['expected'], tz)}")
    return {"leave_at": clock(opt["leave_at"], tz), "arrive_at": clock(opt["arrive_at"], tz),
            "minutes": round(opt["duration_s"] / 60), "transfers": opt["transfers"],
            "wait_minutes": round(opt["wait_s"] / 60),
            "walk_minutes": round(opt["walk_s"] / 60), "live": opt["live"],
            "fare_estimate": opt["fare_estimate"], "steps": steps}
