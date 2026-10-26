"""Monthly call budgets for metered APIs (Transitland's REST API).

Transitland's Explorer plan allows 5,000 REST calls a month. Every call the app makes
— a live departures check, a feed lookup, a hosted feed download — is counted here
first; once the month's budget (``TRANSITLAND_MONTHLY_BUDGET``) is spent, callers fall
back to what they can do without the API (e.g. scheduled times instead of live ones).
Counts live in the ``api_usage`` table of the transit database, shared by the web app
and the scheduler. Months follow the site's time zone.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from transit_mcp.db.database import Database


class ApiBudget:
    """Pass ``db`` (an open Database, single-threaded use) or ``db_path`` (a fresh
    connection per check — safe to share across web-server threads)."""

    def __init__(self, db: Database | None = None, *, service: str, monthly_limit: int,
                 timezone: str = "America/New_York", db_path: str | Path | None = None):
        if (db is None) == (db_path is None):
            raise ValueError("give exactly one of db or db_path")
        self.db = db
        self.db_path = db_path
        self.service = service
        self.limit = monthly_limit
        self._tz = ZoneInfo(timezone)

    @classmethod
    def transitland(cls, db: Database | None, settings, *, shared: bool = False) -> ApiBudget:
        """``shared=True``: for long-lived objects used from many threads."""
        return cls(None if shared else db, service="transitland",
                   monthly_limit=settings.transitland_monthly_budget,
                   timezone=settings.timezone,
                   db_path=settings.db_path if shared else None)

    @contextmanager
    def _conn(self) -> Iterator:
        if self.db is not None:
            yield self.db.conn
            return
        with Database(self.db_path) as db:
            yield db.conn

    def _month(self) -> str:
        return datetime.now(self._tz).strftime("%Y-%m")

    def used(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT calls FROM api_usage WHERE month = ? AND service = ?",
                (self._month(), self.service),
            ).fetchone()
        return int(row["calls"]) if row else 0

    def remaining(self) -> int:
        return max(0, self.limit - self.used())

    def try_spend(self, n: int = 1) -> bool:
        """Reserve ``n`` calls; False (nothing reserved) if that would exceed the budget."""
        month = self._month()
        with self._conn() as conn, conn:  # one transaction: check and add together
            row = conn.execute(
                "SELECT calls FROM api_usage WHERE month = ? AND service = ?",
                (month, self.service),
            ).fetchone()
            used = int(row["calls"]) if row else 0
            if used + n > self.limit:
                return False
            conn.execute(
                """
                INSERT INTO api_usage (month, service, calls) VALUES (?, ?, ?)
                ON CONFLICT(month, service) DO UPDATE SET calls = calls + excluded.calls
                """,
                (month, self.service, n),
            )
        return True
