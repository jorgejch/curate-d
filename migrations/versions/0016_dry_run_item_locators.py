"""Dry-run items by locator.

Creates ix_dry_run_items_run_id_destination_name_locator. A dry run treats a locator it
already proposed for other content as taken, and looks up earlier pages' proposals by
run, destination and locator.

Revision ID: 0016_dry_run_item_locators
Revises: 0015_unique_placement_locator
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016_dry_run_item_locators"
down_revision: str | None = "0015_unique_placement_locator"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ix_dry_run_items_run_id_destination_name_locator."""
    op.create_index(
        "ix_dry_run_items_run_id_destination_name_locator",
        "dry_run_items",
        ["run_id", "destination_name", "locator"],
        unique=False,
    )


def downgrade() -> None:
    """Drop ix_dry_run_items_run_id_destination_name_locator."""
    op.drop_index("ix_dry_run_items_run_id_destination_name_locator", table_name="dry_run_items")
