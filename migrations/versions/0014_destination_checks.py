"""Destination checks, and the audit entry for a placement found missing.

Creates destination_checks, the latest result of looking for each destination's
registered placements: when it was checked, whether it could be reached, why not, and how
many placements were found missing. Adds `placement_absent` to the audit entry types.
Redescribes placements.attempts, which now also counts a copy moved to a new locator.

The downgrade fails once a `placement_absent` entry has been recorded: audit entries are
never deleted, so the narrower check cannot be restored over them.

Revision ID: 0014_destination_checks
Revises: 0013_placement_evidence
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_destination_checks"
down_revision: str | None = "0013_placement_evidence"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ATTEMPTS_BEFORE = "Failed attempts so far; part of the next attempt's workflow identity."
_ATTEMPTS_AFTER = "Attempts after the first; part of the next attempt's workflow identity."
_TYPES_BEFORE = (
    "'classification_decided', 'classification_rejected', 'render_failed', "
    "'placement_started', 'placement_registered', 'placement_failed', "
    "'verification_succeeded', 'verification_failed', 'retention_decided', "
    "'source_read_failed', 'source_discrepancy_detected'"
)
_TYPES_AFTER = (
    "'classification_decided', 'classification_rejected', 'render_failed', "
    "'placement_started', 'placement_registered', 'placement_failed', 'placement_absent', "
    "'verification_succeeded', 'verification_failed', 'retention_decided', "
    "'source_read_failed', 'source_discrepancy_detected'"
)


def upgrade() -> None:
    """Create destination_checks; allow placement_absent; redescribe placements.attempts."""
    op.create_table(
        "destination_checks",
        sa.Column("destination_name", sa.Text(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reachable", sa.Boolean(), nullable=False),
        sa.Column(
            "reason",
            sa.Text(),
            nullable=True,
            comment="Why the destination could not be checked.",
        ),
        sa.Column(
            "absent",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="Registered placements found missing by this check.",
        ),
        sa.CheckConstraint("absent >= 0", name="ck_destination_checks_absent_non_negative"),
        sa.PrimaryKeyConstraint("destination_name", name="pk_destination_checks"),
        comment="The latest check of each destination for its registered placements.",
    )
    op.drop_constraint("ck_audit_entries_entry_type_known", "audit_entries", type_="check")
    op.create_check_constraint(
        "ck_audit_entries_entry_type_known", "audit_entries", f"entry_type IN ({_TYPES_AFTER})"
    )
    op.alter_column(
        "placements",
        "attempts",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment=_ATTEMPTS_AFTER,
        existing_comment=_ATTEMPTS_BEFORE,
    )


def downgrade() -> None:
    """Restore the attempts description; disallow placement_absent; drop destination_checks."""
    op.alter_column(
        "placements",
        "attempts",
        existing_type=sa.Integer(),
        existing_nullable=False,
        comment=_ATTEMPTS_BEFORE,
        existing_comment=_ATTEMPTS_AFTER,
    )
    op.drop_constraint("ck_audit_entries_entry_type_known", "audit_entries", type_="check")
    op.create_check_constraint(
        "ck_audit_entries_entry_type_known", "audit_entries", f"entry_type IN ({_TYPES_BEFORE})"
    )
    op.drop_table("destination_checks")
