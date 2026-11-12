"""Markdown-aware chunking: keep headings as metadata, split on paragraph
boundaries, fall back to hard character splits for giant paragraphs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$", re.MULTILINE)
_PARAGRAPH_RE = re.compile(r"\n\s*\n")


@dataclass(frozen=True, slots=True)
class TextChunk:
    index: int
    heading: str | None
    content: str


def chunk_text(text: str, *, size: int, overlap: int) -> list[TextChunk]:
    if size <= 0:
        raise ValueError("size must be positive")
    if not 0 <= overlap < size:
        raise ValueError("overlap must be >= 0 and < size")

    normalized = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        return []

    chunks: list[TextChunk] = []
    for heading, body in _sections(normalized):
        for window in _windows(body, size, overlap):
            cleaned = window.strip()
            if cleaned:
                chunks.append(TextChunk(index=len(chunks), heading=heading, content=cleaned))
    return chunks


def _sections(text: str) -> list[tuple[str | None, str]]:
    """Split on markdown headings; text before the first heading is one section."""
    matches = list(_HEADING_RE.finditer(text))
    if not matches:
        return [(None, text)]

    sections: list[tuple[str | None, str]] = []
    preamble = text[: matches[0].start()].strip()
    if preamble:
        sections.append((None, preamble))
    for i, match in enumerate(matches):
        heading = match.group(2).strip()[:255]
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.end() : end].strip()
        if body:
            sections.append((heading, body))
    return sections


def _windows(text: str, size: int, overlap: int) -> list[str]:
    paragraphs: list[str] = []
    for paragraph in _PARAGRAPH_RE.split(text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) <= size:
            paragraphs.append(paragraph)
        else:
            paragraphs.extend(_hard_split(paragraph, size, overlap))

    windows: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= size:
            current = candidate
            continue
        if current:
            windows.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n\n{paragraph}".strip() if tail else paragraph
        else:
            current = paragraph
        # A tail plus a long paragraph can still exceed the budget, and the
        # remainder after a slice can stay oversized when overlap > size/2.
        while len(current) > size:
            windows.append(current[:size])
            current = current[size - overlap :]
    if current.strip():
        windows.append(current)
    return windows


def _hard_split(text: str, size: int, overlap: int) -> list[str]:
    pieces: list[str] = []
    start = 0
    while start < len(text):
        end = start + size
        pieces.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return pieces
