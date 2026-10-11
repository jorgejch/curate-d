"""Provider connections: one row per provider account, holding its OAuth tokens encrypted.

Tokens are stored as Fernet ciphertext; the key comes from the environment and is never
written here.

Revision ID: 0010_provider_connections
Revises: 0009_transfer_worker_caps
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_provider_connections"
down_revision: str | None = "0009_transfer_worker_caps"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the provider_connections table with its check constraints."""
    op.create_table(
        "provider_connections",
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default="healthy", nullable=False),
        sa.Column(
            "api_host",
            sa.Text(),
            nullable=True,
            comment="Regional API endpoint serving the account, as the provider reported it.",
        ),
        sa.Column(
            "access_token_ciphertext",
            sa.LargeBinary(),
            nullable=False,
            comment="Fernet ciphertext. The key lives in the environment, never in this "
            "database, so a dump of this table is not a credential leak.",
        ),
        sa.Column("refresh_token_ciphertext", sa.LargeBinary(), nullable=True),
        sa.Column("token_type", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Absolute access-token expiry. NULL where the provider declared none.",
        ),
        sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
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
        sa.CheckConstraint("provider IN ('pcloud')", name="ck_provider_connections_provider_known"),
        sa.CheckConstraint(
            "state IN ('healthy', 'expiring', 'requires_reauthentication')",
            name="ck_provider_connections_state_known",
        ),
        sa.CheckConstraint(
            "state <> 'requires_reauthentication' OR failure_reason IS NOT NULL",
            name="ck_provider_connections_reauthentication_has_reason",
        ),
        sa.PrimaryKeyConstraint("provider", "account", name="pk_provider_connections"),
    )


def downgrade() -> None:
    """Drop the provider_connections table."""
    op.drop_table("provider_connections")
