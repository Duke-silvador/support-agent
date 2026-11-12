"""Outbox worker: ``python -m support_agent.workers.outbox``.

Runs one loop that polls due rows, dispatches them with retries/backoff and then
sleeps. Safe to run multiple replicas (claims use ``FOR UPDATE SKIP LOCKED``).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from support_agent.agents.outbox import process_outbox
from support_agent.core.config import get_settings
from support_agent.core.logging import configure_logging
from support_agent.core.redis import close_redis
from support_agent.core.telemetry import configure_worker_telemetry, shutdown_telemetry
from support_agent.db.session import dispose_engine, get_session_factory

logger = logging.getLogger(__name__)


async def run_once() -> int:
    settings = get_settings()
    async with get_session_factory()() as session:
        return await process_outbox(session, settings=settings)


async def run_forever(stop: asyncio.Event) -> None:
    settings = get_settings()
    logger.info("outbox worker started", extra={"poll_seconds": settings.outbox_poll_seconds})
    while not stop.is_set():
        try:
            processed = await run_once()
            if processed:
                logger.info("outbox batch processed", extra={"rows": processed})
        except Exception:
            # A transient database error must not kill the worker; the next poll
            # retries the due rows.
            logger.exception("outbox poll failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.outbox_poll_seconds)
        except TimeoutError:
            continue
    logger.info("outbox worker stopped")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    configure_worker_telemetry(settings)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):  # pragma: no cover - Windows
            loop.add_signal_handler(sig, stop.set)
    try:
        await run_forever(stop)
    finally:
        await close_redis()
        await dispose_engine()
        shutdown_telemetry()


if __name__ == "__main__":  # pragma: no cover - process entry point
    asyncio.run(main())
