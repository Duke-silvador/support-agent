"""Liveness/readiness. Reports dependency state without leaking details."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Response, status

from support_agent import __version__
from support_agent.api.schemas import HealthResponse
from support_agent.core.config import get_settings
from support_agent.core.redis import ping_redis
from support_agent.db.session import ping_database

router = APIRouter(tags=["ops"])


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse, "description": "A dependency is unavailable"}},
)
async def health(response: Response) -> HealthResponse:
    settings = get_settings()
    db_ok, redis_ok = await asyncio.gather(ping_database(), ping_redis())
    checks = {
        "database": "ok" if db_ok else "unavailable",
        "redis": "ok" if redis_ok else "unavailable",
    }
    healthy = all(v == "ok" for v in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if healthy else "degraded",
        service=settings.app_name,
        version=__version__,
        environment=settings.environment,
        checks=checks,
        providers={
            "llm": settings.llm_provider,
            "llm_model": settings.llm_model,
            "embedding": settings.embedding_provider,
            "embedding_model": settings.embedding_model,
        },
    )
