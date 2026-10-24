"""Full-text search over document chunks (SQLite FTS5 + bm25 ranking)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from transit_mcp.db.database import Database

_TOKEN_RE = re.compile(r"[0-9a-z']+", re.IGNORECASE)
# FTS5 columns weighted: text matches matter more than title matches.
_BM25_WEIGHTS = "bm25(chunks_fts, 1.0, 0.5)"


@dataclass
class SearchHit:
    title: str
    url: str
    category: str
    heading_path: str
    snippet: str
    retrieved_at: str
    score: float
    chunk_indexes: list[int] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "category": self.category,
            "heading_path": self.heading_path,
            "snippet": self.snippet,
            "retrieved_at": self.retrieved_at,
            "score": round(self.score, 4),
        }


def _match_expr(query: str, *, conjunctive: bool) -> str | None:
    tokens = _TOKEN_RE.findall(query.lower())
    tokens = [t for t in tokens if len(t) > 1]
    if not tokens:
        return None
    quoted = [f'"{t}"' for t in tokens]
    joiner = " AND " if conjunctive else " OR "
    return joiner.join(quoted)


def _run(
    db: Database, match_expr: str, category: str | None, limit: int
) -> list[dict]:
    sql = f"""
        SELECT d.title       AS title,
               d.url         AS url,
               d.category    AS category,
               d.retrieved_at AS retrieved_at,
               dc.heading_path AS heading_path,
               dc.chunk_index  AS chunk_index,
               dc.text        AS text,
               {_BM25_WEIGHTS} AS score
          FROM chunks_fts
          JOIN document_chunks dc ON dc.id = chunks_fts.rowid
          JOIN documents d        ON d.id = dc.document_id
         WHERE chunks_fts MATCH ?
    """
    params: list[object] = [match_expr]
    if category:
        sql += " AND d.category = ?"
        params.append(category)
    sql += " ORDER BY score LIMIT ?"
    params.append(limit * 3)  # over-fetch; we merge per-document below
    return [dict(r) for r in db.conn.execute(sql, params).fetchall()]


def _merge_by_document(rows: list[dict], limit: int) -> list[SearchHit]:
    by_url: dict[str, list[dict]] = {}
    order: list[str] = []
    for r in rows:
        if r["url"] not in by_url:
            by_url[r["url"]] = []
            order.append(r["url"])
        by_url[r["url"]].append(r)

    hits: list[SearchHit] = []
    for url in order:
        group = sorted(by_url[url], key=lambda r: r["chunk_index"])
        best = min(group, key=lambda r: r["score"])
        snippet = "\n\n".join(g["text"] for g in group[:3]).strip()
        hits.append(
            SearchHit(
                title=best["title"],
                url=url,
                category=best["category"],
                heading_path=best["heading_path"],
                snippet=snippet,
                retrieved_at=best["retrieved_at"],
                score=best["score"],
                chunk_indexes=[g["chunk_index"] for g in group],
            )
        )
    hits.sort(key=lambda h: h.score)
    return hits[:limit]


def search(
    db: Database, query: str, *, category: str | None = None, limit: int = 5
) -> list[SearchHit]:
    """Search all narrative content. Tries AND of terms, falls back to OR."""
    for conjunctive in (True, False):
        expr = _match_expr(query, conjunctive=conjunctive)
        if not expr:
            return []
        rows = _run(db, expr, category, limit)
        if rows:
            return _merge_by_document(rows, limit)
    return []
