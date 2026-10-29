"""Accounts: visitors save their hotel or home and the places they want to visit, then
pick them with one tap when planning a trip.

Sign-in is by email code: the visitor types an email address, we send a 6-digit code
(``mailer.py``), and typing it back signs them in — no passwords. A code works for
10 minutes and for 5 tries. Signed-in browsers hold a random session token in an
HttpOnly cookie; only its hash is stored.

Everything lives in its own SQLite file (``ACCOUNTS_DB``), separate from the chat log:
accounts are never linked to the anonymous usage log. An account and its places are
deleted when the visitor deletes it, or after ``ACCOUNTS_INACTIVE_DAYS`` without a
visit (:meth:`AccountStore.prune`).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

log = logging.getLogger("transit_mcp.accounts")

CODE_TTL_S = 10 * 60
CODE_TRIES = 5
SESSION_TTL_S = 90 * 86_400
KINDS = ("hotel", "home", "attraction", "work", "other")
EMAIL = re.compile(r"^[^\s@<>()\[\],;:\"]{1,64}@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    email       TEXT NOT NULL UNIQUE,      -- lower case
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL              -- UTC date, for deleting unused accounts
);
-- One code at a time per email address; only its keyed hash is kept.
CREATE TABLE IF NOT EXISTS login_codes (
    email       TEXT PRIMARY KEY,
    code_hash   TEXT NOT NULL,
    expires_at  REAL NOT NULL,
    tries       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
-- Saved places. Dates: a hotel stay (check-in, check-out) or a planned visit (start).
CREATE TABLE IF NOT EXISTS places (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- hotel | home | attraction | work | other
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    address     TEXT,
    start_date  TEXT,
    end_date    TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_places_user ON places(user_id);
"""


def normalize_email(email: str) -> str | None:
    """The address in lower case, or None if it doesn't look like one."""
    e = (email or "").strip().lower()
    return e if len(e) <= 254 and EMAIL.match(e) else None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass
class PlaceInput:
    name: str
    kind: str
    lat: float
    lon: float
    address: str | None = None
    start_date: date | None = None
    end_date: date | None = None


class PlaceError(ValueError):
    """A saved place the store won't accept (shown to the visitor)."""


class AccountStore:
    def __init__(self, path: str | Path, *, secret: str | None = None,
                 max_places: int = 50, clock=time.time):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_places = max_places
        self._clock = clock
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._secret = (secret or self._stored_secret()).encode("utf-8")

    @classmethod
    def from_settings(cls, settings) -> AccountStore:
        return cls(settings.accounts_db, secret=settings.accounts_secret,
                   max_places=settings.accounts_max_places)

    def close(self) -> None:
        self.conn.close()

    def _stored_secret(self) -> str:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'secret'").fetchone()
            if row:
                return row["value"]
            secret = secrets.token_hex(32)
            self.conn.execute("INSERT INTO meta (key, value) VALUES ('secret', ?)", (secret,))
            self.conn.commit()
            return secret

    def _code_hash(self, email: str, code: str) -> str:
        return hmac.new(self._secret, f"{email}:{code}".encode(), hashlib.sha256).hexdigest()

    # -- sign-in codes -----------------------------------------------------------------
    def issue_code(self, email: str) -> str:
        """A new 6-digit code for ``email`` (any earlier one stops working)."""
        code = f"{secrets.randbelow(1_000_000):06d}"
        with self._lock:
            self.conn.execute(
                "INSERT INTO login_codes (email, code_hash, expires_at, tries) "
                "VALUES (?, ?, ?, 0) ON CONFLICT(email) DO UPDATE SET "
                "code_hash = excluded.code_hash, expires_at = excluded.expires_at, tries = 0",
                (email, self._code_hash(email, code), self._clock() + CODE_TTL_S))
            self.conn.commit()
        return code

    def check_code(self, email: str, code: str) -> bool:
        """True once for the right, unexpired code; each wrong try counts."""
        code = re.sub(r"\D", "", code or "")
        with self._lock:
            row = self.conn.execute("SELECT * FROM login_codes WHERE email = ?",
                                    (email,)).fetchone()
            if row is None or row["expires_at"] < self._clock() or row["tries"] >= CODE_TRIES:
                return False
            if len(code) == 6 and hmac.compare_digest(row["code_hash"],
                                                      self._code_hash(email, code)):
                self.conn.execute("DELETE FROM login_codes WHERE email = ?", (email,))
                self.conn.commit()
                return True
            self.conn.execute("UPDATE login_codes SET tries = tries + 1 WHERE email = ?",
                              (email,))
            self.conn.commit()
            return False

    # -- users and sessions ------------------------------------------------------------
    def sign_in(self, email: str) -> tuple[dict, str, bool]:
        """(user, session token, whether the account is new) for a checked email."""
        token = secrets.token_urlsafe(32)
        now = _utc_now()
        with self._lock:
            row = self.conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
            created = row is None
            if created:
                cur = self.conn.execute("INSERT INTO users (email, created_at, last_seen) "
                                        "VALUES (?, ?, ?)", (email, now, now[:10]))
                user_id = cur.lastrowid
            else:
                user_id = row["id"]
                self.conn.execute("UPDATE users SET last_seen = ? WHERE id = ?",
                                  (now[:10], user_id))
            self.conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) "
                              "VALUES (?, ?, ?)", (_sha(token), user_id,
                                                   self._clock() + SESSION_TTL_S))
            self.conn.commit()
        return {"id": user_id, "email": email}, token, created

    def user_for_token(self, token: str | None) -> dict | None:
        """The signed-in user, or None. Using a session keeps it alive for 90 days."""
        if not token or len(token) > 100:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT u.id, u.email, u.last_seen, s.expires_at FROM sessions s "
                "JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
                (_sha(token),)).fetchone()
            if row is None or row["expires_at"] < self._clock():
                return None
            today = _utc_now()[:10]
            if row["last_seen"] != today:
                self.conn.execute("UPDATE users SET last_seen = ? WHERE id = ?",
                                  (today, row["id"]))
                self.conn.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                                  (self._clock() + SESSION_TTL_S, _sha(token)))
                self.conn.commit()
        return {"id": row["id"], "email": row["email"]}

    def sign_out(self, token: str | None) -> None:
        if token:
            with self._lock:
                self.conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_sha(token),))
                self.conn.commit()

    def delete_user(self, user_id: int) -> None:
        """The account, its places and its sessions."""
        with self._lock:
            for table, col in (("places", "user_id"), ("sessions", "user_id"),
                               ("users", "id")):
                self.conn.execute(f"DELETE FROM {table} WHERE {col} = ?", (user_id,))
            self.conn.commit()

    # -- places --------------------------------------------------------------------------
    @staticmethod
    def _place(row: sqlite3.Row) -> dict:
        return {k: row[k] for k in ("id", "name", "kind", "lat", "lon", "address",
                                    "start_date", "end_date")}

    def places(self, user_id: int) -> list[dict]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM places WHERE user_id = ? ORDER BY id",
                                     (user_id,)).fetchall()
        return [self._place(r) for r in rows]

    @staticmethod
    def _values(p: PlaceInput) -> tuple:
        name = " ".join((p.name or "").split())[:80]
        if not name:
            raise PlaceError("Please give the place a name.")
        if p.kind not in KINDS:
            raise PlaceError("Please pick what kind of place this is.")
        start, end = p.start_date, p.end_date
        if p.kind != "hotel":
            end = None                      # only hotel stays have an end date
        if start and end and end < start:
            raise PlaceError("The check-out date is before the check-in date.")
        address = " ".join((p.address or "").split())[:200] or None
        return (name, p.kind, round(p.lat, 6), round(p.lon, 6), address,
                start.isoformat() if start else None, end.isoformat() if end else None)

    def add_place(self, user_id: int, p: PlaceInput) -> dict:
        values = self._values(p)
        now = _utc_now()
        with self._lock:
            (n,) = self.conn.execute("SELECT COUNT(*) FROM places WHERE user_id = ?",
                                     (user_id,)).fetchone()
            if n >= self.max_places:
                raise PlaceError(f"You can save up to {self.max_places} places. "
                                 "Delete one to add another.")
            cur = self.conn.execute(
                "INSERT INTO places (user_id, name, kind, lat, lon, address, start_date, "
                "end_date, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, *values, now, now))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM places WHERE id = ?",
                                    (cur.lastrowid,)).fetchone()
        return self._place(row)

    def update_place(self, user_id: int, place_id: int, p: PlaceInput) -> dict | None:
        values = self._values(p)
        with self._lock:
            cur = self.conn.execute(
                "UPDATE places SET name = ?, kind = ?, lat = ?, lon = ?, address = ?, "
                "start_date = ?, end_date = ?, updated_at = ? WHERE id = ? AND user_id = ?",
                (*values, _utc_now(), place_id, user_id))
            self.conn.commit()
            if not cur.rowcount:
                return None
            row = self.conn.execute("SELECT * FROM places WHERE id = ?", (place_id,)).fetchone()
        return self._place(row)

    def delete_place(self, user_id: int, place_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM places WHERE id = ? AND user_id = ?",
                                    (place_id, user_id))
            self.conn.commit()
        return bool(cur.rowcount)

    # -- housekeeping ------------------------------------------------------------------
    def prune(self, inactive_days: int) -> int:
        """Drop used-up codes and sessions, and accounts unused for ``inactive_days``.
        Returns the number of accounts deleted."""
        now = self._clock()
        cutoff = (datetime.now(UTC) - timedelta(days=inactive_days)).date().isoformat()
        with self._lock:
            self.conn.execute("DELETE FROM login_codes WHERE expires_at < ?", (now,))
            self.conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
            stale = [r[0] for r in self.conn.execute(
                "SELECT id FROM users WHERE last_seen < ?", (cutoff,))]
            for user_id in stale:
                self.conn.execute("DELETE FROM places WHERE user_id = ?", (user_id,))
                self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self.conn.commit()
        return len(stale)
