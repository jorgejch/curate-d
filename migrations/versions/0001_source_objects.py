"""Source objects: the provider-native identity layer.

Revision ID: 0001_source_objects
Revises:
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_source_objects"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the source_objects table with its indexes and check constraints."""
    op.create_table(
        "source_objects",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column(
            "native_id",
            sa.Text(),
            nullable=False,
            comment="Provider-assigned file identifier; stable across renames within a provider.",
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("modified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "version_marker",
            sa.Text(),
            nullable=True,
            comment="Provider change token. NULL where the provider supplies none.",
        ),
        sa.Column("state", sa.Text(), server_default="discovered", nullable=False),
        sa.Column("change_signal", sa.Text(), nullable=False),
        sa.Column(
            "last_verified_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When a full rescan last confirmed the recorded fingerprint. NULL means "
            "never re-examined, which is where a resumed full rescan picks up.",
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_at",
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
            "change_signal IN ('version_marker', 'size_and_modified_time', 'content_fingerprint')",
            name="ck_source_objects_change_signal_known",
        ),
        sa.CheckConstraint("provider IN ('pcloud')", name="ck_source_objects_provider_known"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_source_objects_size_non_negative"),
        sa.CheckConstraint(
            "state <> 'failed' OR failure_reason IS NOT NULL",
            name="ck_source_objects_failure_has_reason",
        ),
        sa.CheckConstraint(
            "state IN ('discovered', 'fingerprinted', 'failed', 'excluded')",
            name="ck_source_objects_state_known",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_source_objects"),
        sa.UniqueConstraint(
            "provider",
            "account",
            "native_id",
            name="uq_source_objects_provider_account_native_id",
        ),
    )
    op.create_index(
        "ix_source_objects_last_verified_at", "source_objects", ["last_verified_at"], unique=False
    )
    op.create_index(
        "ix_source_objects_provider_account_path",
        "source_objects",
        ["provider", "account", "path"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the source_objects table and its indexes."""
    op.drop_index("ix_source_objects_provider_account_path", table_name="source_objects")
    op.drop_index("ix_source_objects_last_verified_at", table_name="source_objects")
    op.drop_table("source_objects")
