from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from support_agent.api.middleware import BodySizeLimitMiddleware
from support_agent.api.routes import approvals, chat, documents, health
from support_agent.core.config import get_settings
from support_agent.core.identity import reset_verifiers
from support_agent.core.logging import configure_logging
from support_agent.core.redis import close_redis
from support_agent.core.telemetry import configure_telemetry, shutdown_telemetry
from support_agent.db.session import dispose_engine, verify_embedding_dimension

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Fail fast on a dimension mismatch instead of on the first upload.
    await verify_embedding_dimension(get_settings().embedding_dim)
    try:
        yield
    finally:
        # Drop cached JWKS clients so a config change between restarts (or a
        # test's monkeypatched verifier) cannot leak into the next boot.
        reset_verifiers()
        await close_redis()
        await dispose_engine()
        shutdown_telemetry()


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    if settings.auth_mode != "oidc":
        logger.warning(
            "AUTH_MODE=%s: identity comes from the spoofable X-User-ID header. "
            "Local demo only — set AUTH_MODE=oidc before exposing this port.",
            settings.auth_mode,
        )
    app = FastAPI(
        title="Support Agent",
        version="0.1.0",
        description="RAG customer-support agent with citations and human escalation.",
        lifespan=lifespan,
        docs_url="/docs" if settings.environment != "prod" else None,
        openapi_url="/openapi.json" if settings.environment != "prod" else None,
        redoc_url=None,
    )

    # Chunked-safe body caps: count received bytes, so a request without
    # Content-Length cannot spool an unbounded upload to disk or an unbounded
    # JSON body into memory before auth/rate-limit dependencies run.
    app.add_middleware(
        BodySizeLimitMiddleware,
        upload_max_bytes=settings.max_upload_bytes + 64 * 1024,
        json_max_bytes=settings.max_json_bytes,
    )

    app.include_router(health.router)
    app.include_router(documents.router)
    app.include_router(chat.router)
    app.include_router(approvals.router)
    # Must run before startup: OTel instrumentation adds ASGI middleware and
    # Starlette rejects add_middleware() once the app has started.
    configure_telemetry(app, settings)
    return app


app = create_app()
