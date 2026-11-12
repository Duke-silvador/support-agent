"""Human-in-the-loop decisions for agent escalations.

`escalate_to_human` only creates a row in `approvals` with status=pending.
Nothing external happens until a different operator approves it.

On approval the decision **and** an outbox row commit in one transaction, so the
ticket cannot be lost between "decided" and "dispatched". A first dispatch is
attempted inline; the worker retries anything that fails, with exponential
backoff. ``approval_id`` is unique in the outbox, so a retry can never create a
second ticket.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from support_agent.agents.outbox import (
    enqueue_escalation,
    outbox_state,
    process_outbox,
)
from support_agent.api.deps import enforce_rate_limit, is_operator
from support_agent.api.schemas import ApprovalDecision, ApprovalResponse
from support_agent.core.config import Settings, get_settings
from support_agent.core.identity import Identity, get_identity
from support_agent.core.ratelimit import RateLimitResult
from support_agent.db.models import Approval, EscalationOutbox, OutboxStatus
from support_agent.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter(tags=["approvals"])

_OPERATOR_ERROR = "only an operator may decide an escalation"


@router.get(
    "/approvals/{approval_id}",
    response_model=ApprovalResponse,
    responses={
        403: {"description": "Not the requester or an operator"},
        404: {"description": "Unknown approval"},
    },
)
async def get_approval(
    approval_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(get_identity)],
    settings: Annotated[Settings, Depends(get_settings)],
    _rate: Annotated[RateLimitResult, Depends(enforce_rate_limit)],
) -> ApprovalResponse:
    approval = await session.get(Approval, approval_id)
    operator = _operator(identity, settings)
    # Do not leak the existence of other users' escalations.
    if approval is None or (approval.user_id != identity.user_id and not operator):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
    row = await outbox_state(session, approval.id)
    return _to_response(approval, outbox=row)


@router.post(
    "/approvals/{approval_id}",
    response_model=ApprovalResponse,
    responses={
        403: {"description": "Requester or non-operator cannot approve"},
        404: {"description": "Unknown approval"},
        409: {"description": "Already decided"},
        503: {"description": "Decision stored; ticket dispatch is queued for retry"},
    },
)
async def decide_approval(
    approval_id: UUID,
    decision: ApprovalDecision,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(get_identity)],
    settings: Annotated[Settings, Depends(get_settings)],
    _rate: Annotated[RateLimitResult, Depends(enforce_rate_limit)],
) -> ApprovalResponse:
    stmt = select(Approval).where(Approval.id == approval_id).with_for_update()
    approval = await session.scalar(stmt)
    if approval is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
    # The deciding identity comes from the authenticated caller, never from the
    # request body. A body field would let anyone forge an "operator".
    if not _operator(identity, settings):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_OPERATOR_ERROR)
    if approval.status != "pending":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"approval already {approval.status}",
        )
    # Separation of duties: the person who triggered the escalation cannot
    # rubber-stamp it.
    if identity.user_id == approval.user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="a different operator must approve this escalation",
        )

    approval.status = "approved" if decision.decision == "approve" else "rejected"
    approval.decided_at = datetime.now(tz=UTC)
    approval.decided_by = identity.user_id
    if decision.note:
        payload: dict[str, Any] = dict(approval.payload or {})
        payload["decision_note"] = decision.note
        approval.payload = payload

    if approval.status == "approved":
        # Same transaction as the decision: either both exist or neither does.
        await enqueue_escalation(session, approval.id)
    await session.commit()

    row: EscalationOutbox | None = None
    if approval.status == "approved":
        # Fast path: try to create the ticket now. Failures stay in the outbox
        # and the worker retries them, so a 5xx provider response never loses it.
        await process_outbox(session, settings=settings, only=approval.id)
        row = await outbox_state(session, approval.id)

    logger.info(
        "approval decided",
        extra={
            "approval_id": str(approval.id),
            "status": approval.status,
            "decided_by": approval.decided_by,
            "outbox_status": row.status if row is not None else None,
            "ticket_ref": row.ticket_ref if row is not None else None,
        },
    )
    return _to_response(approval, outbox=row)


@router.post(
    "/approvals/{approval_id}/dispatch",
    response_model=ApprovalResponse,
    responses={
        403: {"description": "Not an operator"},
        404: {"description": "Unknown approval"},
        409: {"description": "Approval is not approved"},
        502: {"description": "Dispatch failed; queued for retry"},
    },
)
async def retry_dispatch(
    approval_id: UUID,
    session: Annotated[AsyncSession, Depends(get_session)],
    identity: Annotated[Identity, Depends(get_identity)],
    settings: Annotated[Settings, Depends(get_settings)],
    _rate: Annotated[RateLimitResult, Depends(enforce_rate_limit)],
) -> ApprovalResponse:
    """Idempotent retry for an approved escalation whose ticket does not exist yet.

    Dispatching twice is safe: the outbox is unique per approval, a stored
    ``ticket_ref`` short-circuits, and the dispatcher treats ``approval.id`` as
    its idempotency key.
    """
    approval = await session.scalar(select(Approval).where(Approval.id == approval_id))
    if approval is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="approval not found")
    if not _operator(identity, settings):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only an operator may dispatch an escalation",
        )
    if approval.status != "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"approval is {approval.status}, not approved",
        )

    await process_outbox(session, settings=settings, only=approval.id)
    row = await outbox_state(session, approval.id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="approved escalation has no outbox entry",
        )
    if row.status != OutboxStatus.DONE:
        logger.warning(
            "escalation dispatch retry is still queued",
            extra={
                "approval_id": str(approval.id),
                "outbox_status": row.status,
                "attempts": row.attempts,
            },
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="escalation dispatch failed; it will be retried automatically",
        )
    return _to_response(approval, outbox=row)


def _operator(identity: Identity, settings: Settings) -> bool:
    return is_operator(identity.user_id, settings, identity.roles)


def _to_response(approval: Approval, *, outbox: EscalationOutbox | None = None) -> ApprovalResponse:
    payload = approval.payload or {}
    stored_ticket = payload.get("ticket_ref")
    ticket_ref = outbox.ticket_ref if outbox is not None and outbox.ticket_ref else stored_ticket
    dispatch_failed = bool(payload.get("dispatch_failed", False))
    if outbox is not None and outbox.status != OutboxStatus.DONE:
        # The decision stands, but the ticket does not exist yet (queued or
        # retrying). Surface that instead of implying a ticket was created.
        dispatch_failed = True
    return ApprovalResponse(
        id=approval.id,
        status=approval.status,
        user_id=approval.user_id,
        conversation_id=approval.conversation_id,
        reason=approval.reason,
        summary=approval.summary,
        priority=str(payload.get("priority", "normal")),
        created_at=approval.created_at,
        decided_at=approval.decided_at,
        decided_by=approval.decided_by,
        ticket_ref=str(ticket_ref) if ticket_ref else None,
        dispatch_failed=dispatch_failed,
    )
