"""Redis fixed-window rate limiting (atomic via Lua).

Fails **closed**: if Redis is unreachable the request is rejected with 503
rather than letting an unlimited agent run up provider costs.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, cast

import redis.asyncio as redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

_LUA = """
local current = redis.call('INCR', KEYS[1])
local ttl = redis.call('PTTL', KEYS[1])
if ttl < 0 then
  -- Self-heal a key whose expiry was lost (crash between INCR and PEXPIRE).
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  ttl = tonumber(ARGV[2])
end
return {current, ttl}
"""


class RateLimitUnavailable(RuntimeError):
    """Redis is down; caller should return 503."""


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    used: int
    remaining: int
    reset_seconds: int


async def check_rate_limit(
    client: redis.Redis, *, user_id: str, limit: int, window_seconds: int
) -> RateLimitResult:
    key = f"rl:v1:{user_id}:{window_seconds}"
    try:
        # redis-py types `eval` as sync-or-async; on the asyncio client the
        # awaitable branch is the real one. The Lua script returns [used, ttl_ms].
        pending = cast(
            Awaitable[Any], client.eval(_LUA, 1, key, str(limit), str(window_seconds * 1000))
        )
        used_raw, ttl_raw = cast(list[int], await pending)
    except (RedisError, OSError) as exc:
        logger.error("rate limiter unavailable", extra={"error": type(exc).__name__})
        raise RateLimitUnavailable("rate limiter unavailable") from exc

    used = int(used_raw)
    ttl = int(ttl_raw)
    reset_seconds = max(1, (ttl + 999) // 1000) if ttl > 0 else window_seconds
    return RateLimitResult(
        allowed=used <= limit,
        limit=limit,
        used=used,
        remaining=max(0, limit - used),
        reset_seconds=reset_seconds,
    )
