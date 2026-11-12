"""Side effects that are only allowed after a human approves an escalation.

Kept out of the agent/tool layer on purpose: the model cannot reach this code.
Swap the log line for the real ITSM/queue integration (Zendesk, Jira, Slack...).
"""

from __future__ import annotations

import logging

from support_agent.db.models import Approval

logger = logging.getLogger(__name__)


async def dispatch_escalation(approval: Approval) -> str:
    """Create the human ticket. Returns the ticket reference."""
    ticket_ref = f"SUP-{approval.id.hex[:8].upper()}"
    logger.info(
        "escalation approved; ticket created",
        extra={
            "approval_id": str(approval.id),
            "ticket_ref": ticket_ref,
            "user_id": approval.user_id,
            "conversation_id": approval.conversation_id,
            "trace_id": approval.trace_id,
        },
    )
    return ticket_ref
