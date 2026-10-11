"""Classification: versioned configuration, facts per content object, review, remote spend.

Creates configurations (every version of the taxonomy, templates and policy, one active
per kind), classifications (the facts recorded for each content object), review_items
(content awaiting a person, one open item per content object), remote_spend (the remote
model's spend per calendar month against its cap) and classification_holds (content
waiting for the next spend period).

Revision ID: 0012_classification
Revises: 0011_scans
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_classification"
down_revision: str | None = "0011_scans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create configurations, classifications, review_items, remote_spend and holds."""
    op.create_table(
        "configurations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.Text(), nullable=False, comment="taxonomy, templates or policy."),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "document",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="The validated configuration document for this version.",
        ),
        sa.Column("active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.CheckConstraint(
            "kind IN ('taxonomy', 'templates', 'policy')", name="ck_configurations_kind_known"
        ),
        sa.CheckConstraint("version >= 1", name="ck_configurations_version_positive"),
        sa.PrimaryKeyConstraint("id", name="pk_configurations"),
        sa.UniqueConstraint("kind", "version", name="uq_configurations_kind_version"),
        comment="Every configuration version ever activated. Changing configuration adds a "
        "version; the previous one stays, so a rejected change leaves it in force.",
    )
    op.create_index(
        "uq_configurations_active_kind",
        "configurations",
        ["kind"],
        unique=True,
        postgresql_where=sa.text("active"),
    )

    op.create_table(
        "classifications",
        sa.Column(
            "content_object_id",
            sa.UUID(as_uuid=True),
            nullable=False,
            comment="One classification per content fingerprint: repeat content reuses it.",
        ),
        sa.Column("tier", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=True),
        sa.Column("taxonomy_version", sa.Integer(), nullable=True),
        sa.Column("produced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("media_kind", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("subcategory", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("latitude", sa.Float(), nullable=True),
        sa.Column("longitude", sa.Float(), nullable=True),
        sa.Column("place_label", sa.Text(), nullable=True),
        sa.Column("event_group", sa.Text(), nullable=True),
        sa.Column("correspondent", sa.Text(), nullable=True),
        sa.Column("language", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "tier IN ('deterministic', 'local_inference', 'remote_model', 'human_review')",
            name="ck_classifications_tier_known",
        ),
        sa.CheckConstraint(
            "media_kind IN ('document', 'photo', 'video', 'audio', 'archive', 'other')",
            name="ck_classifications_media_kind_known",
        ),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 1", name="ck_classifications_confidence_in_range"
        ),
        sa.CheckConstraint(
            "(latitude IS NULL) = (longitude IS NULL)", name="ck_classifications_place_complete"
        ),
        sa.ForeignKeyConstraint(
            ["content_object_id"],
            ["content_objects.id"],
            name="fk_classifications_content_object_id_content_objects",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("content_object_id", name="pk_classifications"),
        comment="Facts per content object. Columns rather than a document, so facts can be "
        "queried and clustered in place.",
    )
    op.create_index("ix_classifications_captured_at", "classifications", ["captured_at"])

    op.create_table(
        "review_items",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("content_object_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column(
            "rejected_values",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
            comment="Serialized RejectedValue list, validated by the application.",
        ),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "reason IN ('low_confidence', 'rejected_value', 'malformed_output', 'render_failed')",
            name="ck_review_items_reason_known",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved', 'excluded')", name="ck_review_items_status_known"
        ),
        sa.CheckConstraint(
            "status = 'open' OR resolved_at IS NOT NULL", name="ck_review_items_closed_has_time"
        ),
        sa.ForeignKeyConstraint(
            ["content_object_id"],
            ["content_objects.id"],
            name="fk_review_items_content_object_id_content_objects",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_review_items"),
    )
    op.create_index(
        "uq_review_items_open_content",
        "review_items",
        ["content_object_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )

    op.create_table(
        "remote_spend",
        sa.Column("period", sa.Text(), nullable=False, comment="Calendar month, YYYY-MM, in UTC."),
        sa.Column("spent_usd", sa.Numeric(14, 6), server_default=sa.text("0"), nullable=False),
        sa.Column("calls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cap_usd", sa.Numeric(14, 6), nullable=False),
        sa.Column(
            "cap_reached_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="When the period's spend first reached its cap; reported by the control plane.",
        ),
        sa.CheckConstraint("spent_usd >= 0", name="ck_remote_spend_spent_non_negative"),
        sa.PrimaryKeyConstraint("period", name="pk_remote_spend"),
    )

    op.create_table(
        "classification_holds",
        sa.Column("content_object_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "period", sa.Text(), nullable=False, comment="The spend period whose cap held it."
        ),
        sa.Column("held_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "request",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            comment="The prepared remote request and base facts, so the retry needs no staged "
            "content. Validated by the application.",
        ),
        sa.ForeignKeyConstraint(
            ["content_object_id"],
            ["content_objects.id"],
            name="fk_classification_holds_content_object_id_content_objects",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("content_object_id", name="pk_classification_holds"),
        comment="Content waiting for the next spend period rather than being misclassified.",
    )


def downgrade() -> None:
    """Drop classification_holds, remote_spend, review_items, classifications, configurations."""
    op.drop_table("classification_holds")
    op.drop_table("remote_spend")
    op.drop_index("uq_review_items_open_content", table_name="review_items")
    op.drop_table("review_items")
    op.drop_index("ix_classifications_captured_at", table_name="classifications")
    op.drop_table("classifications")
    op.drop_index("uq_configurations_active_kind", table_name="configurations")
    op.drop_table("configurations")
