"""The assistant's trip-planning tools: find places to eat and visit, save places, build a
day-by-day plan, and get between its stops.

These run inside the web app rather than the MCP server, because they act for the
signed-in rider (their saved places and plan live in the accounts database) and they
spend the Google Places budget. Signed-out riders get a "please sign in" result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from transit_mcp.accounts import KINDS, PlaceError, PlaceInput, PlanStop
from transit_mcp.db.database import Database
from transit_mcp.geocode import BudgetSpent, GeocodeError, PlaceSearch, in_region
from transit_mcp.planning import itinerary
from transit_mcp.planning.live import Point

log = logging.getLogger("transit_mcp.trip_tools")

_POINT = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")
_TIME = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")
MAX_HOPS = 8
SIGN_IN = {"error": "sign_in_required",
           "message": "Planning a trip needs a free account. Ask the rider to use the Sign in "
                      "button at the top of the page (no email needed), then try again."}

_WHERE = ("Where it is: a 'point' (\"lat,lon\") from find_places or geocode_address, a "
          "street address, or the name of one of the rider's saved places.")
_DAY = "The date, YYYY-MM-DD."
_HHMM = "24-hour local time, HH:MM (e.g. 14:30)."

TOOLS = [
    {"name": "find_places",
     "description": "Find restaurants, cafes, attractions, museums, shops or other places in "
                    "the DC area with Google Maps: name, address, type, star rating, price "
                    "level, whether it's open now, opening hours and a Google Maps link. Use "
                    "it for every 'where should I eat / what should I see' question; never "
                    "suggest or describe a place from memory. Results come from Google Maps "
                    "and you must say so.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string", "description": "What to look for, e.g. 'pizza', "
                                                    "'kid-friendly museum', 'coffee'."},
         "near": {"type": "string", "description": "Optional. Search around here: " + _WHERE},
         "open_now": {"type": "boolean", "description": "Only places open right now."}},
         "required": ["query"]}},
    {"name": "my_trip",
     "description": "The rider's saved places and their trip plan, day by day, with each "
                    "stop's id, times and notes. Call it before changing the plan.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "save_place",
     "description": "Save a place to the rider's account (it then shows on their map and as "
                    "a one-tap pick). Kinds: hotel (with check-in and check-out dates), home, "
                    "attraction (with the day they plan to visit), work, other. Check the "
                    "details with the rider before saving.",
     "input_schema": {"type": "object", "properties": {
         "name": {"type": "string", "description": "Name to show, e.g. 'My hotel'."},
         "kind": {"type": "string", "enum": list(KINDS)},
         "where": {"type": "string", "description": _WHERE},
         "address": {"type": "string", "description": "Optional. The street address, from "
                                                      "find_places or geocode_address."},
         "start_date": {"type": "string", "description": "Hotel check-in date, or the day "
                                                        "they plan to visit. YYYY-MM-DD."},
         "end_date": {"type": "string", "description": "Hotel check-out date. YYYY-MM-DD."}},
         "required": ["name", "kind", "where"]}},
    {"name": "remove_saved_place",
     "description": "Remove one of the rider's saved places, by its id from my_trip.",
     "input_schema": {"type": "object", "properties": {
         "saved_place_id": {"type": "integer"}}, "required": ["saved_place_id"]}},
    {"name": "add_to_plan",
     "description": "Add a stop to the rider's plan: a place on a day, with the time they "
                    "must arrive by and/or the time they'll leave, if they have one (a tour, "
                    "a reservation, a show). Check day and times with the rider first.",
     "input_schema": {"type": "object", "properties": {
         "day": {"type": "string", "description": _DAY},
         "name": {"type": "string", "description": "Name to show, e.g. 'Lunch at Ben's "
                                                   "Chili Bowl'."},
         "where": {"type": "string", "description": _WHERE},
         "address": {"type": "string", "description": "Optional. The street address, from "
                                                      "find_places or geocode_address."},
         "arrive_by": {"type": "string", "description": "Optional. Must be there by. " + _HHMM},
         "leave_at": {"type": "string", "description": "Optional. Will leave at. " + _HHMM},
         "notes": {"type": "string", "description": "Optional short note, e.g. "
                                                    "'tickets at the door'."},
         "google_place_id": {"type": "string", "description": "Optional, from find_places."}},
         "required": ["day", "name", "where"]}},
    {"name": "change_plan_stop",
     "description": "Change a plan stop's day, times or note, by its id from my_trip. Pass "
                    "an empty string to clear a time or the note.",
     "input_schema": {"type": "object", "properties": {
         "stop_id": {"type": "integer"},
         "day": {"type": "string", "description": _DAY},
         "arrive_by": {"type": "string", "description": _HHMM},
         "leave_at": {"type": "string", "description": _HHMM},
         "notes": {"type": "string"}}, "required": ["stop_id"]}},
    {"name": "remove_from_plan",
     "description": "Remove a stop from the rider's plan, by its id from my_trip.",
     "input_schema": {"type": "object", "properties": {
         "stop_id": {"type": "integer"}}, "required": ["stop_id"]}},
    {"name": "directions_for_day",
     "description": "How to get between the stops of one day of the rider's plan, in order: "
                    "for each part, when to leave, which buses and trains, walking, and the "
                    "fare; arriving by each stop's time, or leaving at the previous stop's "
                    "time. Starts from the rider's hotel (or home) unless start_from is "
                    "given. Times are from the timetable.",
     "input_schema": {"type": "object", "properties": {
         "day": {"type": "string", "description": _DAY},
         "start_from": {"type": "string", "description": "Optional starting place. " + _WHERE},
         "start_time": {"type": "string", "description": "Optional. When they'll leave the "
                                                         "starting place. " + _HHMM}},
         "required": ["day"]}},
]
NAMES = frozenset(t["name"] for t in TOOLS)
# Tools that change the rider's account: the page refreshes its pins and lists after them.
CHANGES = frozenset({"save_place", "remove_saved_place", "add_to_plan", "change_plan_stop",
                     "remove_from_plan"})

PLANNING_PROMPT = """
## Planning a visitor's trip
You are also a friendly trip planner for visitors to the DC area, and the conversation is \
the planner. The trip tools (find_places, my_trip, save_place, add_to_plan, \
change_plan_stop, remove_from_plan, remove_saved_place, directions_for_day) work only for \
signed-in riders. If one returns sign_in_required, explain kindly that planning needs a \
free account (the Sign in button at the top of the page; no email needed), and keep \
helping with transit questions.
- Get to know the trip a little at a time (one or two questions per reply): where \
they're staying and their check-in and check-out dates, which days they're here, what \
they'd like to see, eat or do, and any times they must be somewhere (a tour, a \
reservation, a show, a train).
- Restaurants, cafes, attractions, museums, shops: ALWAYS use find_places, near where \
they'll be. Never name, recommend or describe a place from memory. Offer two to four \
choices with rating, price and whether it's open then, and say the details are from \
Google Maps.
- Save their hotel and the places they choose with save_place, and build the plan with \
add_to_plan (a day, plus the time they must arrive or leave when there is one). Before \
saving, check the place, day and time with them; afterwards, say what you saved.
- Use my_trip to see the plan, then walk them through each day with directions_for_day: \
when to leave, which bus or train, how long they'll walk, and the fare. For a single \
trip, use plan_trip.
- Dates: work out "Saturday" or "tomorrow" from the date in the note below and repeat \
the date back. Show times in 12-hour form (2:30 PM).
"""


def context_note(user: dict | None, tz: ZoneInfo, now: datetime | None = None) -> str:
    """Today's date (for "tomorrow" and "Saturday") and who is signed in."""
    now = now or datetime.now(tz)
    week = ", ".join(f"{(now + timedelta(days=i)):%a %b %d} = {(now + timedelta(days=i)):%Y-%m-%d}"
                     for i in range(8))
    who = (f"The rider is signed in as {user.get('username') or 'an account'}; the trip "
           "tools work." if user else
           "The rider is NOT signed in; trip tools will ask them to sign in.")
    return (f"[Context, not from the rider] It is now {now:%A, %B %d, %Y, %I:%M %p} Eastern. "
            f"Next days: {week}. {who}")


def _hhmm(value: str | None) -> str | None:
    """"9:30" -> "09:30"; "" or None -> None."""
    if not value:
        return None
    m = _TIME.match(value)
    if not m:
        raise PlaceError("Times must be 24-hour HH:MM, like 09:30 or 14:00.")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _day(value: str, today: date) -> date:
    try:
        d = date.fromisoformat((value or "").strip())
    except ValueError:
        raise PlaceError("Days must be dates like 2026-11-08.") from None
    if d < today:
        raise PlaceError("That day has already passed.")
    if d > today + timedelta(days=400):
        raise PlaceError("That day is too far ahead to plan.")
    return d


class TripTools:
    """The tools for one chat request, acting for whoever is signed in."""

    names = NAMES
    definitions = TOOLS

    def __init__(self, request):
        from transit_mcp.chat.account_api import COOKIE
        from transit_mcp.chat.trip_api import _service

        st = request.app.state
        self.settings = st.settings
        self.tz = ZoneInfo(self.settings.timezone)
        self.accounts = getattr(st, "accounts", None)
        self.user = (self.accounts.store.user_for_token(request.cookies.get(COOKIE))
                     if self.accounts is not None else None)
        self._trips = lambda: _service(request)
        self._state = st

    # -- plumbing -------------------------------------------------------------------------
    def _places(self) -> PlaceSearch:
        if getattr(self._state, "places_search", None) is None:
            self._state.places_search = PlaceSearch.for_search(self.settings,
                                                              http=self._trips().http)
        return self._state.places_search

    async def call(self, name: str, args: dict) -> tuple[str, bool, bool]:
        """(result JSON, is_error, changed the account)."""
        try:
            result = await asyncio.to_thread(getattr(self, f"_{name}"), **(args or {}))
        except TypeError as exc:
            result = {"error": "bad_arguments", "message": str(exc)}
        except PlaceError as exc:
            result = {"error": "not_saved", "message": str(exc)}
        except Exception:  # noqa: BLE001 - the model gets a plain failure, the log the detail
            log.exception("trip tool %s failed", name)
            result = {"error": "failed", "message": "That didn't work. Please try again."}
        failed = "error" in result
        return json.dumps(result, default=str), failed, (name in CHANGES and not failed)

    def _uid(self) -> int | None:
        return self.user["id"] if self.user else None

    def _today(self) -> date:
        return datetime.now(self.tz).date()

    def _resolve(self, where: str) -> tuple[float, float, str, str | None] | None:
        """(lat, lon, label, address) for a point, a saved place's name or an address."""
        where = (where or "").strip()
        m = _POINT.match(where)
        if m:
            lat, lon = float(m.group(1)), float(m.group(2))
            return (lat, lon, where, None) if in_region(lat, lon) else None
        if self._uid():
            key = where.lower()
            mine = self.accounts.store.places(self._uid())
            hit = (next((p for p in mine if p["name"].lower() == key), None)
                   or next((p for p in mine if key and key in p["name"].lower()), None))
            if hit:
                return hit["lat"], hit["lon"], hit["name"], hit["address"]
        try:
            found = self._trips().geocoder.search(where)
        except GeocodeError:
            found = []
        if found:
            f = found[0]
            return f["lat"], f["lon"], f["label"], f.get("full_label")
        return None

    @staticmethod
    def _address(spot, where: str, given: str | None) -> str | None:
        """The street address to keep: given, found, or the place name typed (never a
        bare "lat,lon")."""
        return given or spot[3] or (None if _POINT.match(where or "") else spot[2])

    def _where_error(self, where: str) -> dict:
        return {"error": "place_not_found",
                "message": f"Couldn't find '{where}' in the DC area. Use find_places or "
                           "geocode_address and pass its point, or ask for a fuller address."}

    # -- tools ----------------------------------------------------------------------------
    def _find_places(self, query: str, near: str | None = None, open_now: bool = False) -> dict:
        if not self._uid():
            return SIGN_IN
        places = self._places()
        if not places.available:
            return {"error": "not_set_up", "message": "Place search isn't set up on this site."}
        center = None
        if near:
            spot = self._resolve(near)
            if spot is None:
                return self._where_error(near)
            center = (spot[0], spot[1])
        try:
            results = places.search(query, near=center, open_now=bool(open_now))
        except BudgetSpent:
            return {"error": "search_paused",
                    "message": "This month's place searches are used up. Suggest the rider "
                               "check Google Maps directly."}
        except GeocodeError:
            return {"error": "search_failed", "message": "Place search isn't responding. "
                                                        "Try again in a moment."}
        return {"query": query, "near": near, "results": results, "source": "Google Maps",
                "note": "Ratings, prices and hours are from Google Maps and can change; "
                        "pass a result's point to save_place or add_to_plan."}

    def _my_trip(self) -> dict:
        if not self._uid():
            return SIGN_IN
        store = self.accounts.store
        days: dict[str, list] = {}
        for s in store.plan(self._uid()):
            days.setdefault(s["day"], []).append(
                {k: s[k] for k in ("id", "name", "arrive_by", "leave_at", "address", "notes")
                 if s[k] is not None})
        return {"today": self._today().isoformat(),
                "saved_places": [{k: p[k] for k in ("id", "name", "kind", "address",
                                                    "start_date", "end_date") if p[k]}
                                 for p in store.places(self._uid())],
                "plan": [{"day": d, "stops": stops} for d, stops in sorted(days.items())]}

    def _save_place(self, name: str, kind: str, where: str, start_date: str | None = None,
                    end_date: str | None = None, address: str | None = None) -> dict:
        if not self._uid():
            return SIGN_IN
        spot = self._resolve(where)
        if spot is None:
            return self._where_error(where)
        today = self._today()
        start = _day(start_date, today) if start_date else None
        end = _day(end_date, today) if end_date else None
        saved = self.accounts.store.add_place(self._uid(), PlaceInput(
            name=name, kind=kind, lat=spot[0], lon=spot[1],
            address=self._address(spot, where, address),
            start_date=start, end_date=end))
        return {"saved": saved, "message": f"Saved {saved['name']} to the rider's places."}

    def _remove_saved_place(self, saved_place_id: int) -> dict:
        if not self._uid():
            return SIGN_IN
        if not self.accounts.store.delete_place(self._uid(), int(saved_place_id)):
            return {"error": "not_found", "message": "No saved place has that id."}
        return {"removed": int(saved_place_id)}

    def _add_to_plan(self, day: str, name: str, where: str, arrive_by: str | None = None,
                     leave_at: str | None = None, notes: str | None = None,
                     google_place_id: str | None = None, address: str | None = None) -> dict:
        if not self._uid():
            return SIGN_IN
        spot = self._resolve(where)
        if spot is None:
            return self._where_error(where)
        stop = self.accounts.store.add_stop(self._uid(), PlanStop(
            day=_day(day, self._today()), name=name, lat=spot[0], lon=spot[1],
            arrive_by=_hhmm(arrive_by), leave_at=_hhmm(leave_at),
            address=self._address(spot, where, address),
            google_place_id=google_place_id, notes=notes))
        return {"added": stop, "message": f"Added {stop['name']} on {stop['day']}."}

    def _change_plan_stop(self, stop_id: int, day: str | None = None,
                          arrive_by: str | None = None, leave_at: str | None = None,
                          notes: str | None = None) -> dict:
        if not self._uid():
            return SIGN_IN
        changes = {"day": _day(day, self._today()) if day else None,
                   "arrive_by": "" if arrive_by == "" else _hhmm(arrive_by),
                   "leave_at": "" if leave_at == "" else _hhmm(leave_at),
                   "notes": notes}
        stop = self.accounts.store.update_stop(self._uid(), int(stop_id), **changes)
        if stop is None:
            return {"error": "not_found", "message": "No plan stop has that id."}
        return {"changed": stop}

    def _remove_from_plan(self, stop_id: int) -> dict:
        if not self._uid():
            return SIGN_IN
        if not self.accounts.store.delete_stop(self._uid(), int(stop_id)):
            return {"error": "not_found", "message": "No plan stop has that id."}
        return {"removed": int(stop_id)}

    def _base(self, day: date) -> dict | None:
        """Where the day starts: the hotel they're staying in that day, else home."""
        mine = self.accounts.store.places(self._uid())
        iso = day.isoformat()
        stays = [p for p in mine if p["kind"] == "hotel"
                 and (p["start_date"] or "") <= iso <= (p["end_date"] or p["start_date"] or iso)]
        undated = [p for p in mine if p["kind"] == "hotel" and not p["start_date"]]
        homes = [p for p in mine if p["kind"] == "home"]
        return (stays or undated or homes or [None])[0]

    def _directions_for_day(self, day: str, start_from: str | None = None,
                            start_time: str | None = None) -> dict:
        if not self._uid():
            return SIGN_IN
        d = _day(day, self._today())
        stops = self.accounts.store.plan(self._uid(), d.isoformat())
        if not stops:
            return {"day": d.isoformat(), "error": "no_stops",
                    "message": "There's nothing in the plan for that day yet."}
        if start_from:
            spot = self._resolve(start_from)
            if spot is None:
                return self._where_error(start_from)
            origin = Point(spot[0], spot[1], spot[2])
        else:
            base = self._base(d)
            if base is None:
                return {"day": d.isoformat(), "error": "no_start",
                        "message": "Ask where they'll start the day (their hotel?), then call "
                                   "again with start_from."}
            origin = Point(base["lat"], base["lon"], base["name"])
        svc = self._trips()
        legs, prev_leave = [], _hhmm(start_time)
        with Database(self.settings.db_path) as db:
            for stop in stops[:MAX_HOPS]:
                dest = Point(stop["lat"], stop["lon"], stop["name"])
                leg = itinerary.route(db, self.settings, svc.agencies, origin, dest, d,
                                      arrive_by=stop["arrive_by"],
                                      leave_at=None if stop["arrive_by"] else prev_leave)
                legs.append({"stop_id": stop["id"], **leg})
                origin, prev_leave = dest, stop["leave_at"]
        out = {"day": d.isoformat(), "legs": legs,
               "note": "Times are from the agencies' timetables (not live). Riders should "
                       "confirm on the day; plan_trip gives live times when leaving now."}
        if len(stops) > MAX_HOPS:
            out["more"] = f"Only the first {MAX_HOPS} stops are covered; ask about the rest."
        return out
