from __future__ import annotations

from transit_mcp.ingest.chunker import MAX_CHARS, chunk_markdown

SAMPLE = """\
# Pay for Metro

Intro paragraph about paying to ride.

## Fares

### Metrorail

Metrorail fares are distance based and range widely.

### Metrobus

Metrobus is a flat fare. Drivers cannot make change.

## Riding with children

Two children under five ride free.
"""


def test_chunks_carry_heading_breadcrumbs():
    chunks = chunk_markdown(SAMPLE, doc_title="Pay for Metro")
    paths = {c.heading_path for c in chunks}
    assert "Pay for Metro > Fares > Metrorail" in paths
    assert "Pay for Metro > Fares > Metrobus" in paths
    assert "Pay for Metro > Riding with children" in paths


def test_heading_path_dedupes_repeated_title():
    # doc_title plus a leading H1 of the same name must not double up.
    chunks = chunk_markdown(SAMPLE, doc_title="Pay for Metro")
    assert all(not c.heading_path.startswith("Pay for Metro > Pay for Metro") for c in chunks)


def test_hard_cap_on_chunk_size():
    big = "# Doc\n\n" + ("word " * 4000)
    chunks = chunk_markdown(big, doc_title="Doc")
    assert chunks
    assert all(len(c.text) <= MAX_CHARS for c in chunks)


def test_indexes_are_sequential():
    chunks = chunk_markdown(SAMPLE, doc_title="Pay for Metro")
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
