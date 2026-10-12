"""What a dry run evaluated with, and which rule selected each destination.

Adds to dry_runs the source of the placement rules (the policy in force, a draft, or the
destinations the run named), the version in force when it was used, the rule names, and
whether the template set was a draft, with the draft documents themselves: a draft is
stored nowhere else, and DBOS retention eventually prunes the workflow input that carried
it. Adds to dry_run_items the rules that selected the item's destination.

Runs recorded before this revision keep a null policy_source and empty rules.

Revision ID: 0020_dry_run_drafts
Revises: 0019_command_completed_audit
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020_dry_run_drafts"
down_revision: str | None = "0019_command_completed_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_COLUMNS = (
    "templates_document",
    "policy_document",
    "templates_draft",
    "policy_rules",
    "policy_version",
    "policy_source",
)


def upgrade() -> None:
    """Add dry_runs' policy and draft columns and dry_run_items.rules."""
    op.add_column(
        "dry_runs",
        sa.Column(
            "policy_source",
            sa.Text(),
            nullable=True,
            comment="Where the placement rules came from; null for runs recorded before it was.",
        ),
    )
    op.create_check_constraint(
        "ck_dry_runs_policy_source_known",
        "dry_runs",
        "policy_source IN ('active', 'draft', 'destinations')",
    )
    op.add_column(
        "dry_runs",
        sa.Column(
            "policy_version",
            sa.Integer(),
            nullable=True,
            comment="The policy version in force that the run evaluated with; null for a draft.",
        ),
    )
    op.add_column(
        "dry_runs",
        sa.Column(
            "policy_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="The names of the placement rules evaluated, in policy order.",
        ),
    )
    op.add_column(
        "dry_runs",
        sa.Column(
            "templates_draft",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
            comment="Whether the run rendered with a draft template set.",
        ),
    )
    op.add_column(
        "dry_runs",
        sa.Column(
            "policy_document",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="The draft policy evaluated, so the run stays reviewable; null otherwise.",
        ),
    )
    op.add_column(
        "dry_runs",
        sa.Column(
            "templates_document",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
            comment="The draft template set rendered with; null otherwise.",
        ),
    )
    op.add_column(
        "dry_run_items",
        sa.Column(
            "rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="The placement rules that selected this destination; empty when the run "
            "named its destinations.",
        ),
    )


def downgrade() -> None:
    """Drop dry_run_items.rules and dry_runs' policy and draft columns."""
    op.drop_column("dry_run_items", "rules")
    op.drop_constraint("ck_dry_runs_policy_source_known", "dry_runs", type_="check")
    for column in _RUN_COLUMNS:
        op.drop_column("dry_runs", column)
