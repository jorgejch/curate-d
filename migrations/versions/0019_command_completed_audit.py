"""The audit entry for an operator command's outcome.

Adds `workflow_command_completed` to the audit entry types: once a Conductor command's
workflow has made its DBOS call, it records whether the command applied, and if not why,
beside the `workflow_command_issued` entry the gate recorded when it accepted the request.

The downgrade fails once a `workflow_command_completed` entry has been recorded: audit
entries are never deleted, so the narrower check cannot be restored over them.

Revision ID: 0019_command_completed_audit
Revises: 0018_command_request_unique
Create Date: 2026-10-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019_command_completed_audit"
down_revision: str | None = "0018_command_request_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TYPES_BEFORE = (
    "'classification_decided', 'classification_rejected', 'render_failed', "
    "'placement_started', 'placement_registered', 'placement_failed', 'placement_absent', "
    "'verification_succeeded', 'verification_failed', 'retention_decided', "
    "'source_read_failed', 'source_discrepancy_detected', 'workflow_command_issued'"
)
_TYPES_AFTER = f"{_TYPES_BEFORE}, 'workflow_command_completed'"


def upgrade() -> None:
    """Recreate audit_entries' ck_audit_entries_entry_type_known with workflow_command_completed."""
    op.drop_constraint("ck_audit_entries_entry_type_known", "audit_entries", type_="check")
    op.create_check_constraint(
        "ck_audit_entries_entry_type_known", "audit_entries", f"entry_type IN ({_TYPES_AFTER})"
    )


def downgrade() -> None:
    """Recreate audit_entries' ck_audit_entries_entry_type_known as it was before this revision."""
    op.drop_constraint("ck_audit_entries_entry_type_known", "audit_entries", type_="check")
    op.create_check_constraint(
        "ck_audit_entries_entry_type_known", "audit_entries", f"entry_type IN ({_TYPES_BEFORE})"
    )
