"""Scans and reported digests.

Adds the provider-reported SHA-1, SHA-256 and MD5 columns to source_objects, and the scans
and scan_folders tables that make an interrupted enumeration resumable from the folders
it had not yet listed.

Revision ID: 0011_scans
Revises: 0010_provider_connections
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_scans"
down_revision: str | None = "0010_provider_connections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIGESTS = (("reported_sha1", 40), ("reported_sha256", 64), ("reported_md5", 32))


def upgrade() -> None:
    """Add reported digest columns to source_objects; create scans and scan_folders."""
    for column, length in _DIGESTS:
        op.add_column(
            "source_objects",
            sa.Column(
                column,
                sa.Text(),
                nullable=True,
                comment=(
                    "SHA-1 the provider reports, from its metadata rather than a content read."
                    if column == "reported_sha1"
                    else None
                ),
            ),
        )
        op.create_check_constraint(
            f"ck_source_objects_{column}_shape",
            "source_objects",
            f"{column} ~ '^[0-9a-f]{{{length}}}$'",
        )

    op.create_table(
        "scans",
        sa.Column("id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("account", sa.Text(), nullable=False),
        sa.Column(
            "root",
            sa.Text(),
            nullable=False,
            comment="Configured subtree, as a provider path. One open scan per root at a time.",
        ),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'incomplete', 'complete')", name="ck_scans_status_known"
        ),
        sa.CheckConstraint(
            "status <> 'incomplete' OR failure_reason IS NOT NULL",
            name="ck_scans_incomplete_has_reason",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_scans"),
        comment="One enumeration of a subtree. An incomplete scan is resumed by the next one "
        "rather than restarted, from the folders it had not yet listed.",
    )
    op.create_index(
        "uq_scans_open_root",
        "scans",
        ["provider", "account", "root"],
        unique=True,
        postgresql_where=sa.text("status <> 'complete'"),
    )

    op.create_table(
        "scan_folders",
        sa.Column("scan_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "folder_id", sa.Text(), nullable=False, comment="Provider-native folder identifier."
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column(
            "listed_at",
            sa.DateTime(timezone=True),
            nullable=True,
            comment="Set once every file in the folder is recorded. NULL is the frontier.",
        ),
        sa.ForeignKeyConstraint(
            ["scan_id"],
            ["scans.id"],
            name="fk_scan_folders_scan_id_scans",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("scan_id", "folder_id", name="pk_scan_folders"),
    )


def downgrade() -> None:
    """Drop scan_folders and scans; drop the reported digest columns from source_objects."""
    op.drop_table("scan_folders")
    op.drop_index("uq_scans_open_root", table_name="scans")
    op.drop_table("scans")
    for column, _ in _DIGESTS:
        op.drop_constraint(f"ck_source_objects_{column}_shape", "source_objects", type_="check")
        op.drop_column("source_objects", column)
