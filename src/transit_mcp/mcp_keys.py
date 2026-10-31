"""API keys for the MCP server's web address (``server/http.py``).

Each person or application gets their own key, created with ``transit-mcp mcp-keys add``
and shown once; only its hash is stored, in its own SQLite file (``MCP_KEYS_DB``). A key
can be revoked at any time, without a restart. Nothing about the calls is kept except a
count and the last time the key was used.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path

PREFIX = "dmvt_"

SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS mcp_keys (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,             -- who it was given to
    key_hash    TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    last_used   TEXT,
    calls       INTEGER NOT NULL DEFAULT 0,
    revoked_at  TEXT
);
"""


def _sha(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class McpKeys:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    @classmethod
    def from_settings(cls, settings) -> McpKeys:
        return cls(settings.mcp_keys_db)

    def close(self) -> None:
        self.conn.close()

    def add(self, name: str) -> tuple[int, str]:
        """A new key for ``name``: (its id, the key). The key can't be shown again."""
        name = " ".join((name or "").split())[:80]
        if not name:
            raise ValueError("give the key a name: who it's for")
        key = PREFIX + secrets.token_urlsafe(32)
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO mcp_keys (name, key_hash, created_at) VALUES (?, ?, ?)",
                (name, _sha(key), _now()))
            self.conn.commit()
        return cur.lastrowid, key

    def check(self, key: str | None) -> dict | None:
        """The key's row if it's valid and not revoked; counts this use."""
        if not key or not key.startswith(PREFIX) or len(key) > 100:
            return None
        h = _sha(key)
        with self._lock:
            row = self.conn.execute(
                "SELECT id, name FROM mcp_keys WHERE key_hash = ? AND revoked_at IS NULL",
                (h,)).fetchone()
            if row is None:
                return None
            self.conn.execute(
                "UPDATE mcp_keys SET calls = calls + 1, last_used = ? WHERE key_hash = ?",
                (_now(), h))
            self.conn.commit()
        return dict(row)

    def revoke(self, key_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE mcp_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (_now(), key_id))
            self.conn.commit()
        return bool(cur.rowcount)

    def list(self) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, name, created_at, last_used, calls, revoked_at FROM mcp_keys "
                "ORDER BY id").fetchall()
        return [dict(r) for r in rows]
