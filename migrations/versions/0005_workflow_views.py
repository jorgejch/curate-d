"""Views reporting workflow status by queue and state.

DBOS has thinner operational tooling than the alternatives, which is offset by its state
living in this project's own database. These views are that offset made concrete: they
answer "what is the system doing" and "what is stuck" from psql, with no external
dashboard and no dependency on a hosted console.

The views read DBOS's own tables, so they carry the same compatibility risk as any query
against another component's schema. They are views precisely so that a DBOS upgrade that
moves a column is a one-file migration rather than a hunt through the control plane.

Revision ID: 0005_workflow_views
Revises: 0004_audit_entries
Create Date: 2026-09-07
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005_workflow_views"
down_revision: str | None = "0004_audit_entries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept in sync with curate_d.runtime.dbos_app.SYSTEM_SCHEMA.
SYSTEM_SCHEMA = "dbos"

WORKFLOW_STATUS_BY_QUEUE = f"""
CREATE OR REPLACE VIEW workflow_status_by_queue AS
SELECT
    coalesce(queue_name, '(unqueued)') AS queue_name,
    -- Provider-facing queues are partitioned on provider identity, so a backlog is a
    -- per-partition fact. Aggregating it away would hide which provider is stalled.
    coalesce(queue_partition_key, '(unpartitioned)') AS partition_key,
    status,
    application_version,
    count(*) AS workflow_count,
    min(to_timestamp(created_at / 1000.0)) AS oldest_created_at,
    max(to_timestamp(updated_at / 1000.0)) AS newest_updated_at
FROM {SYSTEM_SCHEMA}.workflow_status
GROUP BY 1, 2, 3, 4;
"""

# Work that has been claimed but is not progressing. The application_version column is
# here on purpose: a workflow parked on a retired version needs a worker on that version
# to resume, and that is invisible in a plain count by state.
STALLED_WORKFLOWS = f"""
CREATE OR REPLACE VIEW stalled_workflows AS
SELECT
    workflow_uuid AS workflow_id,
    name AS workflow_name,
    coalesce(queue_name, '(unqueued)') AS queue_name,
    coalesce(queue_partition_key, '(unpartitioned)') AS partition_key,
    status,
    application_version,
    executor_id,
    recovery_attempts,
    to_timestamp(created_at / 1000.0) AS created_at,
    to_timestamp(updated_at / 1000.0) AS updated_at
FROM {SYSTEM_SCHEMA}.workflow_status
WHERE status IN ('PENDING', 'ENQUEUED')
ORDER BY updated_at;
"""

FAILED_WORKFLOWS = f"""
CREATE OR REPLACE VIEW failed_workflows AS
SELECT
    workflow_uuid AS workflow_id,
    name AS workflow_name,
    coalesce(queue_name, '(unqueued)') AS queue_name,
    application_version,
    recovery_attempts,
    to_timestamp(updated_at / 1000.0) AS failed_at,
    -- DBOS serializes the exception rather than storing a message, so this is a payload
    -- to be decoded through the DBOS API, not something to render to a person. Named
    -- accordingly so nobody puts it in a UI expecting a sentence.
    error AS error_payload
FROM {SYSTEM_SCHEMA}.workflow_status
WHERE status IN ('ERROR', 'CANCELLED', 'MAX_RECOVERY_ATTEMPTS_EXCEEDED')
ORDER BY updated_at DESC;
"""


def upgrade() -> None:
    """Create the workflow_status_by_queue, stalled_workflows, and failed_workflows views."""
    op.execute(WORKFLOW_STATUS_BY_QUEUE)
    op.execute(STALLED_WORKFLOWS)
    op.execute(FAILED_WORKFLOWS)


def downgrade() -> None:
    """Drop the three workflow monitoring views."""
    op.execute("DROP VIEW IF EXISTS failed_workflows")
    op.execute("DROP VIEW IF EXISTS stalled_workflows")
    op.execute("DROP VIEW IF EXISTS workflow_status_by_queue")
