"""Staging reservations: in-flight uploads' claims on staging headroom.

Adds `staging_reservations`: one row per admitted upload attempt, naming its staging key,
the bytes it reserved and an expiry by the database clock. Admission counts live rows as
occupancy beside the object-store listing, so the staging writer lock no longer has to
span an upload to keep two workers from spending the same headroom. A row is consumed by
the registration that publishes its attempt, deleted when the reserving block exits
without registering, and ignored once expired; it holds only in-flight uploads, so it
needs no index beyond its key.

Revision ID: 0021_staging_reservations
Revises: 0020_lease_versions
Create Date: 2026-10-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0021_staging_reservations"
down_revision: str | None = "0020_lease_versions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the staging_reservations table with its check constraints."""
    op.create_table(
        "staging_reservations",
        sa.Column(
            "attempt_id",
            sa.UUID(as_uuid=True),
            nullable=False,
            comment="Upload attempt holding the reservation; part of the remote object name.",
        ),
        sa.Column(
            "staging_key",
            sa.Text(),
            nullable=False,
            comment="Staging key the attempt uploads under.",
        ),
        sa.Column(
            "reserved_bytes",
            sa.BigInteger(),
            nullable=False,
            comment="Declared maximum admission counted for the attempt.",
        ),
        sa.Column(
            "reserved_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="When admission wrote the reservation, by the database clock.",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="Database time after which the reservation is neither counted nor "
            "consumed. Never compared against a worker's clock.",
        ),
        sa.CheckConstraint(
            "reserved_bytes >= 0",
            name="ck_staging_reservations_reserved_non_negative",
        ),
        sa.CheckConstraint(
            "staging_key ~ '^[0-9a-f]{32}$'",
            name="ck_staging_reservations_key_is_derived_identifier",
        ),
        sa.PrimaryKeyConstraint("attempt_id", name="pk_staging_reservations"),
    )


def downgrade() -> None:
    """Drop the staging_reservations table."""
    op.drop_table("staging_reservations")
