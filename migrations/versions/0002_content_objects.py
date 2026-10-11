"""Content objects: the content-addressed identity layer.

Also links source objects to them. The column and its foreign key live here rather than
in 0001 because the link belongs to the content layer, and 0001 must remain applicable on
its own.

Revision ID: 0002_content_objects
Revises: 0001_source_objects
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_content_objects"
down_revision: str | None = "0001_source_objects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create content_objects and link source_objects.content_object_id to it."""
    op.create_table(
        "content_objects",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("fingerprint_algorithm", sa.Text(), nullable=False),
        sa.Column("fingerprint_digest", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column(
            "perceptual_fingerprint",
            sa.BigInteger(),
            nullable=True,
            comment="64-bit image hash, stored signed. Compared by bit distance, not equality, "
            "so it is deliberately not indexed for equality lookup.",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "fingerprint_algorithm IN ('sha256', 'sha1', 'md5')",
            name="ck_content_objects_fingerprint_algorithm_known",
        ),
        sa.CheckConstraint(
            "fingerprint_digest ~ '^[0-9a-f]+$'",
            name="ck_content_objects_fingerprint_digest_is_hex",
        ),
        sa.CheckConstraint(
            "media_type ~ '^[a-z0-9][^/]*/[a-z0-9][^/]*$'",
            name="ck_content_objects_media_type_shape",
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_content_objects_size_non_negative"),
        sa.PrimaryKeyConstraint("id", name="pk_content_objects"),
        sa.UniqueConstraint(
            "fingerprint_algorithm",
            "fingerprint_digest",
            name="uq_content_objects_fingerprint_algorithm_fingerprint_digest",
        ),
    )
    op.add_column(
        "source_objects",
        sa.Column(
            "content_object_id",
            sa.UUID(as_uuid=True),
            nullable=True,
            comment="Set once the file's content has been fingerprinted.",
        ),
    )
    # RESTRICT rather than CASCADE: content is never deleted out from under a source
    # object that still points at it, and a delete that would orphan one is a bug worth
    # surfacing rather than absorbing.
    op.create_foreign_key(
        "fk_source_objects_content_object_id_content_objects",
        "source_objects",
        "content_objects",
        ["content_object_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_source_objects_content_object_id",
        "source_objects",
        ["content_object_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the content_object_id link and the content_objects table."""
    op.drop_index("ix_source_objects_content_object_id", table_name="source_objects")
    op.drop_constraint(
        "fk_source_objects_content_object_id_content_objects",
        "source_objects",
        type_="foreignkey",
    )
    op.drop_column("source_objects", "content_object_id")
    op.drop_table("content_objects")
