"""Answer cache for repeated questions.

Key includes the normalised question, the provider/model pair and a coarse
``corpus_version`` (document count + newest document timestamp), so adding a
document invalidates old answers without scanning the table.

Cache is an optimisation only: Redis failures are logged and treated as misses.
Answers that request a human approval are never cached.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

import redis.asyncio as redis
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.db.models import Document

logger = logging.getLogger(__name__)

_KEY_PREFIX = "cache:answer:v2"


def normalise_question(question: str) -> str:
    return " ".join(question.lower().split())


async def corpus_version(session: AsyncSession) -> str:
    row = await session.execute(select(func.count(Document.id), func.max(Document.created_at)))
    count, newest = row.one()
    stamp = newest.isoformat() if newest is not None else "empty"
    return f"{count}:{stamp}"


def build_cache_key(
    *,
    question: str,
    corpus: str,
    llm_provider: str,
    llm_model: str,
    embedding_model: str,
    viewer_id: str,
    pipeline: str,
) -> str:
    """Cache key for one question and one viewer's visible corpus.

    Answers are owner-scoped (retrieval filters by owner/public), so the viewer
    must be part of the key: otherwise user B could be served user A's answer
    about A's private documents. ``pipeline`` carries the prompt revision and the
    retrieval/agent knobs, so tuning them cannot keep serving stale answers.
    """
    material = "|".join(
        [
            normalise_question(question),
            corpus,
            llm_provider,
            llm_model,
            embedding_model,
            viewer_id,
            pipeline,
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"{_KEY_PREFIX}:{digest}"


async def get_cached(
    client: redis.Redis, key: str, *, ttl_seconds: int | None = None
) -> dict[str, Any] | None:
    if ttl_seconds is not None and ttl_seconds <= 0:
        # CACHE_TTL_SECONDS=0 means "no cache", not "keep serving old writes
        # until their original Redis TTL expires".
        return None
    try:
        raw = await client.get(key)
    except (RedisError, OSError) as exc:
        logger.warning("cache read failed", extra={"error": type(exc).__name__})
        return None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("corrupt cache entry ignored")
        return None
    return payload if isinstance(payload, dict) else None


async def set_cached(
    client: redis.Redis, key: str, payload: dict[str, Any], ttl_seconds: int
) -> None:
    if ttl_seconds <= 0:
        return
    try:
        await client.set(key, json.dumps(payload, separators=(",", ":")), ex=ttl_seconds)
    except (RedisError, OSError) as exc:
        logger.warning("cache write failed", extra={"error": type(exc).__name__})


async def delete_cached(client: redis.Redis, key: str) -> None:
    """Drop a corrupt/poisoned entry; failures are non-fatal (it expires anyway)."""
    try:
        await client.delete(key)
    except (RedisError, OSError) as exc:
        logger.warning("cache delete failed", extra={"error": type(exc).__name__})
