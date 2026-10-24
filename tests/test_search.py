from __future__ import annotations

from datetime import datetime

from transit_mcp.ingest.chunker import chunk_markdown
from transit_mcp.models import Document
from transit_mcp.retrieval.search import search


def _add(db, url, category, title, body):
    sid, _ = db.upsert_source(
        url=url, source_type="web", category=category,
        http_status=200, content_hash=url,
    )
    doc = Document(
        url=url, category=category, title=title, body_markdown=body,
        raw_hash=url, retrieved_at=datetime.now(),
    )
    db.replace_document(source_id=sid, document=doc, chunks=chunk_markdown(body, doc_title=title))
    db.commit()


def _seed(db):
    _add(
        db, "https://w/pay.html", "payment", "Smart Trip",
        "# Smart Trip\n\nAdd Smart Trip to your phone to tap and pay at the fare gate.\n",
    )
    _add(
        db, "https://w/bikes.html", "new_rider", "Bikes on Metro",
        "# Bikes on Metro\n\nYou may bring a bicycle on Metrorail except during rush hour.\n",
    )
    _add(
        db, "https://w/safety.html", "safety", "Safety",
        "# Safety\n\nIf you see something, say something. Use the emergency intercom on the train.\n",
    )


def test_search_ranks_relevant_doc_first(tmp_db):
    _seed(tmp_db)
    hits = search(tmp_db, "pay with my phone")
    assert hits
    assert hits[0].url == "https://w/pay.html"
    assert hits[0].url.startswith("https://")
    assert hits[0].retrieved_at


def test_category_filter(tmp_db):
    _seed(tmp_db)
    hits = search(tmp_db, "emergency intercom", category="safety")
    assert hits and all(h.category == "safety" for h in hits)
    # The same query without the filter still finds it, but the filter must not leak.
    assert search(tmp_db, "emergency intercom", category="payment") == []


def test_or_fallback_when_and_has_no_hits(tmp_db):
    _seed(tmp_db)
    # "bicycle" only appears in the bikes doc; "helicopter" appears nowhere.
    hits = search(tmp_db, "bicycle helicopter")
    assert hits and hits[0].url == "https://w/bikes.html"


def test_no_results_returns_empty(tmp_db):
    _seed(tmp_db)
    assert search(tmp_db, "zzzznotawordxyz") == []
