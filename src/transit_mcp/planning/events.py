"""Holiday markets and seasonal events, curated in ``events.toml`` from each organizer's
own page: when each one is open, where, what it costs, and the source it came from.

``open_days`` turns an event's start/end dates, weekly hours and closed dates into the
actual days it's open; ``find`` lists the events open in a date range, optionally near a
place or matching words, each with its open days and the nearest Metro station.
"""

from __future__ import annotations

import re
import sqlite3
import tomllib
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path

from transit_mcp.ingest.gtfs.clusters import haversine_m

DEFAULT_FILE = Path(__file__).with_name("events.toml")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
METRO_WALK_M = 1600          # list a Metro station as "nearby" within about a mile


@lru_cache(maxsize=4)
def curated(path: Path = DEFAULT_FILE) -> list[dict]:
    with open(path, "rb") as f:
        return tomllib.load(f).get("event", [])


def clock(hhmm: str) -> str:
    """"18:00" -> "6 PM", "11:30" -> "11:30 AM"."""
    h, m = (int(x) for x in hhmm.split(":"))
    suffix = "AM" if h < 12 else "PM"
    h = h % 12 or 12
    return f"{h}:{m:02d} {suffix}" if m else f"{h} {suffix}"


def hours_on(event: dict, day: date) -> dict | None:
    """When the event is open on one day, or None if it isn't."""
    if not (event["start"] <= day <= event["end"]) or day in event.get("closed", []):
        return None
    weekday = DAYS[day.weekday()]
    for h in event.get("hours", []):
        if weekday in h["days"]:
            return {"date": day.isoformat(), "day": day.strftime("%A, %B ") + str(day.day),
                    "opens": h["open"], "closes": h["close"],
                    "hours": f"{clock(h['open'])} to {clock(h['close'])}"}
    return None


def open_days(event: dict, first: date, last: date) -> list[dict]:
    """Every day from ``first`` to ``last`` (inclusive) that the event is open."""
    day, out = max(first, event["start"]), []
    while day <= min(last, event["end"]):
        h = hours_on(event, day)
        if h:
            out.append(h)
        day += timedelta(days=1)
    return out


def dates_text(event: dict) -> str:
    """"Nov 13 to Dec 23" or "Sat, Dec 12" for the whole run."""
    s, e = event["start"], event["end"]
    if s == e:
        return s.strftime("%a, %b ") + str(s.day)
    return f"{s.strftime('%b ')}{s.day} to {e.strftime('%b ')}{e.day}"


def weekly_hours(event: dict) -> list[str]:
    """["Mon-Thu 12 PM to 8 PM", ...] for showing the usual hours."""
    out = []
    for h in event.get("hours", []):
        idx = sorted(DAYS.index(d) for d in h["days"])
        run = idx == list(range(idx[0], idx[-1] + 1)) and len(idx) > 2
        names = ([DAYS[idx[0]].title() + "-" + DAYS[idx[-1]].title()] if run
                 else [DAYS[i].title() for i in idx])
        out.append(f"{', '.join(names)} {clock(h['open'])} to {clock(h['close'])}")
    return out


def window(when: str | None, today: date) -> tuple[date, date, str]:
    """A rider's "when" as a date range: today, tomorrow, this weekend, next weekend,
    this week, a date ("2026-12-12"), a range ("2026-12-04..2026-12-06"), "season" (every
    upcoming day) or nothing (the next two weeks)."""
    w = (when or "").strip().lower()
    if w in ("", "upcoming", "soon"):
        return today, today + timedelta(days=13), "the next two weeks"
    if w == "today":
        return today, today, "today"
    if w == "tomorrow":
        t = today + timedelta(days=1)
        return t, t, "tomorrow"
    if w in ("this weekend", "weekend", "next weekend"):
        # Friday to Sunday; on a Friday, Saturday or Sunday "this weekend" is the rest of it.
        fri = today + timedelta(days=(4 - today.weekday()) % 7)
        if today.weekday() >= 4:
            fri = today - timedelta(days=today.weekday() - 4)
        if w == "next weekend":
            fri += timedelta(days=7)
        sun = fri + timedelta(days=2)
        return max(fri, today), sun, "next weekend" if w == "next weekend" else "this weekend"
    if w in ("this week", "week"):
        return today, today + timedelta(days=6 - today.weekday()), "this week"
    if w in ("season", "all", "holidays", "any"):
        return today, today + timedelta(days=366), "the rest of the season"
    found = re.findall(r"\d{4}-\d{2}-\d{2}", w)
    if found:
        first = date.fromisoformat(found[0])
        last = date.fromisoformat(found[1]) if len(found) > 1 else first
        first, last = max(first, today), max(last, first)
        label = first.isoformat() if first == last else f"{first} to {last}"
        return first, min(last, first + timedelta(days=366)), label
    return today, today + timedelta(days=13), "the next two weeks"


def _nearest_metro(db, lat: float, lon: float) -> list[dict]:
    try:
        rows = db.conn.execute("SELECT name, lat, lon FROM stations "
                               "WHERE lat IS NOT NULL").fetchall()
    except (sqlite3.OperationalError, AttributeError):
        return []
    near, seen = [], set()
    for r in rows:
        d = haversine_m(lat, lon, r["lat"], r["lon"])
        if d <= METRO_WALK_M and r["name"] not in seen:
            seen.add(r["name"])
            near.append({"station": r["name"], "distance_m": round(d)})
    return sorted(near, key=lambda x: x["distance_m"])[:2]


def _matches(event: dict, words: str | None) -> bool:
    if not words:
        return True
    text = " ".join(str(event.get(k, "")) for k in
                    ("name", "kind", "venue", "address", "summary")).lower()
    terms = [t for t in re.findall(r"[a-z0-9]+", words.lower())
             if t not in {"the", "a", "an", "and", "in", "at", "near", "holiday", "market",
                          "markets", "event", "events", "christmas"}]
    return all(t in text for t in terms) if terms else True


def describe(event: dict) -> dict:
    """An event's fixed facts, as tools and the map show them."""
    lat, lon = event["at"]
    return {k: v for k, v in {
        "id": event["id"], "name": event["name"], "kind": event.get("kind", "event"),
        "venue": event.get("venue"), "address": event.get("address"),
        "lat": lat, "lon": lon, "point": f"{lat:.5f},{lon:.5f}",
        "dates": dates_text(event), "start": event["start"].isoformat(),
        "end": event["end"].isoformat(), "usual_hours": weekly_hours(event),
        "cost": event.get("cost"), "summary": event.get("summary"),
        "getting_there": event.get("getting_there"), "notes": event.get("notes"),
        "tickets": event.get("tickets"), "source": event["source"],
        "checked": event.get("checked"),
    }.items() if v is not None}


def find(db, now: datetime, when: str | None = None, near: tuple[float, float] | None = None,
         radius_m: float = 15000, words: str | None = None,
         path: Path = DEFAULT_FILE) -> dict:
    """Events open in the ``when`` range (see ``window``), nearest first when ``near`` is
    given, otherwise soonest first. Each has its fixed facts, its ``open_days`` in the
    range and ``open_now``."""
    first, last, label = window(when, now.date())
    out = []
    for event in curated(path):
        if not _matches(event, words):
            continue
        days = open_days(event, first, last)
        if not days:
            continue
        item = describe(event)
        lat, lon = event["at"]
        if near:
            d = haversine_m(near[0], near[1], lat, lon)
            if d > radius_m:
                continue
            item["distance_m"] = round(d)
        item["open_now"] = is_open_now(days[0], now)
        item["open_days"] = days[:14]
        if len(days) > 14:
            item["more_open_days"] = len(days) - 14
        item["nearest_metro"] = _nearest_metro(db, lat, lon)
        out.append(item)
    out.sort(key=lambda e: (e.get("distance_m", 0), e["open_days"][0]["date"]))
    return {"range": {"from": first.isoformat(), "to": last.isoformat(), "label": label},
            "events": out}


def upcoming(today: date, path: Path = DEFAULT_FILE) -> list[dict]:
    """Every event that hasn't ended, soonest first, with its next open day (for the map)."""
    out = []
    for event in curated(path):
        if event["end"] < today:
            continue
        nxt = open_days(event, today, event["end"])
        if not nxt:
            continue
        out.append(describe(event) | {"next_open": nxt[0]})
    return sorted(out, key=lambda e: e["next_open"]["date"])


def is_open_now(event_day: dict, now: datetime) -> bool:
    return (event_day["date"] == now.date().isoformat()
            and event_day["opens"] <= now.strftime("%H:%M") < event_day["closes"])
