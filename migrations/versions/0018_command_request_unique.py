"""One audit entry per Conductor request.

Adds a partial unique index on the `request_id` a `workflow_command_issued` entry's details
carry. Conductor may deliver a request more than once; the gate inserts the entry with
`ON CONFLICT DO NOTHING` against this index, so a redelivery records no second entry, and
enqueues the command's workflow under an ID derived from the request, so a redelivery runs
nothing twice either. The index's expression and predicate come from
`curate_d.ledger.tables`, which the conflict target uses too, so the two match.

The upgrade fails if a request already has two entries; until this revision each request
ran in a workflow keyed on its ID, which recorded its entry once.

Revision ID: 0018_command_request_unique
Revises: 0017_workflow_command_audit
Create Date: 2026-10-04
"""

from collections.abc import Sequence

from alembic import op

from curate_d.ledger.tables import COMMAND_ENTRIES, COMMAND_REQUEST_ID

revision: str = "0018_command_request_unique"
down_revision: str | None = "0017_workflow_command_audit"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create audit_entries' partial unique index uq_audit_entries_command_request_id."""
    op.create_index(
        "uq_audit_entries_command_request_id",
        "audit_entries",
        [COMMAND_REQUEST_ID],
        unique=True,
        postgresql_where=COMMAND_ENTRIES,
    )


def downgrade() -> None:
    """Drop audit_entries' uq_audit_entries_command_request_id."""
    op.drop_index("uq_audit_entries_command_request_id", table_name="audit_entries")
