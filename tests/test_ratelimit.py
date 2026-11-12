from __future__ import annotations

import fakeredis.aioredis
import pytest

from support_agent.core.ratelimit import RateLimitUnavailable, check_rate_limit


@pytest.fixture
def redis_client() -> fakeredis.aioredis.FakeRedis:
    return fakeredis.aioredis.FakeRedis(decode_responses=True)


@pytest.mark.asyncio
async def test_allows_requests_up_to_the_limit(redis_client: fakeredis.aioredis.FakeRedis) -> None:
    results = [
        await check_rate_limit(redis_client, user_id="alice", limit=20, window_seconds=60)
        for _ in range(20)
    ]
    assert all(r.allowed for r in results)
    assert results[-1].remaining == 0
    assert results[-1].used == 20


@pytest.mark.asyncio
async def test_blocks_the_request_after_the_limit(
    redis_client: fakeredis.aioredis.FakeRedis,
) -> None:
    for _ in range(20):
        await check_rate_limit(redis_client, user_id="alice", limit=20, window_seconds=60)
    blocked = await check_rate_limit(redis_client, user_id="alice", limit=20, window_seconds=60)
    assert blocked.allowed is False
    assert blocked.remaining == 0
    assert 1 <= blocked.reset_seconds <= 60


@pytest.mark.asyncio
async def test_limits_are_per_user(redis_client: fakeredis.aioredis.FakeRedis) -> None:
    for _ in range(20):
        await check_rate_limit(redis_client, user_id="alice", limit=20, window_seconds=60)
    bob = await check_rate_limit(redis_client, user_id="bob", limit=20, window_seconds=60)
    assert bob.allowed is True
    assert bob.used == 1


@pytest.mark.asyncio
async def test_a_lost_expiry_is_repaired(redis_client: fakeredis.aioredis.FakeRedis) -> None:
    # Simulates a crash between INCR and PEXPIRE: the counter has no TTL.
    await redis_client.set("rl:v1:alice:60", 5)
    result = await check_rate_limit(redis_client, user_id="alice", limit=20, window_seconds=60)
    assert result.used == 6
    ttl = await redis_client.pttl("rl:v1:alice:60")
    assert 0 < ttl <= 60_000


@pytest.mark.asyncio
async def test_fails_closed_when_redis_is_down() -> None:
    class Broken:
        async def eval(self, *args: object, **kwargs: object) -> None:
            raise OSError("connection refused")

    with pytest.raises(RateLimitUnavailable):
        await check_rate_limit(Broken(), user_id="alice", limit=20, window_seconds=60)  # type: ignore[arg-type]
