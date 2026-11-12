from __future__ import annotations

from uuid import uuid4

from support_agent.agents.verifier import verify_citations
from support_agent.core.constants import REFUSAL_ANSWER
from support_agent.rag.retriever import RetrievedChunk


def _chunk(chunk_id: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,  # type: ignore[arg-type]
        document_id=uuid4(),
        source="refund-policy.md",
        section="Refunds",
        content="30 days",
        score=0.7,
    )


def test_valid_citation_is_kept() -> None:
    chunk = _chunk(str(uuid4()))
    result = verify_citations(
        "Refunds take 30 days.", [chunk.citation_id], {chunk.citation_id: chunk}
    )
    assert result.refused is False
    assert result.citations == [chunk]
    assert result.dropped_ids == []


def test_hallucinated_citation_is_dropped() -> None:
    result = verify_citations("Refunds take 30 days.", ["does-not-exist"], {})
    assert result.refused is True
    assert result.answer == REFUSAL_ANSWER
    assert result.dropped_ids == ["does-not-exist"]


def test_mixed_citations_keep_only_real_ones() -> None:
    chunk = _chunk(str(uuid4()))
    result = verify_citations(
        "Refunds take 30 days.", [chunk.citation_id, "fake-id"], {chunk.citation_id: chunk}
    )
    assert result.refused is False
    assert [c.citation_id for c in result.citations] == [chunk.citation_id]
    assert result.dropped_ids == ["fake-id"]


def test_canonical_refusal_is_always_refused() -> None:
    chunk = _chunk(str(uuid4()))
    result = verify_citations(REFUSAL_ANSWER, [chunk.citation_id], {chunk.citation_id: chunk})
    assert result.refused is True
    assert result.citations == []


def test_empty_answer_refuses() -> None:
    result = verify_citations("   ", [], {})
    assert result.refused is True
    assert result.answer == REFUSAL_ANSWER
