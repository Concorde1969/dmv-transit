"""Chat log: every question, answer and its diagnostics, kept for review.

Stored in its own SQLite file (``CHATLOG_DB``), separate from the rebuildable transit
database. Visitors are identified only by an anonymous ID — a keyed hash of their IP
address — never the address itself. Email addresses, phone numbers and long digit runs
are removed from questions and answers before they are written (``CHATLOG_REDACT``).
Rows older than ``CHATLOG_RETENTION_DAYS`` are deleted by :meth:`ChatLog.prune`.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import re
import secrets
import sqlite3
import threading
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("transit_mcp.chatlog")

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (
    id          TEXT PRIMARY KEY,
    visitor_id  TEXT NOT NULL,
    device      TEXT,
    started_at  TEXT NOT NULL,
    last_at     TEXT NOT NULL,
    turns       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS turns (
    id                 INTEGER PRIMARY KEY,
    conversation_id    TEXT NOT NULL,
    visitor_id         TEXT NOT NULL,
    ts                 TEXT NOT NULL,
    question           TEXT NOT NULL,
    answer             TEXT,
    outcome            TEXT NOT NULL,   -- ok | error | why it was refused
    error              TEXT,
    tools_json         TEXT NOT NULL DEFAULT '[]',
    model              TEXT,
    input_tokens       INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms         INTEGER,
    first_text_ms      INTEGER,
    nudged             INTEGER NOT NULL DEFAULT 0
);
-- Map trip searches. Coordinates are rounded to 3 decimals (~100 m); typed addresses
-- are not stored.
CREATE TABLE IF NOT EXISTS trip_searches (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    visitor_id  TEXT NOT NULL,
    origin_lat  REAL, origin_lon REAL,
    dest_lat    REAL, dest_lon   REAL,
    n_options   INTEGER NOT NULL,
    live_checks INTEGER NOT NULL DEFAULT 0,
    latency_ms  INTEGER,
    notice      TEXT
);
CREATE INDEX IF NOT EXISTS idx_trip_searches_ts ON trip_searches(ts);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts);
CREATE INDEX IF NOT EXISTS idx_turns_conversation ON turns(conversation_id);
CREATE INDEX IF NOT EXISTS idx_turns_visitor ON turns(visitor_id);
CREATE INDEX IF NOT EXISTS idx_conversations_last ON conversations(last_at);
"""

# --- redaction --------------------------------------------------------------------
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?<!\w)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?!\w)")
_DIGITS = re.compile(r"(?<!\w)\d(?:[\s-]?\d){8,}(?!\w)")  # 9+ digits: card, SSN, account


def redact(text: str | None) -> str | None:
    """Replace email addresses, phone numbers and long digit runs."""
    if not text:
        return text
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    return _DIGITS.sub("[number]", text)


def device_class(user_agent: str | None) -> str:
    """Coarse device type — the full User-Agent string is never stored."""
    ua = (user_agent or "").lower()
    if not ua:
        return "unknown"
    if "ipad" in ua or "tablet" in ua or ("android" in ua and "mobile" not in ua):
        return "tablet"
    if "mobi" in ua or "iphone" in ua or "android" in ua:
        return "mobile"
    return "desktop"


def _now() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


@dataclass
class TurnRecord:
    """What happened during one question, filled in while the answer streams."""

    conversation_id: str
    visitor_id: str
    question: str
    device: str = "unknown"
    answer: str = ""
    outcome: str = "ok"
    error: str | None = None
    tools: list[dict] = field(default_factory=list)
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    latency_ms: int | None = None
    first_text_ms: int | None = None
    nudged: bool = False

    def add_usage(self, event: dict) -> None:
        self.input_tokens += int(event.get("input_tokens") or 0)
        self.output_tokens += int(event.get("output_tokens") or 0)
        self.cache_read_tokens += int(event.get("cache_read_tokens") or 0)
        self.cache_write_tokens += int(event.get("cache_write_tokens") or 0)


class ChatLog:
    def __init__(self, path: str | Path, *, salt: str | None = None, redact_text: bool = True):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._redact = redact_text
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._salt = (salt or self._stored_salt()).encode("utf-8")

    @classmethod
    def from_settings(cls, settings) -> ChatLog:
        return cls(settings.chatlog_db, salt=settings.chatlog_salt,
                   redact_text=settings.chatlog_redact)

    def close(self) -> None:
        self.conn.close()

    def _stored_salt(self) -> str:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = 'salt'").fetchone()
            if row:
                return row["value"]
            salt = secrets.token_hex(32)
            self.conn.execute("INSERT INTO meta (key, value) VALUES ('salt', ?)", (salt,))
            self.conn.commit()
            return salt

    def visitor_id(self, ip: str | None) -> str:
        """Anonymous, stable ID for a visitor: a keyed hash, not reversible to the IP."""
        return hmac.new(self._salt, (ip or "unknown").encode("utf-8"),
                        hashlib.sha256).hexdigest()[:16]

    # -- writing -----------------------------------------------------------------------
    def record_turn(self, rec: TurnRecord) -> None:
        clean = redact if self._redact else (lambda t: t)
        now = _now()
        with self._lock:
            self.conn.execute(
                """
                INSERT INTO conversations (id, visitor_id, device, started_at, last_at, turns)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET last_at = excluded.last_at,
                    turns = conversations.turns + excluded.turns
                """,
                (rec.conversation_id, rec.visitor_id, rec.device, now, now,
                 1 if rec.outcome == "ok" else 0),
            )
            self.conn.execute(
                """
                INSERT INTO turns (conversation_id, visitor_id, ts, question, answer,
                    outcome, error, tools_json, model, input_tokens, output_tokens,
                    cache_read_tokens, cache_write_tokens, latency_ms, first_text_ms, nudged)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (rec.conversation_id, rec.visitor_id, now, clean(rec.question),
                 clean(rec.answer) or None, rec.outcome, rec.error,
                 json.dumps(rec.tools, default=str), rec.model, rec.input_tokens,
                 rec.output_tokens, rec.cache_read_tokens, rec.cache_write_tokens,
                 rec.latency_ms, rec.first_text_ms, int(rec.nudged)),
            )
            self.conn.commit()

    def safe_record(self, rec: TurnRecord) -> None:
        """Record a turn; a logging failure must never break the chat."""
        try:
            self.record_turn(rec)
        except Exception:  # noqa: BLE001
            log.exception("could not write the chat log")

    def safe_record_trip(self, *, visitor_id: str, origin: tuple[float, float],
                         destination: tuple[float, float], n_options: int,
                         live_checks: int, latency_ms: int, notice: str | None) -> None:
        """Log a map trip search (coordinates rounded to ~100 m); never raises."""
        try:
            with self._lock:
                self.conn.execute(
                    "INSERT INTO trip_searches (ts, visitor_id, origin_lat, origin_lon, "
                    "dest_lat, dest_lon, n_options, live_checks, latency_ms, notice) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (_now(), visitor_id, round(origin[0], 3), round(origin[1], 3),
                     round(destination[0], 3), round(destination[1], 3), n_options,
                     live_checks, latency_ms, notice))
                self.conn.commit()
        except Exception:  # noqa: BLE001
            log.exception("could not write the trip log")

    # -- housekeeping ------------------------------------------------------------------
    def prune(self, older_than_days: int) -> int:
        """Delete turns (and then-empty conversations) older than the retention period."""
        if older_than_days <= 0:
            return 0
        cutoff = (datetime.now() - timedelta(days=older_than_days)).isoformat(
            sep=" ", timespec="seconds")
        with self._lock:
            n = self.conn.execute("DELETE FROM turns WHERE ts < ?", (cutoff,)).rowcount
            self.conn.execute("DELETE FROM trip_searches WHERE ts < ?", (cutoff,))
            self.conn.execute(
                "DELETE FROM conversations WHERE id NOT IN "
                "(SELECT DISTINCT conversation_id FROM turns)")
            self.conn.commit()
        return n

    def delete(self, *, conversation: str | None = None, visitor: str | None = None) -> int:
        """Remove a conversation or everything from one visitor (removal requests)."""
        if not (conversation or visitor):
            raise ValueError("give a conversation or a visitor ID")
        col, value = ("conversation_id", conversation) if conversation else ("visitor_id",
                                                                             visitor)
        with self._lock:
            n = self.conn.execute(f"DELETE FROM turns WHERE {col} = ?", (value,)).rowcount
            ccol = "id" if conversation else "visitor_id"
            self.conn.execute(f"DELETE FROM conversations WHERE {ccol} = ?", (value,))
            self.conn.commit()
        return n

    # -- reading -----------------------------------------------------------------------
    def rows(self, since: str | None = None, until: str | None = None) -> Iterator[dict]:
        sql = ("SELECT t.*, c.device FROM turns t "
               "LEFT JOIN conversations c ON c.id = t.conversation_id WHERE 1=1")
        params: list = []
        if since:
            sql += " AND t.ts >= ?"
            params.append(since)
        if until:
            sql += " AND t.ts < ?"
            params.append(until)
        for r in self.conn.execute(sql + " ORDER BY t.ts, t.id", params):
            row = dict(r)
            row["tools"] = json.loads(row.pop("tools_json") or "[]")
            yield row

    def export_jsonl(self, since=None, until=None) -> str:
        return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in self.rows(since, until))

    def export_csv(self, since=None, until=None) -> str:
        cols = ["ts", "conversation_id", "visitor_id", "device", "question", "answer",
                "outcome", "tools_used", "input_tokens", "output_tokens", "seconds", "error"]
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore", lineterminator="\n")
        w.writeheader()
        for r in self.rows(since, until):
            r["tools_used"] = ", ".join(t.get("name", "?") for t in r["tools"])
            r["seconds"] = round(r["latency_ms"] / 1000, 1) if r["latency_ms"] else ""
            w.writerow(r)
        return buf.getvalue()

    def stats(self, days: int = 7) -> dict:
        since = (datetime.now() - timedelta(days=days)).isoformat(sep=" ", timespec="seconds")
        rows = list(self.rows(since=since))
        answered = [r for r in rows if r["outcome"] == "ok"]
        tools = Counter(t.get("name") for r in answered for t in r["tools"])
        latencies = sorted(r["latency_ms"] for r in answered if r["latency_ms"])
        return {
            "days": days,
            "questions": len(rows),
            "answered": len(answered),
            "errors": sum(r["outcome"] == "error" for r in rows),
            "refused": dict(Counter(r["outcome"] for r in rows
                                    if r["outcome"] not in ("ok", "error"))),
            "conversations": len({r["conversation_id"] for r in rows}),
            "visitors": len({r["visitor_id"] for r in rows}),
            "input_tokens": sum(r["input_tokens"] for r in rows),
            "output_tokens": sum(r["output_tokens"] for r in rows),
            "median_seconds": (round(latencies[len(latencies) // 2] / 1000, 1)
                               if latencies else None),
            "top_tools": dict(tools.most_common(8)),
            "grounding_nudges": sum(r["nudged"] for r in rows),
        }
