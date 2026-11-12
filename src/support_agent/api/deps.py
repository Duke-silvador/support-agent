"""Shared FastAPI dependencies."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Response, status

from support_agent.core.config import Settings, get_settings
from support_agent.core.identity import get_user_id
from support_agent.core.ratelimit import RateLimitResult, RateLimitUnavailable, check_rate_limit
from support_agent.core.redis import get_redis

_USER = Annotated[str, Depends(get_user_id)]
_SETTINGS_DEP = Annotated[Settings, Depends(get_settings)]


def is_operator(user_id: str, settings: Settings, roles: frozenset[str] = frozenset()) -> bool:
    """Operators are an allowlist of subjects plus an optional IdP role.

    An empty configuration denies by default. ``ALLOW_ANON_OPERATORS=true``
    exists only for local demos/tests; prod rejects it at config validation time.
    """
    if settings.oidc_operator_role and settings.oidc_operator_role in roles:
        return True
    operators = settings.operators
    if not operators:
        return settings.allow_anon_operators
    return user_id in operators


async def enforce_rate_limit(
    response: Response,
    user_id: _USER,
    settings: _SETTINGS_DEP,
) -> RateLimitResult:
    """20 requests / user / minute by default. Fails closed if Redis is down."""
    try:
        result = await check_rate_limit(
            get_redis(),
            user_id=user_id,
            limit=settings.rate_limit_requests,
            window_seconds=settings.rate_limit_window_seconds,
        )
    except RateLimitUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="request limiter unavailable; retry shortly",
        ) from exc

    response.headers["X-RateLimit-Limit"] = str(result.limit)
    response.headers["X-RateLimit-Remaining"] = str(result.remaining)
    response.headers["X-RateLimit-Reset"] = str(result.reset_seconds)

    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limited",
                "limit": result.limit,
                "window_seconds": settings.rate_limit_window_seconds,
                "retry_after_seconds": result.reset_seconds,
            },
            headers={
                "Retry-After": str(result.reset_seconds),
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(result.reset_seconds),
            },
        )
    return result
