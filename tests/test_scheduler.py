from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from transit_mcp.scheduler import Scheduler, next_daily

ET = ZoneInfo("America/New_York")


def test_next_daily():
    now = datetime(2026, 10, 24, 2, 0, tzinfo=ET)
    assert next_daily(now, "03:30") == datetime(2026, 10, 24, 3, 30, tzinfo=ET)
    later = datetime(2026, 10, 24, 4, 0, tzinfo=ET)
    assert next_daily(later, "03:30") == datetime(2026, 10, 25, 3, 30, tzinfo=ET)


def _sched(isolated_settings, start, *, empty):
    clock = [start]
    ran: list[str] = []
    s = Scheduler(
        isolated_settings,
        now=lambda: clock[0],
        sleep=lambda secs: None,
        full_job=lambda: ran.append("full") or {},
        alerts_job=lambda: ran.append("alerts") or {},
        has_data=lambda: not empty,
    )
    return s, clock, ran


def test_first_boot_loads_everything_then_alerts(isolated_settings):
    s, clock, ran = _sched(isolated_settings, datetime(2026, 10, 24, 12, 0, tzinfo=ET),
                           empty=True)
    s.start()
    s.tick()
    assert ran == ["full", "alerts"]


def test_alerts_every_15_minutes_and_nightly_full(isolated_settings):
    s, clock, ran = _sched(isolated_settings, datetime(2026, 10, 24, 3, 0, tzinfo=ET),
                           empty=False)
    s.start()
    for _ in range(12):  # walk 03:00 -> 03:55 in 5-minute steps
        s.tick()
        clock[0] += timedelta(minutes=5)
    # alerts at 03:00 and 03:15, full at 03:30 (which also covers alerts),
    # then alerts again at 03:45.
    assert ran == ["alerts", "alerts", "full", "alerts"]
    assert s.next_full.date().isoformat() == "2026-10-25"


def test_failed_job_does_not_stop_scheduler(isolated_settings):
    def boom():
        raise RuntimeError("feed server down")

    s = Scheduler(isolated_settings, now=lambda: datetime(2026, 10, 24, 12, tzinfo=ET),
                  full_job=boom, alerts_job=boom, has_data=lambda: True)
    s.start()
    s.tick()  # logs, doesn't raise
    assert s.history[-1][0] == "alerts"
