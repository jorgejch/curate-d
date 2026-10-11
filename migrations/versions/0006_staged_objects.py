"""Staged objects: the cache index naming the published upload per staging key.

Revision ID: 0006_staged_objects
Revises: 0005_workflow_views
Create Date: 2026-09-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_staged_objects"
down_revision: str | None = "0005_workflow_views"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the staged_objects table with its check constraints."""
    op.create_table(
        "staged_objects",
        sa.Column(
            "staging_key",
            sa.Text(),
            nullable=False,
            comment="Source-derived staging key. One published object per key is the "
            "invariant, so it is the primary key rather than a surrogate.",
        ),
        sa.Column(
            "attempt_id",
            sa.UUID(as_uuid=True),
            nullable=False,
            comment="Upload attempt that won this key. Part of the remote object name, so a "
            "losing writer's bytes are a collectable orphan rather than an overwrite.",
        ),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column(
            "sha256",
            sa.Text(),
            nullable=False,
            comment="Computed by the writer after upload and reverified on every download. "
            "Not an ETag: a multipart ETag is not a content hash.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
            comment="Creation time of the remote object, copied from the verified upload's "
            "LastModified. Not the row's insertion time, which is only what the server "
            "default supplies; age-based eviction reads the same value from the S3 listing.",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_staged_objects_size_non_negative"),
        sa.CheckConstraint(
            "sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_staged_objects_sha256_lowercase_hex",
        ),
        sa.CheckConstraint(
            "staging_key ~ '^[0-9a-f]{32}$'",
            name="ck_staged_objects_key_is_derived_identifier",
        ),
        sa.PrimaryKeyConstraint("staging_key", name="pk_staged_objects"),
        sa.UniqueConstraint("attempt_id", name="uq_staged_objects_attempt_id"),
    )


def downgrade() -> None:
    """Drop the staged_objects table."""
    op.drop_table("staged_objects")
