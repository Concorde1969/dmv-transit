from __future__ import annotations

from datetime import datetime

from transit_mcp.models import Chunk, Document


def _doc(url="http://x/a", cat="fares", h="h1"):
    return Document(
        url=url, category=cat, title="Fares", body_markdown="body",
        raw_hash=h, retrieved_at=datetime.now(),
    )


def test_fts_insert_and_match(tmp_db):
    sid, changed = tmp_db.upsert_source(
        url="http://x/a", source_type="web", category="fares",
        http_status=200, content_hash="h1",
    )
    assert changed is True
    tmp_db.replace_document(
        source_id=sid,
        document=_doc(),
        chunks=[Chunk(chunk_index=0, heading_path="Fares", text="pay with your phone using SmarTrip")],
    )
    tmp_db.commit()

    rows = tmp_db.conn.execute(
        "SELECT dc.text FROM chunks_fts f JOIN document_chunks dc ON dc.id = f.rowid "
        "WHERE chunks_fts MATCH 'phone'"
    ).fetchall()
    assert [r["text"] for r in rows] == ["pay with your phone using SmarTrip"]


def test_fts_update_and_delete_stay_in_sync(tmp_db):
    sid, _ = tmp_db.upsert_source(
        url="http://x/a", source_type="web", category="fares",
        http_status=200, content_hash="h1",
    )
    tmp_db.replace_document(
        source_id=sid, document=_doc(),
        chunks=[Chunk(chunk_index=0, heading_path="Fares", text="old bicycle text")],
    )
    tmp_db.commit()

    # Re-ingest replaces the document -> old chunk deleted, new chunk inserted.
    tmp_db.replace_document(
        source_id=sid, document=_doc(h="h2"),
        chunks=[Chunk(chunk_index=0, heading_path="Fares", text="new escalator text")],
    )
    tmp_db.commit()

    assert tmp_db.conn.execute(
        "SELECT count(*) n FROM chunks_fts WHERE chunks_fts MATCH 'bicycle'"
    ).fetchone()["n"] == 0
    assert tmp_db.conn.execute(
        "SELECT count(*) n FROM chunks_fts WHERE chunks_fts MATCH 'escalator'"
    ).fetchone()["n"] == 1

    # Deleting the source cascades to documents, chunks, and the FTS index.
    tmp_db.conn.execute("DELETE FROM sources WHERE id = ?", (sid,))
    tmp_db.commit()
    assert tmp_db.conn.execute("SELECT count(*) n FROM document_chunks").fetchone()["n"] == 0
    assert tmp_db.conn.execute(
        "SELECT count(*) n FROM chunks_fts WHERE chunks_fts MATCH 'escalator'"
    ).fetchone()["n"] == 0


def test_upsert_source_change_detection(tmp_db):
    sid1, c1 = tmp_db.upsert_source(
        url="http://x/a", source_type="web", category="fares",
        http_status=200, content_hash="h1",
    )
    sid2, c2 = tmp_db.upsert_source(
        url="http://x/a", source_type="web", category="fares",
        http_status=200, content_hash="h1",
    )
    sid3, c3 = tmp_db.upsert_source(
        url="http://x/a", source_type="web", category="fares",
        http_status=200, content_hash="h2",
    )
    assert sid1 == sid2 == sid3
    assert (c1, c2, c3) == (True, False, True)
