"""Split a Markdown document into heading-scoped chunks for full-text search."""

from __future__ import annotations

import re

from transit_mcp.models import Chunk

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
TARGET_CHARS = 1200
MAX_CHARS = 1800
MIN_CHARS = 200


def _heading_path(stack: list[str]) -> str:
    parts: list[str] = []
    for h in stack:
        h = h.strip()
        if h and (not parts or parts[-1].lower() != h.lower()):
            parts.append(h)
    return " > ".join(parts)


def chunk_markdown(
    markdown: str, *, doc_title: str, target_chars: int = TARGET_CHARS
) -> list[Chunk]:
    """Break ``markdown`` into chunks, each tagged with its heading breadcrumb.

    A new chunk starts on every heading and whenever the running buffer exceeds
    ``target_chars`` (hard-capped at :data:`MAX_CHARS`). Runs of tiny chunks under
    the same heading are merged.
    """
    lines = markdown.splitlines()
    heading_stack: list[str] = [doc_title.strip()] if doc_title else []
    raw_chunks: list[tuple[str, str]] = []  # (heading_path, text)
    buf: list[str] = []

    def flush() -> None:
        text = "\n".join(buf).strip()
        buf.clear()
        if text:
            raw_chunks.append((_heading_path(heading_stack), text))

    for line in lines:
        m = _HEADING_RE.match(line.strip())
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            base = 1 if doc_title else 0
            del heading_stack[base + level - 1 :]
            heading_stack.append(title)
            continue

        buf.append(line)
        if sum(len(x) for x in buf) >= target_chars and line.strip() == "":
            flush()

    flush()

    # Merge undersized neighbors that share a heading path.
    merged: list[tuple[str, str]] = []
    for hp, text in raw_chunks:
        if (
            merged
            and merged[-1][0] == hp
            and len(merged[-1][1]) < MIN_CHARS
            and len(merged[-1][1]) + len(text) <= MAX_CHARS
        ):
            merged[-1] = (hp, merged[-1][1] + "\n\n" + text)
        else:
            merged.append((hp, text))

    # Hard-split anything still over MAX_CHARS on paragraph boundaries.
    final: list[tuple[str, str]] = []
    for hp, text in merged:
        if len(text) <= MAX_CHARS:
            final.append((hp, text))
            continue
        paras: list[str] = []
        for para in re.split(r"\n\s*\n", text):
            # A single paragraph can still exceed the cap; slice it on whitespace.
            while len(para) > MAX_CHARS:
                cut = para.rfind(" ", 0, MAX_CHARS)
                if cut <= 0:
                    cut = MAX_CHARS
                paras.append(para[:cut])
                para = para[cut:].lstrip()
            paras.append(para)

        cur = ""
        for p in paras:
            if cur and len(cur) + len(p) > MAX_CHARS:
                final.append((hp, cur.strip()))
                cur = p
            else:
                cur = f"{cur}\n\n{p}" if cur else p
        if cur.strip():
            final.append((hp, cur.strip()))

    return [
        Chunk(chunk_index=i, heading_path=hp, text=text)
        for i, (hp, text) in enumerate(final)
        if text.strip()
    ]
