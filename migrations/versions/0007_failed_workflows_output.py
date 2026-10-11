"""Read failed_workflows errors from DBOS 3's workflow_output table.

DBOS 3 moved workflow outputs and errors out of `workflow_status` into `workflow_output`.
Rows written by DBOS 2 keep their error in `workflow_status.error`, so the view takes the
new column first and falls back to the old one, matching how DBOS reads a result itself.
Without this, every failure recorded after the upgrade shows a NULL `error_payload`.

Like 0005, this reads DBOS's tables, so it needs the DBOS 3 system schema;
`migrations/env.py` brings it up to date before an online run.

Revision ID: 0007_failed_workflows_output
Revises: 0006_staged_objects
Create Date: 2026-09-26
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007_failed_workflows_output"
down_revision: str | None = "0006_staged_objects"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept in sync with curate_d.runtime.dbos_app.SYSTEM_SCHEMA.
SYSTEM_SCHEMA = "dbos"

FAILED_WORKFLOWS = f"""
CREATE OR REPLACE VIEW failed_workflows AS
SELECT
    status.workflow_uuid AS workflow_id,
    status.name AS workflow_name,
    coalesce(status.queue_name, '(unqueued)') AS queue_name,
    status.application_version,
    status.recovery_attempts,
    to_timestamp(status.updated_at / 1000.0) AS failed_at,
    coalesce(output.error, status.error) AS error_payload
FROM {SYSTEM_SCHEMA}.workflow_status AS status
LEFT JOIN {SYSTEM_SCHEMA}.workflow_output AS output
    ON output.workflow_uuid = status.workflow_uuid
WHERE status.status IN ('ERROR', 'CANCELLED', 'MAX_RECOVERY_ATTEMPTS_EXCEEDED')
ORDER BY status.updated_at DESC;
"""

# The 0005 definition, restored on downgrade.
FAILED_WORKFLOWS_STATUS_ONLY = f"""
CREATE OR REPLACE VIEW failed_workflows AS
SELECT
    workflow_uuid AS workflow_id,
    name AS workflow_name,
    coalesce(queue_name, '(unqueued)') AS queue_name,
    application_version,
    recovery_attempts,
    to_timestamp(updated_at / 1000.0) AS failed_at,
    error AS error_payload
FROM {SYSTEM_SCHEMA}.workflow_status
WHERE status IN ('ERROR', 'CANCELLED', 'MAX_RECOVERY_ATTEMPTS_EXCEEDED')
ORDER BY updated_at DESC;
"""


def upgrade() -> None:
    """Redefine failed_workflows to read error_payload from workflow_output first."""
    op.execute(FAILED_WORKFLOWS)


def downgrade() -> None:
    """Restore failed_workflows to read error_payload from workflow_status only."""
    op.execute(FAILED_WORKFLOWS_STATUS_ONLY)
