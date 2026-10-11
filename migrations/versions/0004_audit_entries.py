"""Append-only audit trail.

The append-only property is enforced by trigger rather than by application convention: a
bug, a later migration, or a person with psql must all be unable to rewrite history.

Revision ID: 0004_audit_entries
Revises: 0003_placements
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from curate_d.ledger.tables import (
    APPEND_ONLY_FUNCTION,
    APPEND_ONLY_TRIGGER,
    APPEND_ONLY_TRUNCATE_TRIGGER,
    DROP_APPEND_ONLY,
)

revision: str = "0004_audit_entries"
down_revision: str | None = "0003_placements"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create audit_entries with its indexes and the append-only enforcement triggers."""
    op.create_table(
        "audit_entries",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("entry_type", sa.Text(), nullable=False),
        sa.Column("source_object_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("content_object_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("placement_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "decided_by",
            sa.Text(),
            nullable=False,
            comment="Which tier, rule, or operator produced the entry.",
        ),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="Serialized AuditDetails. Validated by the application before write; the "
            "column is schemaless only in the sense that entry types differ in shape.",
        ),
        sa.CheckConstraint(
            "entry_type IN ('classification_decided', 'classification_rejected', "
            "'render_failed', 'placement_started', 'placement_registered', 'placement_failed', "
            "'verification_succeeded', 'verification_failed', 'retention_decided', "
            "'source_read_failed', 'source_discrepancy_detected')",
            name="ck_audit_entries_entry_type_known",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_entries"),
        comment="Append-only. Updates and deletes are rejected by trigger, not by convention.",
    )
    op.create_index(
        "ix_audit_entries_content_object_id_occurred_at",
        "audit_entries",
        ["content_object_id", "occurred_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_entries_entry_type_occurred_at",
        "audit_entries",
        ["entry_type", "occurred_at"],
        unique=False,
    )
    op.execute(APPEND_ONLY_FUNCTION)
    op.execute(APPEND_ONLY_TRIGGER)
    op.execute(APPEND_ONLY_TRUNCATE_TRIGGER)


def downgrade() -> None:
    """Drop the append-only triggers/function and the audit_entries table."""
    op.execute(DROP_APPEND_ONLY)
    op.drop_index("ix_audit_entries_entry_type_occurred_at", table_name="audit_entries")
    op.drop_index("ix_audit_entries_content_object_id_occurred_at", table_name="audit_entries")
    op.drop_table("audit_entries")
