"""Keep a deployed server's data fresh (``transit-mcp scheduler``).

* On start: if the database is empty, load everything (first boot).
* Every ``REFRESH_ALERTS_MINUTES``: refresh real-time sources (``api`` scope — WMATA
  alerts and elevator outages) without the 100-call fare matrix.
* Daily at ``REFRESH_FULL_AT`` (local ``TIMEZONE``): refresh everything — schedules,
  fares, rider pages, the transfer graph.

Runs as its own container next to the web app; SQLite's WAL mode lets the app keep
reading while this writes.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from transit_mcp.config import Settings, get_settings

log = logging.getLogger("transit_mcp.scheduler")


def next_daily(now: datetime, hhmm: str) -> datetime:
    """The next time at local ``HH:MM`` strictly after ``now`` (``now`` is tz-aware)."""
    hour, minute = (int(x) for x in hhmm.split(":", 1))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


class Scheduler:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        full_job: Callable[[], dict] | None = None,
        alerts_job: Callable[[], dict] | None = None,
        has_data: Callable[[], bool] | None = None,
    ):
        self.settings = settings or get_settings()
        tz = ZoneInfo(self.settings.timezone)
        self._now = now or (lambda: datetime.now(tz))
        self._sleep = sleep
        self._full_job = full_job or self._run_full
        self._alerts_job = alerts_job or self._run_alerts
        self._has_data = has_data or self._db_has_data
        self.alerts_every = timedelta(minutes=max(1, self.settings.refresh_alerts_minutes))
        self.next_full: datetime | None = None
        self.next_alerts: datetime | None = None
        self.history: list[tuple[str, datetime]] = []

    # -- default jobs -------------------------------------------------------
    def _run_full(self) -> dict:
        from transit_mcp.ingest.run import run_all

        return run_all(self.settings)

    def _run_alerts(self) -> dict:
        from transit_mcp.ingest.pipeline import run_ingest
        from transit_mcp.ingest.sources import RunOptions

        return run_ingest(
            self.settings, scope="api",
            options=RunOptions(include_fares=False, rebuild_links=False,
                               load_stop_times=self.settings.gtfs_load_stop_times),
        )

    def _db_has_data(self) -> bool:
        from transit_mcp.db.database import init_db
        from transit_mcp.doctor import has_data

        init_db(self.settings.db_path)
        return has_data(self.settings)

    # -- loop ---------------------------------------------------------------
    def _do(self, name: str, job: Callable[[], dict]) -> None:
        started = self._now()
        self.history.append((name, started))
        log.info("starting %s refresh", name)
        try:
            result = job() or {}
            problems = result.get("errors") or []
            log.info("%s refresh finished in %.0fs%s", name,
                     (self._now() - started).total_seconds(),
                     f" with {len(problems)} problem(s): {problems}" if problems else "")
        except Exception:  # noqa: BLE001 - keep the scheduler alive; retry next cycle
            log.exception("%s refresh failed", name)

    def start(self) -> None:
        now = self._now()
        if not self._has_data():
            log.info("database is empty — loading all transit data (first run)")
            self._do("full", self._full_job)
            now = self._now()
        self.next_full = next_daily(now, self.settings.refresh_full_at)
        self.next_alerts = now  # refresh alerts right away

    def tick(self) -> None:
        """Run whatever is due now (at most one job per tick)."""
        now = self._now()
        if now >= self.next_full:
            self._do("full", self._full_job)
            now = self._now()
            self.next_full = next_daily(now, self.settings.refresh_full_at)
            self.next_alerts = now + self.alerts_every  # a full run includes alerts
        elif now >= self.next_alerts:
            self._do("alerts", self._alerts_job)
            self.next_alerts = self._now() + self.alerts_every

    def seconds_until_next(self) -> float:
        due = min(self.next_full, self.next_alerts)
        return max(1.0, min(60.0, (due - self._now()).total_seconds()))

    def run_forever(self) -> None:
        self.start()
        log.info("next alerts refresh %s; next full refresh %s",
                 self.next_alerts.strftime("%H:%M"), self.next_full.strftime("%a %H:%M"))
        while True:
            self.tick()
            self._sleep(self.seconds_until_next())
