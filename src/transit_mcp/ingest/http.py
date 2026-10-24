"""Shared HTTP client: rate limiting, retry/backoff, conditional GET, disk cache.

Used by both the WMATA API client and the web scraper.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class RetryableHTTPStatus(Exception):
    """Raised for 429/5xx so tenacity retries the request."""


@dataclass
class RawResponse:
    url: str
    status_code: int
    text: str
    headers: dict[str, str] = field(default_factory=dict)
    from_cache: bool = False
    not_modified: bool = False

    @property
    def etag(self) -> str | None:
        return self.headers.get("etag")

    @property
    def last_modified(self) -> str | None:
        return self.headers.get("last-modified")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class _RateLimiter:
    def __init__(self, min_interval_seconds: float):
        self._min = max(0.0, min_interval_seconds)
        self._last = 0.0

    def wait(self) -> None:
        if self._min <= 0:
            return
        elapsed = time.monotonic() - self._last
        if elapsed < self._min:
            time.sleep(self._min - elapsed)
        self._last = time.monotonic()


class HttpClient:
    """A small wrapper around httpx.Client with politeness + caching built in."""

    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 30.0,
        min_interval_seconds: float = 0.0,
        cache_dir: str | Path | None = None,
        default_headers: dict[str, str] | None = None,
    ):
        headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        if default_headers:
            headers.update(default_headers)
        self._client = httpx.Client(
            headers=headers, timeout=timeout, follow_redirects=True
        )
        self._limiter = _RateLimiter(min_interval_seconds)
        self._cache_dir = Path(cache_dir) if cache_dir else None
        if self._cache_dir:
            self._cache_dir.mkdir(parents=True, exist_ok=True)

    # -- cache helpers ----------------------------------------------------
    def _cache_path(self, url: str) -> Path | None:
        if not self._cache_dir:
            return None
        digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
        return self._cache_dir / f"{digest}.json"

    def load_cache(self, url: str) -> dict | None:
        path = self._cache_path(url)
        if path and path.exists():
            try:
                return json.loads(path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                return None
        return None

    def _store_cache(self, url: str, resp: RawResponse) -> None:
        path = self._cache_path(url)
        if not path:
            return
        path.write_text(
            json.dumps(
                {
                    "url": url,
                    "status_code": resp.status_code,
                    "headers": resp.headers,
                    "text": resp.text,
                    "cached_at": time.time(),
                }
            ),
            "utf-8",
        )

    # -- fetch ----------------------------------------------------------
    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        conditional: bool = True,
        headers: dict[str, str] | None = None,
    ) -> RawResponse:
        """GET ``url`` with rate limiting, retry, and conditional-GET support.

        On HTTP 304 the body is served from the disk cache and
        ``not_modified=True``. Raises ``httpx.HTTPStatusError`` for non-retryable
        4xx responses. ``headers`` apply to this request only (use them for API
        keys, so a key is never sent to another host).
        """
        cached = self.load_cache(url) if conditional else None
        req_headers: dict[str, str] = dict(headers or {})
        if cached:
            etag = cached.get("headers", {}).get("etag")
            last_mod = cached.get("headers", {}).get("last-modified")
            if etag:
                req_headers["If-None-Match"] = etag
            if last_mod:
                req_headers["If-Modified-Since"] = last_mod

        resp = self._get_with_retry(url, params, req_headers)

        if resp.status_code == 304 and cached:
            return RawResponse(
                url=url,
                status_code=200,
                text=cached["text"],
                headers=cached.get("headers", {}),
                from_cache=True,
                not_modified=True,
            )

        raw = RawResponse(
            url=url,
            status_code=resp.status_code,
            text=resp.text,
            headers={k.lower(): v for k, v in resp.headers.items()},
        )
        if raw.ok:
            self._store_cache(url, raw)
        else:
            resp.raise_for_status()
        return raw

    @retry(
        retry=retry_if_exception_type((RetryableHTTPStatus, httpx.TransportError)),
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        reraise=True,
    )
    def _get_with_retry(
        self, url: str, params: dict[str, str] | None, headers: dict[str, str]
    ) -> httpx.Response:
        self._limiter.wait()
        resp = self._client.get(url, params=params, headers=headers)
        if resp.status_code in RETRYABLE_STATUS:
            raise RetryableHTTPStatus(f"{resp.status_code} for {url}")
        return resp

    def get_bytes(
        self, url: str, *, headers: dict[str, str] | None = None
    ) -> tuple[int, bytes, dict[str, str]]:
        """Fetch a binary resource (e.g. a GTFS zip). No text cache / conditional GET.

        Returns ``(status_code, content, headers)``; raises ``httpx.HTTPStatusError``
        for a non-retryable 4xx. ``headers`` apply to this request only.
        """
        resp = self._get_with_retry(url, None, dict(headers or {}))
        if not (200 <= resp.status_code < 300):
            resp.raise_for_status()
        return (
            resp.status_code,
            resp.content,
            {k.lower(): v for k, v in resp.headers.items()},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
