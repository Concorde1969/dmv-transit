"""Protection for a public chat endpoint: rate limits, a site-wide daily cap, and
bounded conversation storage.

Everything is in-memory and per-process, which is right for the single-instance
deployment this app targets. The authoritative cost ceiling is still the monthly spend
limit set in the Anthropic Console; these guards keep any one visitor (or a bot) from
reaching it.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

MINUTE = 60.0
DAY = 86_400.0


class RateLimiter:
    """Sliding-window limits per key (visitor IP): N per minute and M per day."""

    def __init__(self, per_minute: int, per_day: int, *, clock=time.monotonic,
                 max_keys: int = 50_000):
        self.per_minute = per_minute
        self.per_day = per_day
        self._clock = clock
        self._max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def check(self, key: str) -> str | None:
        """Record a hit for ``key``. Returns None if allowed, else 'minute' or 'day'."""
        now = self._clock()
        hits = self._hits.get(key)
        if hits is None:
            hits = self._hits[key] = deque()
        self._hits.move_to_end(key)
        while hits and now - hits[0] > DAY:
            hits.popleft()
        if self.per_day and len(hits) >= self.per_day:
            return "day"
        if self.per_minute and sum(1 for t in hits if now - t <= MINUTE) >= self.per_minute:
            return "minute"
        hits.append(now)
        while len(self._hits) > self._max_keys:  # bound memory under a flood of IPs
            self._hits.popitem(last=False)
        return None


class DailyCap:
    """Site-wide count of chat messages that resets at local midnight."""

    def __init__(self, limit: int, tz: str = "America/New_York", *, now=None):
        self.limit = limit
        self._tz = ZoneInfo(tz)
        self._now = now or (lambda: datetime.now(self._tz))
        self._day = None
        self.count = 0

    def try_take(self) -> bool:
        today = self._now().date()
        if today != self._day:
            self._day, self.count = today, 0
        if self.limit and self.count >= self.limit:
            return False
        self.count += 1
        return True


@dataclass
class Session:
    messages: list[dict] = field(default_factory=list)
    turns: int = 0
    last_used: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def _is_user_turn_start(msg: dict) -> bool:
    """A real question from the rider (not a tool_result carrier message)."""
    return msg.get("role") == "user" and isinstance(msg.get("content"), str)


def trim_history(messages: list[dict], keep_turns: int) -> list[dict]:
    """Keep only the last ``keep_turns`` rider turns. Cuts happen only at the start
    of a rider turn, so a tool_use is never separated from its tool_result."""
    if keep_turns <= 0:
        return messages
    starts = [i for i, m in enumerate(messages) if _is_user_turn_start(m)]
    if len(starts) <= keep_turns:
        return messages
    return messages[starts[-keep_turns]:]


class SessionStore:
    """Conversations keyed by the browser's session id, with idle expiry and a
    maximum count (least-recently-used are dropped first)."""

    def __init__(self, *, ttl_seconds: float, max_sessions: int, clock=time.monotonic):
        self.ttl = ttl_seconds
        self.max_sessions = max_sessions
        self._clock = clock
        self._sessions: OrderedDict[str, Session] = OrderedDict()

    def __len__(self) -> int:
        return len(self._sessions)

    def _expire(self) -> None:
        now = self._clock()
        stale = [k for k, s in self._sessions.items()
                 if now - s.last_used > self.ttl and not s.lock.locked()]
        for k in stale:
            del self._sessions[k]
        while len(self._sessions) > self.max_sessions:
            oldest = next(iter(self._sessions))
            if self._sessions[oldest].lock.locked():
                break
            del self._sessions[oldest]

    def get(self, session_id: str) -> Session:
        self._expire()
        sess = self._sessions.get(session_id)
        if sess is None:
            sess = self._sessions[session_id] = Session()
        self._sessions.move_to_end(session_id)
        sess.last_used = self._clock()
        self._expire()  # enforce the cap after adding
        return sess

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)


# Plain-language messages shown to riders.
MESSAGES = {
    "too_long": "That message is a little long. Please shorten your question to under "
                "{limit} characters and try again.",
    "minute": "You've asked several questions very quickly. Please wait about a minute, "
              "then try again.",
    "day": "You've reached today's limit of questions from this device. Please come back "
           "tomorrow — thank you for understanding.",
    "site_busy": "The assistant has answered as many questions as it can for today. "
                 "Please try again tomorrow. For official information, visit the "
                 "transit agency's own website (for Metro, wmata.com) — this site is "
                 "an unofficial tool and is not affiliated with any agency.",
    "busy_session": "I'm still working on your last question. Please wait for that "
                    "answer first.",
    "max_turns": "This conversation has gotten long. Please press \"Start a new "
                 "question\" at the top to begin a fresh one.",
    "unavailable": "Sorry — the assistant isn't available right now. Please try again "
                   "in a few minutes.",
    "failed": "Sorry — something went wrong while finding your answer. Please try "
              "asking again.",
    "empty": "Please type a question first.",
}
