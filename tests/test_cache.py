from __future__ import annotations

import fakeredis.aioredis
import pytest

from support_agent.core.cache import (
    build_cache_key,
    delete_cached,
    get_cached,
    normalise_question,
    set_cached,
)

_VIEWER = "agent-user"


def _key(
    question: str = "How do refunds work?",
    viewer: str = _VIEWER,
    pipeline: str = "p1|k=6|s=0.15|i=4|o=800",
) -> str:
    return build_cache_key(
        question=question,
        corpus="3:2026-11-05T10:00:00+00:00",
        llm_provider="fake",
        llm_model="fake-extractive-v1",
        embedding_model="fake-hashing-256",
        viewer_id=viewer,
        pipeline=pipeline,
    )


def test_question_normalisation() -> None:
    assert normalise_question("  How   do REFUNDS work? ") == "how do refunds work?"


def test_cache_key_is_stable_for_equivalent_questions() -> None:
    assert _key("How do refunds work?") == _key("  how   do refunds work? ")


def test_cache_key_changes_with_corpus_and_model() -> None:
    base = _key()
    assert base != build_cache_key(
        question="How do refunds work?",
        corpus="4:2026-11-06T10:00:00+00:00",
        llm_provider="fake",
        llm_model="fake-extractive-v1",
        embedding_model="fake-hashing-256",
        viewer_id=_VIEWER,
        pipeline="p1|k=6|s=0.15|i=4|o=800",
    )
    assert base != build_cache_key(
        question="How do refunds work?",
        corpus="3:2026-11-05T10:00:00+00:00",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        embedding_model="text-embedding-3-small",
        viewer_id=_VIEWER,
        pipeline="p1|k=6|s=0.15|i=4|o=800",
    )


def test_cache_key_changes_with_prompt_or_retrieval_settings() -> None:
    """A prompt/retrieval change must not keep serving answers cached before it."""
    assert _key(pipeline="p1|k=6|s=0.15|i=4|o=800") != _key(pipeline="p2|k=6|s=0.15|i=4|o=800")
    assert _key(pipeline="p1|k=6|s=0.15|i=4|o=800") != _key(pipeline="p1|k=10|s=0.15|i=4|o=800")


def test_cache_key_is_scoped_to_the_viewer() -> None:
    """Answers cover the viewer's visible corpus, so B must never hit A's entry."""
    assert _key(viewer="agent-user") != _key(viewer="other-user")


@pytest.mark.asyncio
async def test_roundtrip() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await set_cached(client, _key(), {"answer": "30 days"}, ttl_seconds=60)
    assert await get_cached(client, _key()) == {"answer": "30 days"}


@pytest.mark.asyncio
async def test_ttl_zero_disables_cache() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await set_cached(client, _key(), {"answer": "x"}, ttl_seconds=0)
    assert await get_cached(client, _key()) is None


@pytest.mark.asyncio
async def test_ttl_zero_also_disables_reads() -> None:
    """Entries written while the cache was on must not survive TTL=0."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await set_cached(client, _key(), {"answer": "stale"}, ttl_seconds=60)
    assert await get_cached(client, _key(), ttl_seconds=0) is None
    assert await get_cached(client, _key(), ttl_seconds=60) == {"answer": "stale"}


@pytest.mark.asyncio
async def test_corrupt_entry_is_a_miss() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await client.set(_key(), "{not json")
    assert await get_cached(client, _key()) is None


@pytest.mark.asyncio
async def test_delete_removes_a_poisoned_entry() -> None:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await set_cached(client, _key(), {"answer": "x"}, ttl_seconds=60)
    await delete_cached(client, _key())
    assert await get_cached(client, _key()) is None
