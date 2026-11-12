"""Citation verification.

The model's citations are untrusted claims. Every id must correspond to a chunk
that retrieval actually returned; otherwise it is dropped. An answer with no
surviving citation is not grounded, so it is replaced by the refusal sentence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from support_agent.core.constants import REFUSAL_ANSWER
from support_agent.rag.retriever import RetrievedChunk

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Verification:
    answer: str
    citations: list[RetrievedChunk] = field(default_factory=list)
    dropped_ids: list[str] = field(default_factory=list)
    refused: bool = False


def verify_citations(
    answer: str, claimed_ids: list[str], allowed: dict[str, RetrievedChunk]
) -> Verification:
    cleaned_answer = (answer or "").strip()
    if not cleaned_answer:
        return Verification(answer=REFUSAL_ANSWER, refused=True)

    if cleaned_answer == REFUSAL_ANSWER:
        return Verification(answer=REFUSAL_ANSWER, refused=True)

    seen: set[str] = set()
    valid: list[RetrievedChunk] = []
    dropped: list[str] = []
    for raw_id in claimed_ids:
        chunk_id = str(raw_id).strip()
        if not chunk_id or chunk_id in seen:
            continue
        seen.add(chunk_id)
        chunk = allowed.get(chunk_id)
        if chunk is None:
            dropped.append(chunk_id)
        else:
            valid.append(chunk)

    if dropped:
        logger.warning("dropped hallucinated citations", extra={"dropped": dropped[:10]})

    if not valid:
        # Ungrounded answer: refuse rather than show text nobody can verify.
        return Verification(answer=REFUSAL_ANSWER, dropped_ids=dropped, refused=True)

    return Verification(answer=cleaned_answer, citations=valid, dropped_ids=dropped)
