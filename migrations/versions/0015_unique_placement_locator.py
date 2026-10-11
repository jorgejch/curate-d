"""One content object per locator at a destination.

Replaces the plain index on placements (destination_name, locator) with a unique
constraint. Two content objects rendering to one path were both copied there, the second
overwriting the first while both stayed registered present; placement now reserves the
locator when a copy starts, and the constraint is the database's backstop for that.

The upgrade fails if duplicate locators were already recorded; nothing before this
revision's release wrote placements.

Revision ID: 0015_unique_placement_locator
Revises: 0014_destination_checks
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015_unique_placement_locator"
down_revision: str | None = "0014_destination_checks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace ix_placements_destination_name_locator with a unique constraint."""
    op.drop_index("ix_placements_destination_name_locator", table_name="placements")
    op.create_unique_constraint(
        "uq_placements_destination_name_locator", "placements", ["destination_name", "locator"]
    )


def downgrade() -> None:
    """Restore the non-unique ix_placements_destination_name_locator."""
    op.drop_constraint("uq_placements_destination_name_locator", "placements", type_="unique")
    op.create_index(
        "ix_placements_destination_name_locator",
        "placements",
        ["destination_name", "locator"],
        unique=False,
    )
