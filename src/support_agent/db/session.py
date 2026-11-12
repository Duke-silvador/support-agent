"""Async SQLAlchemy engine/session. No schema here — Alembic owns that."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from support_agent.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=5,
            pool_recycle=1800,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def ping_database(budget_seconds: float = 2.0) -> bool:
    try:
        async with asyncio.timeout(budget_seconds):
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def verify_embedding_dimension(expected: int) -> None:
    """Fail fast if EMBEDDING_DIM does not match the migrated vector column.

    Without this, the first insert fails with a cryptic pgvector type error.
    """
    query = text(
        "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
        "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
    )
    async with get_engine().connect() as conn:
        column_type = await conn.scalar(query)
    text_type = str(column_type or "")
    actual = text_type.removeprefix("vector(").removesuffix(")")
    if actual != str(expected):
        raise RuntimeError(
            f"EMBEDDING_DIM={expected} does not match database column {text_type}; "
            "re-run migrations or fix EMBEDDING_DIM"
        )


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
