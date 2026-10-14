"""The registry of versions that hold the version lease.

Adds `lease_versions`: one row per application version whose workers registered before
launch, with the database time of the first registration. The recovery sweep forks or
retires a version's rows only if that version is registered and was registered before
its target, so a build that predates the lease, or the newer build a rollback replaced,
is never treated as dead.

Revision ID: 0020_lease_versions
Revises: 0019_command_completed_audit
Create Date: 2026-10-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_lease_versions"
down_revision: str | None = "0019_command_completed_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the lease_versions table."""
    op.create_table(
        "lease_versions",
        sa.Column("version", sa.Text(), nullable=False, comment="A DBOS application version."),
        sa.Column(
            "first_registered",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
            comment="When a worker of this version first registered, by the database clock; "
            "deploy order, which a version promotion does not rewrite.",
        ),
        sa.PrimaryKeyConstraint("version", name=op.f("pk_lease_versions")),
        comment="Versions whose workers hold the version lease while they run. The recovery "
        "sweep forks or retires only a registered version older than its target.",
    )


def downgrade() -> None:
    """Drop the lease_versions table."""
    op.drop_table("lease_versions")
