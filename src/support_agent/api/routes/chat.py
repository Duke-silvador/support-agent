"""POST /chat — RAG answer with verified citations."""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import uuid4

import httpx
from anthropic import AnthropicError
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from openai import OpenAIError
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.agents.loop import AgentAnswer, run_agent
from support_agent.api.deps import enforce_rate_limit
from support_agent.api.schemas import (
    ApprovalRefSchema,
    ChatRequest,
    ChatResponse,
    CitationSchema,
    UsageSchema,
)
from support_agent.core.cache import (
    build_cache_key,
    corpus_version,
    delete_cached,
    get_cached,
    set_cached,
)
from support_agent.core.config import Settings, get_settings
from support_agent.core.constants import PROMPT_VERSION
from support_agent.core.identity import get_user_id
from support_agent.core.ratelimit import RateLimitResult
from support_agent.core.redis import get_redis
from support_agent.core.telemetry import current_trace_id
from support_agent.db.session import get_session
from support_agent.providers.factory import get_embedding_provider, get_llm_provider

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])

# Only upstream provider failures are "temporarily unavailable"; DB/programming
# errors must surface as 500 so they are visible to alerting.
_PROVIDER_ERRORS: tuple[type[BaseException], ...] = (
    OpenAIError,
    AnthropicError,
    httpx.HTTPError,
    TimeoutError,
)


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses={
        429: {"description": "Rate limit exceeded"},
        502: {"description": "LLM provider unavailable"},
        503: {"description": "Rate limiter unavailable"},
    },
)
async def chat(
    payload: ChatRequest,
    request: Request,
    response: Response,
    user_id: Annotated[str, Depends(get_user_id)],
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    _rate: Annotated[RateLimitResult, Depends(enforce_rate_limit)],
) -> ChatResponse:
    conversation_id = payload.conversation_id or f"c-{uuid4().hex[:16]}"
    redis_client = get_redis()
    # Clients (and the eval runner) can force a fresh answer without waiting for
    # the TTL. `no-cache` only bypasses the read; writes still warm the cache.
    no_cache = "no-cache" in request.headers.get("cache-control", "").lower()

    cache_key = build_cache_key(
        question=payload.question,
        corpus=await corpus_version(session),
        llm_provider=get_llm_provider().name,
        llm_model=get_llm_provider().model,
        embedding_model=get_embedding_provider().model,
        # Answers are scoped to the viewer's visible corpus (own + public docs).
        viewer_id=user_id,
        # Prompt revision and retrieval/agent knobs: change one, miss the cache.
        pipeline=(
            f"{PROMPT_VERSION}|k={settings.retrieval_top_k}"
            f"|s={settings.retrieval_min_score}|i={settings.agent_max_iterations}"
            f"|o={settings.max_output_tokens}"
        ),
    )
    # corpus_version() starts a read transaction; end it so the DB connection is
    # not pinned for the whole (slow) LLM round-trip.
    await session.rollback()
    cached = (
        None
        if no_cache
        else await get_cached(redis_client, cache_key, ttl_seconds=settings.cache_ttl_seconds)
    )
    if cached is not None:
        # Cached payloads are corpus-scoped but conversation ids are not.
        # Always re-issue request-scoped fields so user B never receives user A's
        # conversation or trace id.
        cached["conversation_id"] = conversation_id
        cached["cached"] = True
        cached["trace_id"] = current_trace_id()
        try:
            cached_response = ChatResponse.model_validate(cached)
        except ValidationError:
            # A corrupt/old entry must not become a permanent 500 for this
            # question: drop it and answer normally.
            logger.warning("discarding invalid cache entry")
            await delete_cached(redis_client, cache_key)
        else:
            response.headers["X-Cache"] = "HIT"
            return cached_response

    try:
        answer = await run_agent(
            session,
            question=payload.question,
            user_id=user_id,
            conversation_id=conversation_id,
            settings=settings,
        )
    except _PROVIDER_ERRORS as exc:
        logger.exception("provider call failed", extra={"user_id": user_id})
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="the assistant is temporarily unavailable; please retry",
        ) from exc

    result = _to_response(answer, conversation_id=conversation_id, cached=False)
    if answer.approval is None:
        await set_cached(
            redis_client, cache_key, result.model_dump(mode="json"), settings.cache_ttl_seconds
        )
    response.headers["X-Cache"] = "MISS"

    logger.info(
        "chat answered",
        extra={
            "user_id": user_id,
            "conversation_id": conversation_id,
            "refused": answer.refused,
            "citations": len(answer.citations),
            "tools": answer.used_tools,
            "input_tokens": answer.input_tokens,
            "output_tokens": answer.output_tokens,
            "cached": False,
        },
    )
    return result


def _to_response(answer: AgentAnswer, *, conversation_id: str, cached: bool) -> ChatResponse:
    return ChatResponse(
        answer=answer.answer,
        citations=[
            CitationSchema(
                chunk_id=c.citation_id,
                source=c.source,
                section=c.section,
                score=round(c.score, 4),
                snippet=c.content[:280],
            )
            for c in answer.citations
        ],
        refused=answer.refused,
        used_tools=answer.used_tools,
        approval=(
            ApprovalRefSchema(id=answer.approval.id, status=answer.approval.status)
            if answer.approval is not None
            else None
        ),
        conversation_id=conversation_id,
        model=answer.model,
        usage=UsageSchema(input_tokens=answer.input_tokens, output_tokens=answer.output_tokens),
        trace_id=current_trace_id(),
        cached=cached,
    )
