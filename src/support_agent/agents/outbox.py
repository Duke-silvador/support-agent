"""Transactional outbox for escalation tickets.

Why: the approval decision and "a ticket must exist" have to be atomic. The
decision commits together with an outbox row; a separate worker then performs the
external side effect with retries and exponential backoff. A crash after the
decision can therefore no longer lose the ticket, and a retry after a successful
dispatch can no longer create a second one (``approval_id`` is unique and the
dispatcher must be idempotent on it).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import Table, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.core.config import Settings
from support_agent.db.models import Approval, EscalationOutbox, OutboxStatus

logger = logging.getLogger(__name__)

Dispatcher = Callable[[Approval], Awaitable[str]]

_MAX_BACKOFF_SECONDS = 3600.0


async def enqueue_escalation(session: AsyncSession, approval_id: UUID) -> None:
    """Idempotently add one outbox row. Safe to call inside the decision tx."""
    table: Table = cast(Table, EscalationOutbox.__table__)
    stmt = (
        pg_insert(table)
        .values(
            approval_id=approval_id,
            status=OutboxStatus.PENDING,
            attempts=0,
            next_attempt_at=datetime.now(UTC),
        )
        .on_conflict_do_nothing(index_elements=["approval_id"])
    )
    await session.execute(stmt)


async def outbox_state(session: AsyncSession, approval_id: UUID) -> EscalationOutbox | None:
    row: EscalationOutbox | None = await session.scalar(
        select(EscalationOutbox).where(EscalationOutbox.approval_id == approval_id)
    )
    return row


def backoff_seconds(settings: Settings, attempts: int) -> float:
    """Exponential backoff for the next attempt (attempts >= 1)."""
    delay: float = min(
        settings.outbox_base_backoff_seconds * (2 ** max(0, attempts - 1)), _MAX_BACKOFF_SECONDS
    )
    return delay


async def process_outbox(
    session: AsyncSession,
    *,
    settings: Settings,
    dispatcher: Dispatcher | None = None,
    only: UUID | None = None,
) -> int:
    """Dispatch due rows. Returns how many rows were claimed.

    Rows are claimed with ``FOR UPDATE SKIP LOCKED`` (safe with several worker
    replicas) and the claim is committed before the external call, so a slow
    ticket API never holds a database lock. ``only`` forces a specific approval
    to be retried now (used by the operator retry endpoint).
    """
    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=settings.outbox_stale_seconds)
    # Resolve the side effect at call time so a deployment can swap the module
    # level dispatcher (and tests can monkeypatch it) without touching the
    # worker or this function's signature.
    if dispatcher is None:
        from support_agent.agents.escalation import dispatch_escalation as default_dispatcher

        dispatcher = default_dispatcher

    # A worker that died mid-dispatch leaves a row `in_progress`; put it back in
    # the queue once its claim is stale.
    await session.execute(
        update(EscalationOutbox)
        .where(
            EscalationOutbox.status == OutboxStatus.IN_PROGRESS,
            EscalationOutbox.updated_at < stale_before,
        )
        .values(status=OutboxStatus.PENDING, next_attempt_at=now, updated_at=now)
    )
    if only is not None:
        await session.execute(
            update(EscalationOutbox)
            .where(
                EscalationOutbox.approval_id == only,
                EscalationOutbox.status.in_((OutboxStatus.PENDING, OutboxStatus.FAILED)),
            )
            .values(status=OutboxStatus.PENDING, next_attempt_at=now, updated_at=now)
        )
    await session.commit()

    conditions = [
        EscalationOutbox.status == OutboxStatus.PENDING,
        EscalationOutbox.next_attempt_at <= now,
    ]
    if only is not None:
        conditions.append(EscalationOutbox.approval_id == only)

    rows = list(
        await session.scalars(
            select(EscalationOutbox)
            .where(*conditions)
            .order_by(EscalationOutbox.next_attempt_at)
            .limit(settings.outbox_batch_size)
            .with_for_update(skip_locked=True)
        )
    )
    if not rows:
        return 0

    claimed: list[UUID] = []
    for row in rows:
        row.status = OutboxStatus.IN_PROGRESS
        row.attempts += 1
        row.updated_at = now
        claimed.append(row.id)
    await session.commit()

    for row_id in claimed:
        claimed_row: EscalationOutbox | None = await session.get(EscalationOutbox, row_id)
        if claimed_row is None:  # pragma: no cover - deleted after claim
            continue
        await _dispatch_row(session, claimed_row, settings=settings, dispatcher=dispatcher)
    return len(claimed)


async def _dispatch_row(
    session: AsyncSession,
    row: EscalationOutbox,
    *,
    settings: Settings,
    dispatcher: Dispatcher,
) -> None:
    now = datetime.now(UTC)
    approval = await session.get(Approval, row.approval_id)
    if approval is None or approval.status != "approved":
        row.status = OutboxStatus.FAILED
        row.last_error = "approval missing or not approved"
        row.updated_at = now
        await session.commit()
        logger.error("outbox row without an approved approval", extra={"outbox_id": str(row.id)})
        return

    payload = dict(approval.payload or {})
    existing = payload.get("ticket_ref")
    if existing:
        # Already dispatched: never call the ticket API twice for one approval.
        row.status = OutboxStatus.DONE
        row.ticket_ref = str(existing)
        row.last_error = None
        row.updated_at = now
        await session.commit()
        return

    try:
        ticket_ref = await dispatcher(approval)
    except Exception as exc:
        row.last_error = type(exc).__name__
        row.updated_at = now
        if row.attempts >= settings.outbox_max_attempts:
            row.status = OutboxStatus.FAILED
        else:
            row.status = OutboxStatus.PENDING
            row.next_attempt_at = now + timedelta(seconds=backoff_seconds(settings, row.attempts))
        await session.commit()
        logger.exception(
            "escalation dispatch failed",
            extra={
                "approval_id": str(approval.id),
                "attempts": row.attempts,
                "status": row.status,
            },
        )
        return

    row.status = OutboxStatus.DONE
    row.ticket_ref = ticket_ref
    row.last_error = None
    row.updated_at = now
    payload["ticket_ref"] = ticket_ref
    payload["dispatch_failed"] = False
    approval.payload = payload
    await session.commit()
    logger.info(
        "escalation ticket created",
        extra={"approval_id": str(approval.id), "ticket_ref": ticket_ref},
    )
