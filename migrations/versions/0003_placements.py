"""Placements: the per-destination identity layer.

Revision ID: 0003_placements
Revises: 0002_content_objects
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_placements"
down_revision: str | None = "0002_content_objects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the placements table with its indexes and check constraints."""
    op.create_table(
        "placements",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("content_object_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("destination_name", sa.Text(), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("template_version", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="pending", nullable=False),
        sa.Column(
            "verification_level",
            sa.SmallInteger(),
            server_default="0",
            nullable=False,
            comment="VerificationLevel ordinal. Ordered so a policy can require 'at least'.",
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("observed_source_digest", sa.Text(), nullable=True),
        sa.Column("observed_destination_digest", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state <> 'present' OR verified_at IS NOT NULL",
            name="ck_placements_present_implies_verified",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'copying', 'verifying', 'present', 'absent', 'failed', "
            "'untrusted')",
            name="ck_placements_state_known",
        ),
        sa.CheckConstraint(
            "state NOT IN ('failed', 'untrusted') OR failure_reason IS NOT NULL",
            name="ck_placements_failure_has_reason",
        ),
        sa.CheckConstraint(
            "verification_level BETWEEN 0 AND 3",
            name="ck_placements_verification_level_in_range",
        ),
        sa.ForeignKeyConstraint(
            ["content_object_id"],
            ["content_objects.id"],
            name="fk_placements_content_object_id_content_objects",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_placements"),
        sa.UniqueConstraint(
            "content_object_id",
            "destination_name",
            name="uq_placements_content_object_id_destination_name",
        ),
    )
    op.create_index(
        "ix_placements_destination_name_locator",
        "placements",
        ["destination_name", "locator"],
        unique=False,
    )
    op.create_index("ix_placements_state", "placements", ["state"], unique=False)


def downgrade() -> None:
    """Drop the placements table and its indexes."""
    op.drop_index("ix_placements_state", table_name="placements")
    op.drop_index("ix_placements_destination_name_locator", table_name="placements")
    op.drop_table("placements")
