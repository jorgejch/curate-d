"""What a dry run evaluated with, which rules selected each destination, and unselected content.

Adds to dry_runs the source of the placement rules (the policy in force, a draft, or the
destinations the run named), the version in force when it was used, the rule names, and
whether the template set was a draft, with the draft documents themselves: a draft is
stored nowhere else, and DBOS retention eventually prunes the workflow input that carried
it. Adds to dry_run_items the rules that selected the item's destination, and the
`unselected` outcome for classified content no rule selects.

Runs recorded before this revision keep a null policy_source and empty rules.

The downgrade fails while an `unselected` item exists, since the narrower check cannot be
restored over it; it names the DELETE to run first rather than deleting review data itself.

Revision ID: 0021_dry_run_drafts
Revises: 0020_lease_versions
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021_dry_run_drafts"
down_revision: str | None = "0020_lease_versions"
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
_OUTCOMES_BEFORE = "'proposed', 'already_present', 'render_failed', 'unclassified'"
_OUTCOMES_AFTER = f"{_OUTCOMES_BEFORE}, 'unselected'"
_DELETE_UNSELECTED = "DELETE FROM dry_run_items WHERE outcome = 'unselected'"


def upgrade() -> None:
    """Add dry_runs' policy and draft columns, dry_run_items.rules, and the unselected outcome."""
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
            comment="The policy version in force that the run evaluated with; null for a draft "
            "or named destinations.",
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
    op.drop_constraint("ck_dry_run_items_outcome_known", "dry_run_items", type_="check")
    op.create_check_constraint(
        "ck_dry_run_items_outcome_known", "dry_run_items", f"outcome IN ({_OUTCOMES_AFTER})"
    )


def downgrade() -> None:
    """Restore ck_dry_run_items_outcome_known; drop dry_run_items.rules and dry_runs' columns.

    Raises:
        RuntimeError: an `unselected` item exists; the message names the DELETE to run first.
    """
    unselected = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM dry_run_items WHERE outcome = 'unselected'")
    )
    if unselected:
        raise RuntimeError(
            f"{unselected} dry_run_items rows have outcome 'unselected', which the restored "
            f"check refuses; run `{_DELETE_UNSELECTED}` first"
        )
    op.drop_constraint("ck_dry_run_items_outcome_known", "dry_run_items", type_="check")
    op.create_check_constraint(
        "ck_dry_run_items_outcome_known", "dry_run_items", f"outcome IN ({_OUTCOMES_BEFORE})"
    )
    op.drop_column("dry_run_items", "rules")
    op.drop_constraint("ck_dry_runs_policy_source_known", "dry_runs", type_="check")
    for column in _RUN_COLUMNS:
        op.drop_column("dry_runs", column)
