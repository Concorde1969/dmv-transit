"""Getting between the stops of a day's plan: be somewhere by a time, or leave at one.

The live planner looks forward from a start time, so "arrive by 10:30" is answered by
planning from earlier starts and keeping the latest trip that still arrives in time.
Plans are for future days, so times come from the timetable.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from transit_mcp.ingest.gtfs.clusters import haversine_m
from transit_mcp.planning.describe import brief_option, clock
from transit_mcp.planning.live import LivePlanner, LiveSettings, Point

WALK_ONLY_M = 900          # closer than this, just walk
LOOK_BACK_MIN = (45, 90, 180)  # how far before an arrive-by time to start searching
SLACK_MIN = 10                 # aim to arrive at least this early, when a trip allows


def _at(day: date, hhmm: str, tz: ZoneInfo) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(day, time(h, m), tz)


def _planner(db, settings, agencies, start: datetime) -> LivePlanner:
    return LivePlanner(db, cfg=LiveSettings.from_settings(settings), realtime=None,
                       agencies=agencies, tz=settings.timezone, now=start)


def route(db, settings, agencies, origin: Point, dest: Point, day: date, *,
          arrive_by: str | None = None, leave_at: str | None = None,
          now: datetime | None = None) -> dict:
    """The trip from ``origin`` to ``dest`` on ``day`` that arrives by ``arrive_by`` (as
    late as possible) or leaves at ``leave_at``."""
    tz = ZoneInfo(settings.timezone)
    now = now or datetime.now(tz)
    out: dict = {"from": origin.label, "to": dest.label}
    meters = haversine_m(origin.lat, origin.lon, dest.lat, dest.lon)
    if meters <= WALK_ONLY_M:
        walk = math.ceil(meters * 1.25 / settings.trip_walk_m_per_min)
        leave = (_at(day, arrive_by, tz) - timedelta(minutes=walk + 5) if arrive_by
                 else _at(day, leave_at, tz) if leave_at else None)
        out.update(status="walk", walk_minutes=walk,
                   note=f"It's about a {walk}-minute walk, so no bus or train is needed.")
        if leave:
            out["leave_at"] = clock(leave.timestamp(), tz)
        return out

    if arrive_by:
        target = _at(day, arrive_by, tz)
        out["arrive_by"] = clock(target.timestamp(), tz)
        if target <= now:
            out.update(status="past", note="That time has already passed.")
            return out
        earliest = None
        for back in LOOK_BACK_MIN:
            start = max(target - timedelta(minutes=back), now)
            options = _planner(db, settings, agencies, start).plan(origin, dest)["options"]
            fits = [o for o in options if o["arrive_at"] <= target.timestamp()]
            if fits:
                comfy = [o for o in fits
                         if target.timestamp() - o["arrive_at"] >= SLACK_MIN * 60]
                best = max(comfy or fits, key=lambda o: o["leave_at"])
                out.update(status="ok", option=brief_option(best, tz),
                           minutes_to_spare=round((target.timestamp() - best["arrive_at"]) / 60),
                           other_options=len(fits) - 1)
                return out
            if options and earliest is None:
                earliest = min(options, key=lambda o: o["arrive_at"])
            if start == now:
                break
        if earliest:
            out.update(status="late", option=brief_option(earliest, tz),
                       minutes_late=round((earliest["arrive_at"] - target.timestamp()) / 60),
                       note="No trip in the timetable arrives in time; this is the earliest.")
        else:
            out.update(status="none", note="No bus or train trip was found for this hop.")
        return out

    if leave_at:
        start = _at(day, leave_at, tz)
        out["leave_at"] = clock(start.timestamp(), tz)
        if start < now - timedelta(minutes=5):
            out.update(status="past", note="That time has already passed.")
            return out
        options = _planner(db, settings, agencies, max(start, now)).plan(origin, dest)["options"]
        if options:
            out.update(status="ok", option=brief_option(options[0], tz),
                       other_options=len(options) - 1)
        else:
            out.update(status="none", note="No bus or train trip was found for this hop.")
        return out

    out.update(status="no_time", note=f"Add a time to arrive at {dest.label}, or a time to "
                                       f"leave {origin.label}, to plan this part.")
    return out
