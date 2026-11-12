"""Redis client: rate limiting + response cache live on top of this."""

from __future__ import annotations

import asyncio
from typing import Any

import redis.asyncio as redis

from support_agent.core.config import get_settings

_client: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(
            get_settings().redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
            health_check_interval=30,
        )
    return _client


async def ping_redis(budget_seconds: float = 2.0) -> bool:
    try:
        async with asyncio.timeout(budget_seconds):
            pong: Any = await get_redis().ping()
        return bool(pong)
    except Exception:
        return False


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
