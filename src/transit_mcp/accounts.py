"""Accounts: visitors save their hotel or home and the places they want to visit, then
pick them with one tap when planning a trip.

Visitors sign in with a username and password. Passwords are stored only as salted
scrypt hashes; there is no email, so a recovery code shown once at sign-up is the way
back in after a forgotten password. Five wrong tries lock an account for 15 minutes.

Sign-in by email code (``mailer.py``) also works when it's switched on
(``ACCOUNTS_EMAIL_CODES``): the visitor types an email address and types back the
6-digit code we send. A code works for 10 minutes and for 5 tries.

Signed-in browsers hold a random session token in an HttpOnly cookie; only its hash is
stored.

Everything lives in its own SQLite file (``ACCOUNTS_DB``), separate from the chat log:
accounts are never linked to the anonymous usage log. An account and its places are
deleted when the visitor deletes it, or after ``ACCOUNTS_INACTIVE_DAYS`` without a
visit (:meth:`AccountStore.prune`).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
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
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,29}$")
MIN_PASSWORD = 10
MAX_PLAN_STOPS = 100
HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
MAX_FAILS = 5
LOCK_S = 15 * 60
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 1         # about 32 MB and 0.1 s per check
# Passwords long enough to pass the length rule that people still pick.
COMMON_PASSWORDS = {
    "1234567890", "12345678910", "0123456789", "0987654321", "1111111111", "1234512345",
    "password12", "password123", "password1234", "password!!", "passw0rd12", "qwertyuiop",
    "qwerty1234", "qwerty12345", "1q2w3e4r5t", "1qaz2wsx3edc", "abcdefghij", "abc1234567",
    "iloveyou12", "iloveyou123", "letmein123", "welcome123", "welcome1234", "football12",
    "baseball12", "princess12", "sunshine12", "superman12", "trustno1234", "monkey1234",
    "dragon1234", "asdfghjkl1", "zxcvbnm123", "changeme12", "changeme123", "metro12345",
    "dmvtransit", "dmvtransit1", "washington", "washington1", "washingtondc",
}

USERS_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id            INTEGER PRIMARY KEY,
    email         TEXT UNIQUE,                    -- lower case; email-code sign-in
    username      TEXT UNIQUE COLLATE NOCASE,     -- password sign-in
    password_hash TEXT,                           -- scrypt, salted
    recovery_hash TEXT,                           -- keyed hash of the recovery code
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until  REAL NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    last_seen     TEXT NOT NULL                   -- UTC date, for deleting unused accounts
);
"""

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
""" + USERS_TABLE.format(name="users") + """
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
-- The trip plan: places to be at on a day, with the times that matter (local HH:MM).
CREATE TABLE IF NOT EXISTS plan_stops (
    id              INTEGER PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    day             TEXT NOT NULL,             -- YYYY-MM-DD
    arrive_by       TEXT,                      -- must be there by
    leave_at        TEXT,                      -- will leave at
    name            TEXT NOT NULL,
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    address         TEXT,
    google_place_id TEXT,
    notes           TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plan_stops_user ON plan_stops(user_id, day);
"""


def normalize_email(email: str) -> str | None:
    """The address in lower case, or None if it doesn't look like one."""
    e = (email or "").strip().lower()
    return e if len(e) <= 254 and EMAIL.match(e) else None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R,
                         p=SCRYPT_P, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${key.hex()}"


def check_password(password: str, stored: str | None) -> bool:
    """True if ``password`` matches ``stored``. With nothing stored, the same work is
    still done, so a missing account takes as long to answer as a wrong password."""
    try:
        _, n, r, p, salt, key = (stored or "").split("$")
        n, r, p, salt, key = int(n), int(r), int(p), bytes.fromhex(salt), bytes.fromhex(key)
    except ValueError:
        n, r, p, salt, key = SCRYPT_N, SCRYPT_R, SCRYPT_P, b"\0" * 16, b""
    got = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
                         maxmem=64 * 1024 * 1024, dklen=32)
    return bool(key) and hmac.compare_digest(got, key)


def new_recovery_code() -> str:
    """16 letters and digits in groups of four, e.g. "K7QM-2XPA-9ZD4-HW3R" (80 bits)."""
    raw = base64.b32encode(secrets.token_bytes(10)).decode("ascii")
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def password_problem(password: str, username: str = "") -> str | None:
    """Why a new password won't do, or None."""
    if len(password) < MIN_PASSWORD:
        return f"Please use at least {MIN_PASSWORD} characters for your password."
    if len(password) > 200:
        return "That password is too long. Please use 200 characters or fewer."
    if password.lower() in COMMON_PASSWORDS or len(set(password)) < 4:
        return "That password is too easy to guess. Please choose another."
    if username and username.lower() in password.lower():
        return "Please don't use your username in your password."
    return None


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


@dataclass
class PlanStop:
    day: date
    name: str
    lat: float
    lon: float
    arrive_by: str | None = None
    leave_at: str | None = None
    address: str | None = None
    google_place_id: str | None = None
    notes: str | None = None


class AuthError(ValueError):
    """A sign-in that didn't work; the message is shown to the visitor."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


WRONG_LOGIN = "That username and password don't match. Please try again."
LOCKED = ("Too many wrong tries for this account. Please wait 15 minutes and try again, "
          "or use your recovery code.")


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
        self._migrate()
        self._secret = (secret or self._stored_secret()).encode("utf-8")

    @classmethod
    def from_settings(cls, settings) -> AccountStore:
        return cls(settings.accounts_db, secret=settings.accounts_secret,
                   max_places=settings.accounts_max_places)

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        """Accounts made before passwords had a required email; make room for usernames,
        keeping every account, session and place."""
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(users)")}
        if "username" in cols:
            return
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.conn:
                self.conn.execute(USERS_TABLE.format(name="users_new"))
                self.conn.execute("INSERT INTO users_new (id, email, created_at, last_seen) "
                                  "SELECT id, email, created_at, last_seen FROM users")
                self.conn.execute("DROP TABLE users")
                self.conn.execute("ALTER TABLE users_new RENAME TO users")
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")
        log.info("accounts: added usernames and passwords to the users table")

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

    def _recovery_hash(self, code: str) -> str:
        clean = re.sub(r"[^A-Z0-9]", "", (code or "").upper())
        return hmac.new(self._secret, f"recovery:{clean}".encode(), hashlib.sha256).hexdigest()

    def _new_session(self, user_id: int) -> str:
        """Call with the lock held; the caller commits."""
        token = secrets.token_urlsafe(32)
        self.conn.execute("INSERT INTO sessions (token_hash, user_id, expires_at) "
                          "VALUES (?, ?, ?)", (_sha(token), user_id,
                                               self._clock() + SESSION_TTL_S))
        return token

    @staticmethod
    def _user(row) -> dict:
        return {"id": row["id"], "email": row["email"], "username": row["username"]}

    # -- usernames and passwords -------------------------------------------------------
    def register(self, username: str, password: str) -> tuple[dict, str, str]:
        """(user, session token, recovery code) for a new account."""
        username = (username or "").strip()
        if not USERNAME.match(username):
            raise AuthError("Usernames are 3 to 30 letters, numbers, dots, dashes or "
                            "underscores, starting with a letter or number.", 422)
        problem = password_problem(password, username)
        if problem:
            raise AuthError(problem, 422)
        pw_hash, code = hash_password(password), new_recovery_code()
        now = _utc_now()
        with self._lock:
            try:
                cur = self.conn.execute(
                    "INSERT INTO users (username, password_hash, recovery_hash, created_at, "
                    "last_seen) VALUES (?, ?, ?, ?, ?)",
                    (username, pw_hash, self._recovery_hash(code), now, now[:10]))
            except sqlite3.IntegrityError:
                raise AuthError("That username is taken. Please choose another.", 409) from None
            token = self._new_session(cur.lastrowid)
            self.conn.commit()
        return {"id": cur.lastrowid, "email": None, "username": username}, token, code

    def _account_for(self, username: str):
        with self._lock:
            return self.conn.execute("SELECT * FROM users WHERE username = ?",
                                     ((username or "").strip(),)).fetchone()

    def _check(self, row, ok: bool) -> None:
        """Count a try against the account: raise unless it worked."""
        if row is None:
            raise AuthError(WRONG_LOGIN)
        with self._lock:
            if row["locked_until"] > self._clock():
                raise AuthError(LOCKED, 429)
            if not ok:
                fails = row["failed_logins"] + 1
                locked = self._clock() + LOCK_S if fails >= MAX_FAILS else 0
                self.conn.execute("UPDATE users SET failed_logins = ?, locked_until = ? "
                                  "WHERE id = ?", (0 if locked else fails, locked, row["id"]))
                self.conn.commit()
                raise AuthError(LOCKED if locked else WRONG_LOGIN, 429 if locked else 400)

    def password_sign_in(self, username: str, password: str) -> tuple[dict, str]:
        row = self._account_for(username)
        ok = check_password(password or "", row["password_hash"] if row else None)
        self._check(row, ok)
        with self._lock:
            self.conn.execute("UPDATE users SET failed_logins = 0, locked_until = 0, "
                              "last_seen = ? WHERE id = ?", (_utc_now()[:10], row["id"]))
            token = self._new_session(row["id"])
            self.conn.commit()
        return self._user(row), token

    def recover(self, username: str, code: str, new_password: str) -> tuple[dict, str, str]:
        """Set a new password with the recovery code: (user, token, new recovery code).
        Every other sign-in of the account ends, and the old code stops working."""
        row = self._account_for(username)
        ok = bool(row) and bool(row["recovery_hash"]) and hmac.compare_digest(
            row["recovery_hash"], self._recovery_hash(code))
        self._check(row, ok)
        problem = password_problem(new_password, row["username"])
        if problem:
            raise AuthError(problem, 422)
        pw_hash, new_code = hash_password(new_password), new_recovery_code()
        with self._lock:
            self.conn.execute(
                "UPDATE users SET password_hash = ?, recovery_hash = ?, failed_logins = 0, "
                "locked_until = 0, last_seen = ? WHERE id = ?",
                (pw_hash, self._recovery_hash(new_code), _utc_now()[:10], row["id"]))
            self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
            token = self._new_session(row["id"])
            self.conn.commit()
        return self._user(row), token, new_code

    def change_password(self, user_id: int, current: str, new: str,
                        keep_token: str | None) -> None:
        """Change a signed-in account's password; its other sign-ins end."""
        with self._lock:
            row = self.conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None or not row["password_hash"]:
            raise AuthError("This account signs in by email and has no password.", 422)
        self._check(row, check_password(current or "", row["password_hash"]))
        problem = password_problem(new, row["username"])
        if problem:
            raise AuthError(problem, 422)
        pw_hash = hash_password(new)
        with self._lock:
            self.conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                              (pw_hash, user_id))
            self.conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                              (user_id, _sha(keep_token or "")))
            self.conn.commit()

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
            token = self._new_session(user_id)
            self.conn.commit()
            username = row["username"] if row is not None else None
        return {"id": user_id, "email": email, "username": username}, token, created

    def user_for_token(self, token: str | None) -> dict | None:
        """The signed-in user, or None. Using a session keeps it alive for 90 days."""
        if not token or len(token) > 100:
            return None
        with self._lock:
            row = self.conn.execute(
                "SELECT u.id, u.email, u.username, u.last_seen, s.expires_at FROM sessions s "
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
        return self._user(row)

    def sign_out(self, token: str | None) -> None:
        if token:
            with self._lock:
                self.conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_sha(token),))
                self.conn.commit()

    def delete_user(self, user_id: int) -> None:
        """The account, its places and its sessions."""
        with self._lock:
            for table, col in (("places", "user_id"), ("plan_stops", "user_id"),
                               ("sessions", "user_id"), ("users", "id")):
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

    # -- the trip plan -----------------------------------------------------------------
    _STOP = ("id", "day", "arrive_by", "leave_at", "name", "lat", "lon", "address",
             "google_place_id", "notes")

    def plan(self, user_id: int, day: str | None = None) -> list[dict]:
        """Stops in order: by day, then by the first time given, then as added."""
        sql = "SELECT * FROM plan_stops WHERE user_id = ?"
        args: tuple = (user_id,)
        if day:
            sql, args = sql + " AND day = ?", (user_id, day)
        with self._lock:
            rows = self.conn.execute(
                sql + " ORDER BY day, COALESCE(arrive_by, leave_at, '99:99'), id", args).fetchall()
        return [{k: r[k] for k in self._STOP} for r in rows]

    @staticmethod
    def _stop_values(s: PlanStop) -> tuple:
        name = " ".join((s.name or "").split())[:80]
        if not name:
            raise PlaceError("Please give the stop a name.")
        for t in (s.arrive_by, s.leave_at):
            if t and not HHMM.match(t):
                raise PlaceError("Times must be 24-hour HH:MM, like 09:30 or 14:00.")
        if s.arrive_by and s.leave_at and s.leave_at < s.arrive_by:
            raise PlaceError("The time to leave is before the time to arrive.")
        clean = lambda v, n: " ".join((v or "").split())[:n] or None  # noqa: E731
        return (s.day.isoformat(), s.arrive_by or None, s.leave_at or None, name,
                round(s.lat, 6), round(s.lon, 6), clean(s.address, 200),
                clean(s.google_place_id, 300), clean(s.notes, 300))

    def add_stop(self, user_id: int, s: PlanStop) -> dict:
        values = self._stop_values(s)
        now = _utc_now()
        with self._lock:
            (n,) = self.conn.execute("SELECT COUNT(*) FROM plan_stops WHERE user_id = ?",
                                     (user_id,)).fetchone()
            if n >= MAX_PLAN_STOPS:
                raise PlaceError(f"A plan can have up to {MAX_PLAN_STOPS} stops. "
                                 "Remove some to add more.")
            cur = self.conn.execute(
                "INSERT INTO plan_stops (user_id, day, arrive_by, leave_at, name, lat, lon, "
                "address, google_place_id, notes, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (user_id, *values, now, now))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM plan_stops WHERE id = ?",
                                    (cur.lastrowid,)).fetchone()
        return {k: row[k] for k in self._STOP}

    def update_stop(self, user_id: int, stop_id: int, **changes) -> dict | None:
        """Change a stop's day (a date), times or notes; "" clears a time or note."""
        with self._lock:
            row = self.conn.execute("SELECT * FROM plan_stops WHERE id = ? AND user_id = ?",
                                    (stop_id, user_id)).fetchone()
        if row is None:
            return None
        cur = {k: row[k] for k in self._STOP}
        day = changes.get("day") or date.fromisoformat(cur["day"])
        merged = PlanStop(day=day, name=cur["name"], lat=cur["lat"], lon=cur["lon"],
                          address=cur["address"], google_place_id=cur["google_place_id"],
                          **{k: (changes[k] if changes.get(k) is not None else cur[k]) or None
                             for k in ("arrive_by", "leave_at", "notes")})
        values = self._stop_values(merged)
        with self._lock:
            self.conn.execute(
                "UPDATE plan_stops SET day = ?, arrive_by = ?, leave_at = ?, name = ?, lat = ?, "
                "lon = ?, address = ?, google_place_id = ?, notes = ?, updated_at = ? "
                "WHERE id = ? AND user_id = ?", (*values, _utc_now(), stop_id, user_id))
            self.conn.commit()
            row = self.conn.execute("SELECT * FROM plan_stops WHERE id = ?", (stop_id,)).fetchone()
        return {k: row[k] for k in self._STOP}

    def delete_stop(self, user_id: int, stop_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM plan_stops WHERE id = ? AND user_id = ?",
                                    (stop_id, user_id))
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
                self.conn.execute("DELETE FROM plan_stops WHERE user_id = ?", (user_id,))
                self.conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
                self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            self.conn.commit()
        return len(stale)
