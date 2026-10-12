"""The resource_limit review reason.

Adds `resource_limit` to the review item reasons: content a CPU worker limit kept the
cheap tiers from reading, and that the remote tier would have been shown nothing of, goes
to review under it rather than to a remote call.

The downgrade maps existing `resource_limit` items to `low_confidence`, keeping the
original reason in their detail, so the narrower check can be restored over them.

Revision ID: 0020_review_resource_limit
Revises: 0019_command_completed_audit
Create Date: 2026-10-11
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020_review_resource_limit"
down_revision: str | None = "0019_command_completed_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REASONS_BEFORE = "'low_confidence', 'rejected_value', 'malformed_output', 'render_failed'"
_REASONS_AFTER = f"{_REASONS_BEFORE}, 'resource_limit'"


def upgrade() -> None:
    """Recreate review_items' ck_review_items_reason_known with resource_limit."""
    op.drop_constraint("ck_review_items_reason_known", "review_items", type_="check")
    op.create_check_constraint(
        "ck_review_items_reason_known", "review_items", f"reason IN ({_REASONS_AFTER})"
    )


def downgrade() -> None:
    """Map resource_limit review items to low_confidence and narrow the reason check."""
    op.execute(
        "UPDATE review_items SET reason = 'low_confidence', "
        "detail = detail || ' (reason before downgrade: resource_limit)' "
        "WHERE reason = 'resource_limit'"
    )
    op.drop_constraint("ck_review_items_reason_known", "review_items", type_="check")
    op.create_check_constraint(
        "ck_review_items_reason_known", "review_items", f"reason IN ({_REASONS_BEFORE})"
    )
