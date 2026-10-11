"""Cap each transfer queue per worker and lower transfer-small's partition limit to match.

`runtime/queues.py` now registers transfer-small and transfer-large with a per-worker
`worker_concurrency` of 1, the minimum workable value, because a pod's scratch storage
bounds its transfers whatever provider they come from. DBOS requires that cap to be at
least the partition limit, so transfer-small's per-worker partition limit drops from 4 to
1 as well. Registration uses `never_update`, so rows written by earlier code keep their
old values; this brings them in line. A partition limit the control plane changed at
runtime is kept, and the cap is raised to it rather than set below it. A cap already set
is left alone, and a database with no transfer rows is unchanged.

Like 0005, 0007 and 0008, this reads DBOS's tables, so it needs the DBOS system schema;
`migrations/env.py` brings it up to date before an online run.

Revision ID: 0009_transfer_worker_caps
Revises: 0008_partition_worker_limits
Create Date: 2026-09-27
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009_transfer_worker_caps"
down_revision: str | None = "0008_partition_worker_limits"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Kept in sync with curate_d.runtime.dbos_app.SYSTEM_SCHEMA.
SYSTEM_SCHEMA = "dbos"

# The transfer queues and defaults as of this revision, kept literal so the migration does
# not change meaning if curate_d.runtime.queues later does.
TRANSFER_QUEUES = ("transfer-small", "transfer-large")
OLD_SMALL_PARTITION_LIMIT = 4
MINIMUM = 1

_QUEUE_LIST = ", ".join(f"'{name}'" for name in TRANSFER_QUEUES)

LOWER_SMALL_PARTITION = f"""
UPDATE {SYSTEM_SCHEMA}.queues
   SET partition_worker_concurrency = {MINIMUM}
 WHERE name = 'transfer-small'
   AND partition_worker_concurrency = {OLD_SMALL_PARTITION_LIMIT}
"""

SET_WORKER_CAP = f"""
UPDATE {SYSTEM_SCHEMA}.queues
   SET worker_concurrency = GREATEST({MINIMUM}, COALESCE(partition_worker_concurrency, {MINIMUM}))
 WHERE name IN ({_QUEUE_LIST})
   AND worker_concurrency IS NULL
"""

CLEAR_WORKER_CAP = f"""
UPDATE {SYSTEM_SCHEMA}.queues
   SET worker_concurrency = NULL
 WHERE name IN ({_QUEUE_LIST})
"""

RESTORE_SMALL_PARTITION = f"""
UPDATE {SYSTEM_SCHEMA}.queues
   SET partition_worker_concurrency = {OLD_SMALL_PARTITION_LIMIT}
 WHERE name = 'transfer-small'
   AND partition_worker_concurrency = {MINIMUM}
"""


def upgrade() -> None:
    """Lower transfer-small's partition limit and set worker_concurrency on transfer queues."""
    op.execute(LOWER_SMALL_PARTITION)
    op.execute(SET_WORKER_CAP)


def downgrade() -> None:
    """Clear the transfer queues' worker_concurrency and restore transfer-small's limit of 4."""
    op.execute(CLEAR_WORKER_CAP)
    op.execute(RESTORE_SMALL_PARTITION)
