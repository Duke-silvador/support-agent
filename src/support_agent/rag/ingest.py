"""Document ingestion: validate → scan → chunk → embed → store.

The stored text is always the exact uploaded text. Injection signals are
recorded as ``injection_flags`` metadata, never applied as edits.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePath
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.core.security import scan_for_injection
from support_agent.db.models import Chunk, Document
from support_agent.providers.base import EmbeddingProvider
from support_agent.rag.chunker import chunk_text

logger = logging.getLogger(__name__)

ALLOWED_SUFFIXES = frozenset({".md", ".txt"})
Visibility = Literal["private", "public"]
_TITLE_RE = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
_MIN_TEXT_CHARS = 20
_UNSAFE_FILENAME_RE = re.compile(r'[\x00-\x1f\x7f"<>|\\/:*?]+')


def clean_filename(filename: str) -> str:
    """Keep only a safe basename: markers, quotes and control chars never reach
    the prompt fence."""
    name = PurePath(filename.replace("\\", "/")).name
    name = _UNSAFE_FILENAME_RE.sub("_", name).strip(" .")
    return (name or "upload")[:255]


class DocumentRejected(ValueError):
    """Client error while ingesting a document."""

    status_code = 422


class UnsupportedDocument(DocumentRejected):
    status_code = 415


class DocumentTooLarge(DocumentRejected):
    status_code = 413


@dataclass(frozen=True, slots=True)
class IngestResult:
    document_id: UUID
    filename: str
    title: str
    chunk_count: int
    duplicate: bool
    injection_flags: list[str]
    created_at: datetime
    owner_id: str
    visibility: Visibility


async def ingest_document(
    session: AsyncSession,
    *,
    filename: str,
    payload: bytes,
    settings: Settings,
    embedder: EmbeddingProvider,
    owner_id: str,
    visibility: Visibility = "private",
) -> IngestResult:
    """Validate, chunk, embed and store a document.

    Embedding input includes the markdown heading so section titles are
    searchable; the stored chunk content stays byte-for-byte as authored.

    Duplicate detection is scoped to what the caller can already see (their own
    documents plus public ones), so uploading bytes that exist only in another
    user's private document creates an independent copy instead of revealing
    that document's metadata.
    """
    filename = clean_filename(filename)
    suffix = PurePath(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise UnsupportedDocument(f"unsupported file type {suffix or '(none)'}; allowed: .md, .txt")
    if not payload:
        raise DocumentRejected("empty file")
    if len(payload) > settings.max_upload_bytes:
        raise DocumentTooLarge(f"file too large (max {settings.max_upload_bytes} bytes)")
    if b"\x00" in payload:
        raise DocumentRejected("binary content is not accepted; upload UTF-8 .md or .txt")

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DocumentRejected("file must be valid UTF-8 text") from exc

    if len(text.strip()) < _MIN_TEXT_CHARS:
        raise DocumentRejected("document has no meaningful text")

    digest = hashlib.sha256(payload).hexdigest()
    existing = await session.scalar(select(Document).where(*_visible_duplicate(digest, owner_id)))
    if existing is not None:
        return await _duplicate_result(session, existing)

    flags = scan_for_injection(text)
    chunks = chunk_text(text, size=settings.chunk_size_chars, overlap=settings.chunk_overlap_chars)
    if not chunks:
        raise DocumentRejected("document produced no chunks")
    if len(chunks) > settings.max_chunks_per_document:
        raise DocumentTooLarge(
            "document needs too many chunks "
            f"({len(chunks)} > {settings.max_chunks_per_document}); split it up"
        )

    try:
        vectors = await embedder.embed(
            [f"{c.heading}\n\n{c.content}" if c.heading else c.content for c in chunks]
        )
    except Exception as exc:  # provider failure -> 502 at the API layer
        logger.exception("embedding failed", extra={"filename": filename})
        raise EmbeddingUnavailable("embedding provider failed") from exc
    if len(vectors) != len(chunks):
        raise EmbeddingUnavailable("embedding provider returned a wrong number of vectors")

    title = _title_for(text, filename)
    document = Document(
        filename=filename[:255],
        title=title,
        content_type="text/markdown" if suffix == ".md" else "text/plain",
        content_sha256=digest,
        size_bytes=len(payload),
        raw_text=text,
        injection_flags=flags,
        owner_id=owner_id,
        visibility=visibility,
    )
    document.chunks = [
        Chunk(
            chunk_index=c.index,
            heading=c.heading,
            content=c.content,
            content_sha256=hashlib.sha256(c.content.encode("utf-8")).hexdigest(),
            embedding=vector,
        )
        for c, vector in zip(chunks, vectors, strict=True)
    ]

    session.add(document)
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent upload of identical bytes by the same owner: fall back to
        # the winner's row.
        await session.rollback()
        winner = await session.scalar(select(Document).where(*_visible_duplicate(digest, owner_id)))
        if winner is None:  # pragma: no cover - only on a genuine constraint bug
            raise
        return await _duplicate_result(session, winner)

    await session.refresh(document)
    logger.info(
        "document ingested",
        extra={
            "document_id": str(document.id),
            "chunks": len(chunks),
            "injection_flags": flags,
            "owner_id": owner_id,
            "visibility": visibility,
        },
    )
    return IngestResult(
        document_id=document.id,
        filename=document.filename,
        title=document.title,
        chunk_count=len(chunks),
        duplicate=False,
        injection_flags=flags,
        created_at=document.created_at,
        owner_id=document.owner_id,
        visibility=visibility,
    )


def _visible_duplicate(digest: str, owner_id: str) -> tuple[Any, ...]:
    """WHERE clause for "this same owner already uploaded these exact bytes".

    Deliberately *not* widened to public documents: returning another owner's
    row would hand back a document the caller cannot delete and would disappear
    from their corpus if the operator unpublishes it.
    """
    return (Document.content_sha256 == digest, Document.owner_id == owner_id)


async def _duplicate_result(session: AsyncSession, document: Document) -> IngestResult:
    return IngestResult(
        document_id=document.id,
        filename=document.filename,
        title=document.title,
        chunk_count=await _chunk_count(session, document.id),
        duplicate=True,
        injection_flags=_as_flags(document.injection_flags),
        created_at=document.created_at,
        owner_id=document.owner_id,
        visibility="public" if document.visibility == "public" else "private",
    )


class EmbeddingUnavailable(RuntimeError):
    """Upstream provider problem — maps to 502."""


def _title_for(text: str, filename: str) -> str:
    match = _TITLE_RE.search(text)
    if match:
        return match.group(1).strip()[:255]
    return PurePath(filename).stem.replace("_", " ").replace("-", " ").strip()[:255] or filename


def _as_flags(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


async def _chunk_count(session: AsyncSession, document_id: UUID) -> int:
    total = await session.scalar(
        select(func.count()).select_from(Chunk).where(Chunk.document_id == document_id)
    )
    return int(total or 0)
