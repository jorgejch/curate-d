"""The audit entry for an operator command against a workflow.

Adds `workflow_command_issued` to the audit entry types: a cancel, resume, fork, rewind or
delete issued against a workflow is recorded before it takes effect.

The downgrade fails once a `workflow_command_issued` entry has been recorded: audit
entries are never deleted, so the narrower check cannot be restored over them.

Revision ID: 0017_workflow_command_audit
Revises: 0016_dry_run_item_locators
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017_workflow_command_audit"
down_revision: str | None = "0016_dry_run_item_locators"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TYPES_BEFORE = (
    "'classification_decided', 'classification_rejected', 'render_failed', "
    "'placement_started', 'placement_registered', 'placement_failed', 'placement_absent', "
    "'verification_succeeded', 'verification_failed', 'retention_decided', "
    "'source_read_failed', 'source_discrepancy_detected'"
)
_TYPES_AFTER = (
    "'classification_decided', 'classification_rejected', 'render_failed', "
    "'placement_started', 'placement_registered', 'placement_failed', 'placement_absent', "
    "'verification_succeeded', 'verification_failed', 'retention_decided', "
    "'source_read_failed', 'source_discrepancy_detected', 'workflow_command_issued'"
)


def upgrade() -> None:
    """Recreate audit_entries' ck_audit_entries_entry_type_known with workflow_command_issued."""
    op.drop_constraint("ck_audit_entries_entry_type_known", "audit_entries", type_="check")
    op.create_check_constraint(
        "ck_audit_entries_entry_type_known", "audit_entries", f"entry_type IN ({_TYPES_AFTER})"
    )


def downgrade() -> None:
    """Recreate audit_entries' ck_audit_entries_entry_type_known without workflow_command_issued."""
    op.drop_constraint("ck_audit_entries_entry_type_known", "audit_entries", type_="check")
    op.create_check_constraint(
        "ck_audit_entries_entry_type_known", "audit_entries", f"entry_type IN ({_TYPES_BEFORE})"
    )
