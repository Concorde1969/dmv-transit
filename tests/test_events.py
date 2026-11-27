"""Holiday markets and events (planning/events.py, events.toml, find_events, /api/events)."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from transit_mcp.db.database import Database, init_db
from transit_mcp.planning import events

ET = ZoneInfo("America/New_York")

CURATED = """
[[event]]
id = "downtown"
name = "Downtown Market"
kind = "market"
venue = "F Street NW"
at = [38.89711, -77.02329]
start = 2026-11-13
end = 2026-12-23
closed = [2026-11-26]
hours = [
  { days = ["mon", "tue", "wed", "thu"], open = "12:00", close = "20:00" },
  { days = ["fri", "sat"], open = "11:00", close = "21:00" },
  { days = ["sun"], open = "11:00", close = "20:00" },
]
cost = "Free"
source = "https://example.org/downtown"
checked = "2026-11-21"

[[event]]
id = "weekends"
name = "Weekend Craft Show in Alexandria"
venue = "Gallery"
at = [38.82967, -77.05960]
start = 2026-12-04
end = 2026-12-20
hours = [
  { days = ["fri"], open = "18:00", close = "21:00" },
  { days = ["sat"], open = "09:00", close = "17:00" },
  { days = ["sun"], open = "11:00", close = "17:00" },
]
source = "https://example.org/weekends"
checked = "2026-11-21"
"""


def _setup(tmp_path, monkeypatch):
    path = tmp_path / "events.toml"
    path.write_text(CURATED, "utf-8")
    monkeypatch.setattr(events, "DEFAULT_FILE", path)
    for fn in (events.find, events.upcoming):
        defaults = list(fn.__defaults__)
        defaults[-1] = path
        monkeypatch.setattr(fn, "__defaults__", tuple(defaults))
    init_db(tmp_path / "t.db")
    db = Database(tmp_path / "t.db")
    db.conn.execute("INSERT OR REPLACE INTO stations (code, name, lat, lon) VALUES "
                    "('B01', 'Gallery Pl-Chinatown', 38.8983, -77.0219)")
    db.commit()
    return path, db


def test_open_days_follow_weekly_hours_and_closed_dates(tmp_path, monkeypatch):
    path, db = _setup(tmp_path, monkeypatch)
    db.close()
    downtown, weekends = events.curated(path)
    days = events.open_days(downtown, date(2026, 11, 25), date(2026, 11, 28))
    assert [d["date"] for d in days] == ["2026-11-25", "2026-11-27", "2026-11-28"]   # not Thanksgiving
    assert days[1]["hours"] == "11 AM to 9 PM" and days[1]["day"] == "Friday, November 27"
    only = events.open_days(weekends, date(2026, 12, 1), date(2026, 12, 13))
    assert [d["date"][-2:] for d in only] == ["04", "05", "06", "11", "12", "13"]
    assert only[0]["hours"] == "6 PM to 9 PM"
    assert events.weekly_hours(downtown)[0] == "Mon-Thu 12 PM to 8 PM"
    assert events.weekly_hours(weekends) == ["Fri 6 PM to 9 PM", "Sat 9 AM to 5 PM",
                                             "Sun 11 AM to 5 PM"]
    assert events.dates_text(downtown) == "Nov 13 to Dec 23"


def test_when_words_become_date_ranges():
    sat = date(2026, 11, 21)
    wed = date(2026, 11, 25)
    assert events.window("this weekend", sat)[:2] == (sat, date(2026, 11, 22))
    assert events.window("this weekend", wed)[:2] == (date(2026, 11, 27), date(2026, 11, 29))
    assert events.window("next weekend", sat)[:2] == (date(2026, 11, 27), date(2026, 11, 29))
    assert events.window("tomorrow", sat)[:2] == (date(2026, 11, 22),) * 2
    assert events.window("2026-12-04..2026-12-06", sat)[:2] == (date(2026, 12, 4), date(2026, 12, 6))
    assert events.window(None, sat)[1] == date(2026, 12, 4)


def test_find_lists_open_events_with_nearest_metro(tmp_path, monkeypatch):
    _, db = _setup(tmp_path, monkeypatch)
    try:
        now = datetime(2026, 11, 21, 14, 0, tzinfo=ET)
        out = events.find(db, now, "this weekend")
        assert [e["id"] for e in out["events"]] == ["downtown"]
        e = out["events"][0]
        assert e["open_now"] is True and e["nearest_metro"][0]["station"] == "Gallery Pl-Chinatown"
        assert e["point"] == "38.89711,-77.02329" and e["source"] == "https://example.org/downtown"
        later = events.find(db, now, "season", near=(38.83, -77.06), radius_m=5000)
        assert [e["id"] for e in later["events"]] == ["weekends"]
        assert events.find(db, now, "season", words="alexandria")["events"][0]["id"] == "weekends"
        assert events.find(db, now, "2027-01-10")["events"] == []
    finally:
        db.close()


def test_upcoming_drops_ended_events(tmp_path, monkeypatch):
    path, db = _setup(tmp_path, monkeypatch)
    db.close()
    assert [e["id"] for e in events.upcoming(date(2026, 12, 21), path)] == ["downtown"]
    assert events.upcoming(date(2026, 12, 24), path) == []
    soon = events.upcoming(date(2026, 12, 19), path)
    assert [e["id"] for e in soon] == ["downtown", "weekends"]
    assert soon[1]["next_open"]["date"] == "2026-12-19"


def test_find_events_tool(tmp_path, monkeypatch):
    from transit_mcp.server import app as server

    _, db = _setup(tmp_path, monkeypatch)
    db.close()
    monkeypatch.setattr(server, "_db", lambda: Database(tmp_path / "t.db"))
    out = server.find_events(when="season")
    assert {e["id"] for e in out["events"]} <= {"downtown", "weekends"}
    assert "source" in out["note"] and "confirm" not in out["note"]
    monkeypatch.setattr(server, "_place", lambda db, text: None)
    assert server.find_events(near="Nowhere")["error"] == "place_not_found"


def test_every_curated_event_is_complete():
    seen = set()
    for e in events.curated(events.DEFAULT_FILE):
        assert e["id"] not in seen, e["id"]
        seen.add(e["id"])
        assert e["source"].startswith("https://") and e["checked"], e["id"]
        assert e["name"] and e.get("venue") and e.get("address"), e["id"]
        lat, lon = e["at"]
        assert 38.0 < lat < 40.0 and -78.5 < lon < -76.0, e["id"]
        assert e["start"] <= e["end"], e["id"]
        assert e["hours"], e["id"]
        for h in e["hours"]:
            assert set(h["days"]) <= set(events.DAYS) and h["open"] < h["close"], e["id"]
        # Every event is open at least one day of its run.
        assert events.open_days(e, e["start"], e["end"]), e["id"]
