"""escalation outbox: durable, retrying ticket dispatch

Revision ID: 0003_escalation_outbox
Revises: 0002_document_ownership
Create Date: 2026-11-08

One row per approved escalation. ``approval_id`` is unique, so enqueueing the
same decision twice cannot produce two tickets. A worker claims due rows with
``FOR UPDATE SKIP LOCKED``, dispatches outside the transaction and records the
result; failures get an exponential backoff until ``OUTBOX_MAX_ATTEMPTS``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_escalation_outbox"
down_revision: str | None = "0002_document_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "escalation_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "approval_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("approvals.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("ticket_ref", sa.String(length=128), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'done', 'failed')",
            name="ck_escalation_outbox_status",
        ),
    )
    op.create_index("ix_escalation_outbox_due", "escalation_outbox", ["status", "next_attempt_at"])


def downgrade() -> None:
    op.drop_index("ix_escalation_outbox_due", table_name="escalation_outbox")
    op.drop_table("escalation_outbox")
