"""Config-driven scraper: fetch a curated page list (+ one hop of children) into
documents + chunks.

Used by the ``web_pages`` source for any agency. Deliberately an allow-list, not an
open crawl: only listed pages are fetched, and "hub" pages may fan out one hop to
child links that pass :class:`CrawlRules`.
"""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urljoin, urlparse

import trafilatura
from bs4 import BeautifulSoup

from transit_mcp.db.database import Database
from transit_mcp.ingest.chunker import chunk_markdown
from transit_mcp.ingest.http import HttpClient
from transit_mcp.models import Category, Document

MIN_BODY_CHARS = 200


@dataclass(frozen=True)
class PageSpec:
    url: str
    category: Category
    follow_children: bool = False


@dataclass(frozen=True)
class CrawlRules:
    """Which child links a hub page may fan out to, and how to categorize them."""

    base_url: str | None = None
    child_prefixes: tuple[str, ...] = ()
    child_suffix: str | None = None
    child_blocklist: frozenset[str] = frozenset()
    skip_path_segments: frozenset[str] = frozenset()
    # (path prefix, category) pairs; first match wins.
    child_categories: tuple[tuple[str, Category], ...] = ()

    def absolute(self, url: str) -> str:
        if url.startswith("/") and self.base_url:
            return self.base_url.rstrip("/") + url
        return url

    def allowed_hosts(self, page_url: str) -> set[str]:
        host = urlparse(self.base_url or page_url).netloc.lower()
        bare = host.removeprefix("www.")
        return {host, bare, f"www.{bare}"}

    def category_for(self, path: str, default: Category) -> Category:
        for prefix, category in self.child_categories:
            if path.startswith(prefix):
                return category
        return default

    def eligible_child(self, path: str) -> bool:
        first = path.strip("/").split("/", 1)[0]
        if first in self.skip_path_segments or path in self.child_blocklist:
            return False
        if self.child_suffix and not path.endswith(self.child_suffix):
            return False
        return not self.child_prefixes or path.startswith(self.child_prefixes)


def _extract_title(html: str, fallback_url: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return h1.get_text(strip=True)
    if soup.title and soup.title.get_text(strip=True):
        return soup.title.get_text(strip=True).split("|")[0].strip()
    return urlparse(fallback_url).path.rsplit("/", 1)[-1].replace("-", " ").title()


def _extract_markdown(html: str) -> str:
    md = trafilatura.extract(
        html,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        include_links=False,
        favor_precision=True,
    )
    return (md or "").strip()


def _child_links(html: str, page_url: str, rules: CrawlRules) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    hosts = rules.allowed_hosts(page_url)
    origin = rules.base_url or f"{urlparse(page_url).scheme}://{urlparse(page_url).netloc}"
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        parts = urlparse(urljoin(page_url, a["href"]))
        if parts.netloc.lower() not in hosts or not rules.eligible_child(parts.path):
            continue
        clean = origin.rstrip("/") + parts.path
        if clean not in seen:
            seen.add(clean)
            out.append(clean)
    return out


def scrape_pages(
    db: Database,
    http: HttpClient,
    pages: list[PageSpec],
    *,
    rules: CrawlRules | None = None,
    feed_id: str | None = None,
    max_pages: int = 60,
    force: bool = False,
) -> dict[str, int]:
    """Fetch the listed pages (+ one hop of children from hub pages) and upsert
    documents tagged with ``feed_id``.

    Returns counters: fetched, unchanged, updated, skipped_thin, errors,
    children_found.
    """
    rules = rules or CrawlRules()
    stats = {
        "fetched": 0, "unchanged": 0, "updated": 0,
        "skipped_thin": 0, "errors": 0, "children_found": 0,
    }

    queue: deque[tuple[str, Category, bool]] = deque(
        (rules.absolute(p.url), p.category, p.follow_children) for p in pages
    )
    queued: set[str] = {u for u, _, _ in queue}

    while queue and stats["fetched"] + stats["errors"] < max_pages:
        url, category, follow = queue.popleft()
        try:
            resp = http.get(url, conditional=True)
        except Exception as exc:  # noqa: BLE001 - record and continue
            stats["errors"] += 1
            db.upsert_source(
                url=url, source_type="web", category=category, feed_id=feed_id,
                http_status=getattr(getattr(exc, "response", None), "status_code", None),
                content_hash=None,
            )
            db.commit()
            continue

        stats["fetched"] += 1
        html = resp.text
        markdown = _extract_markdown(html)
        title = _extract_title(html, url)
        raw_hash = hashlib.sha256((title + "\n\n" + markdown).encode("utf-8")).hexdigest()

        source_id, changed = db.upsert_source(
            url=url, source_type="web", category=category, feed_id=feed_id,
            http_status=resp.status_code, content_hash=raw_hash,
            etag=resp.etag, last_modified=resp.last_modified,
        )

        if len(markdown) < MIN_BODY_CHARS:
            stats["skipped_thin"] += 1
        elif changed or force:
            document = Document(
                url=url, category=category, feed_id=feed_id, title=title,
                summary=markdown[:280].replace("\n", " ").strip() or None,
                body_markdown=markdown, raw_hash=raw_hash, retrieved_at=datetime.now(),
            )
            db.replace_document(
                source_id=source_id, document=document,
                chunks=chunk_markdown(markdown, doc_title=title),
            )
            stats["updated"] += 1
        else:
            stats["unchanged"] += 1

        if follow:
            for child in _child_links(html, url, rules):
                if child not in queued and len(queued) < max_pages:
                    queued.add(child)
                    queue.append(
                        (child, rules.category_for(urlparse(child).path, category), False)
                    )
                    stats["children_found"] += 1

        db.commit()

    return stats
