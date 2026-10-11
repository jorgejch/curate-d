"""Move the provider-partitioned queues' partition limit from global to per worker.

`runtime/queues.py` registers scan, transfer-small and transfer-large with
`partition_worker_concurrency` and no `partition_concurrency`. Registration uses
`never_update`, so a row written by the earlier code keeps its global
`partition_concurrency` and a NULL `partition_worker_concurrency` across every restart.
This moves the value across, keeping whatever the control plane set at runtime rather
than resetting it to the code default. A row already carrying a per-worker limit is left
alone, and a database whose rows the new code created, or that has none yet, is
unchanged.

`partition_queue` needs no update: DBOS keeps it true while any partition limit is set,
and the per-worker limit and the partition rate limiter still are.

Like 0005 and 0007, this reads DBOS's tables, so it needs the DBOS system schema;
`migrations/env.py` brings it up to date before an online run.

Revision ID: 0008_partition_worker_limits
Revises: 0007_failed_workflows_output
Create Date: 2026-09-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008_partition_worker_limits"
down_revision: str | None = "0007_failed_workflows_output"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept in sync with curate_d.runtime.dbos_app.SYSTEM_SCHEMA.
SYSTEM_SCHEMA = "dbos"

# The partitioned queues as of this revision, kept literal so the migration does not
# change meaning if curate_d.runtime.queues.PARTITIONED_QUEUES later does.
PARTITIONED_QUEUES = ("scan", "transfer-small", "transfer-large")

_QUEUE_LIST = ", ".join(f"'{name}'" for name in PARTITIONED_QUEUES)

TO_PER_WORKER = f"""
UPDATE {SYSTEM_SCHEMA}.queues
   SET partition_worker_concurrency = partition_concurrency,
       partition_concurrency = NULL
 WHERE name IN ({_QUEUE_LIST})
   AND partition_concurrency IS NOT NULL
   AND partition_worker_concurrency IS NULL
"""

TO_GLOBAL = f"""
UPDATE {SYSTEM_SCHEMA}.queues
   SET partition_concurrency = partition_worker_concurrency,
       partition_worker_concurrency = NULL
 WHERE name IN ({_QUEUE_LIST})
   AND partition_worker_concurrency IS NOT NULL
   AND partition_concurrency IS NULL
"""


def upgrade() -> None:
    """Move dbos.queues.partition_concurrency into partition_worker_concurrency."""
    op.execute(TO_PER_WORKER)


def downgrade() -> None:
    """Move dbos.queues.partition_worker_concurrency back, restoring the global limit."""
    op.execute(TO_GLOBAL)
