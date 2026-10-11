"""Placement evidence and attempts, and dry-run results.

Adds to placements the source a copy was made from, the count of failed attempts (part of
the next attempt's workflow identity), and the evidence both sides left, recorded before
verification assesses it. Creates dry_runs and dry_run_items, which persist what a dry run
proposed for export.

Revision ID: 0013_placement_evidence
Revises: 0012_classification
Create Date: 2026-09-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013_placement_evidence"
down_revision: str | None = "0012_classification"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add placement evidence, attempts and source; create dry_runs and dry_run_items."""
    op.add_column(
        "placements",
        sa.Column(
            "source_object_id",
            sa.UUID(as_uuid=True),
            nullable=True,
            comment="The source copied by the latest attempt.",
        ),
    )
    op.create_foreign_key(
        "fk_placements_source_object_id_source_objects",
        "placements",
        "source_objects",
        ["source_object_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "placements",
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
            comment="Failed attempts so far; part of the next attempt's workflow identity.",
        ),
    )
    op.create_check_constraint("ck_placements_attempts_non_negative", "placements", "attempts >= 0")
    op.add_column(
        "placements",
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Both sides' observed values from the latest copy, recorded before "
            "verification. Validated by the application.",
        ),
    )

    op.create_table(
        "dry_runs",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("template_version", sa.Text(), nullable=False),
        sa.Column("taxonomy_version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_dry_runs"),
        comment="One evaluation of what placement would do, persisted for export.",
    )
    op.create_table(
        "dry_run_items",
        sa.Column("run_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("content_object_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "destination_name",
            sa.Text(),
            nullable=False,
            comment="Empty for content that did not reach rendering.",
        ),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("paths", sa.Text(), nullable=False, comment="The content's source paths."),
        sa.Column(
            "facts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="Derived facts, validated by the application.",
        ),
        sa.Column("locator", sa.Text(), nullable=True),
        sa.Column("expected_level", sa.SmallInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "rejected_values",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="Taxonomy values refused for this content, to inform refinement.",
        ),
        sa.CheckConstraint(
            "outcome IN ('proposed', 'already_present', 'render_failed', 'unclassified')",
            name="ck_dry_run_items_outcome_known",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["dry_runs.id"], name="fk_dry_run_items_run_id_dry_runs", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "run_id", "content_object_id", "destination_name", name="pk_dry_run_items"
        ),
    )


def downgrade() -> None:
    """Drop dry_run_items and dry_runs; drop placement evidence, attempts and source."""
    op.drop_table("dry_run_items")
    op.drop_table("dry_runs")
    op.drop_column("placements", "evidence")
    op.drop_constraint("ck_placements_attempts_non_negative", "placements", type_="check")
    op.drop_column("placements", "attempts")
    op.drop_constraint(
        "fk_placements_source_object_id_source_objects", "placements", type_="foreignkey"
    )
    op.drop_column("placements", "source_object_id")
