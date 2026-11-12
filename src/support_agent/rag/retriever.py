"""Vector retrieval over pgvector (HNSW, cosine)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.telemetry import get_tracer
from support_agent.db.models import Chunk, Document
from support_agent.providers.base import EmbeddingProvider

logger = logging.getLogger(__name__)
tracer = get_tracer()


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    chunk_id: UUID
    document_id: UUID
    source: str
    section: str
    content: str
    score: float

    @property
    def citation_id(self) -> str:
        return str(self.chunk_id)


async def search_documents(
    session: AsyncSession,
    *,
    query: str,
    embedder: EmbeddingProvider,
    top_k: int,
    min_score: float,
    viewer_id: str,
) -> list[RetrievedChunk]:
    """Return the top-k chunks above ``min_score`` (score = 1 - cosine distance).

    Only chunks from documents the viewer owns or that are public are searchable;
    another user's private document is invisible at the SQL layer.
    """
    with tracer.start_as_current_span("retrieval.search") as span:
        span.set_attribute("retrieval.top_k", top_k)
        span.set_attribute("retrieval.min_score", min_score)

        vectors = await embedder.embed([query])
        if not vectors:  # pragma: no cover - providers always return one vector
            return []
        query_vector = vectors[0]

        distance = Chunk.embedding.cosine_distance(query_vector).label("distance")
        stmt = (
            select(Chunk, Document.filename, distance)
            .join(Document, Chunk.document_id == Document.id)
            .where(or_(Document.visibility == "public", Document.owner_id == viewer_id))
            .where(Chunk.embedding.cosine_distance(query_vector) <= 1.0 - min_score)
            .order_by(distance)
            .limit(top_k)
        )
        rows = (await session.execute(stmt)).all()

        results = [
            RetrievedChunk(
                chunk_id=chunk.id,
                document_id=chunk.document_id,
                source=filename,
                section=chunk.heading or "document",
                content=chunk.content,
                score=max(0.0, min(1.0, 1.0 - float(dist))),
            )
            for chunk, filename, dist in rows
        ]
        span.set_attribute("retrieval.result_count", len(results))
        span.set_attribute("retrieval.chunk_ids", ",".join(r.citation_id for r in results)[:1000])
        if results:
            span.set_attribute("retrieval.top_score", round(results[0].score, 4))
        return results
