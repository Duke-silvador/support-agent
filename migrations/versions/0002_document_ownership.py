"""document ownership: owner_id, visibility, per-owner content hash

Revision ID: 0002_document_ownership
Revises: 0001_initial
Create Date: 2026-11-08

Existing rows are the shared company corpus: they are backfilled as
``owner_id='system'`` / ``visibility='public'`` so retrieval behaviour does not
change until they are re-uploaded. New rows must state their owner, so the
``owner_id`` server default introduced for the backfill is dropped again.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_document_ownership"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("owner_id", sa.String(length=128), nullable=False, server_default="system"),
    )
    op.add_column(
        "documents",
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="public"),
    )
    op.create_check_constraint(
        "ck_documents_visibility", "documents", "visibility IN ('private', 'public')"
    )

    # A global content hash would make "duplicate" leak another owner's metadata.
    op.drop_constraint("uq_documents_content_sha256", "documents", type_="unique")
    op.create_unique_constraint(
        "uq_documents_owner_content_sha256", "documents", ["owner_id", "content_sha256"]
    )
    op.create_index("ix_documents_visibility_owner", "documents", ["visibility", "owner_id"])

    # Backfill is done; future inserts must specify an owner, and private is the
    # safe default visibility.
    op.alter_column("documents", "owner_id", server_default=None)
    op.alter_column("documents", "visibility", server_default="private")


def downgrade() -> None:
    op.execute("UPDATE documents SET visibility = 'public' WHERE visibility <> 'public'")
    op.drop_index("ix_documents_visibility_owner", table_name="documents")
    op.drop_constraint("uq_documents_owner_content_sha256", "documents", type_="unique")
    # Rows with duplicate bytes across owners would block the global constraint;
    # keep the oldest of each digest before restoring it.
    op.execute(
        """
        DELETE FROM documents d
        USING documents older
        WHERE d.content_sha256 = older.content_sha256
          AND d.created_at > older.created_at
        """
    )
    op.create_unique_constraint("uq_documents_content_sha256", "documents", ["content_sha256"])
    op.drop_constraint("ck_documents_visibility", "documents", type_="check")
    op.drop_column("documents", "visibility")
    op.drop_column("documents", "owner_id")
