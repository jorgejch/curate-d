"""Pruning workflow history, and the line between it and the audit trail.

These two look alike and are not, which is the whole reason this module exists.

A **workflow record** is operational exhaust. It answers "what did the executor do last
Tuesday" and stops being interesting once the run is old. Modelling puts the workflow
tables at roughly 60% of total database size, dominated by one step-output row per step
per workflow, so pruning them is the single largest lever on storage this system has.

An **audit entry** is the durable answer to "why is this file here" and "why was this
original released". It has to outlive the workflow that produced it by as long as the
file exists. Pruning it would save comparatively little and destroy the record the
system exists to keep, so nothing here touches it — the append-only trigger on
`audit_entries` would reject the attempt anyway, and that belt-and-braces is deliberate.

The retention period is configuration rather than a constant because the right value
depends on how often anyone actually looks, which is not knowable in advance.

A record is eligible once it is terminal (SUCCESS, ERROR, CANCELLED or
MAX_RECOVERY_ATTEMPTS_EXCEEDED) and its `completed_at` is at or before the cutoff: age is
measured from completion, not creation. DBOS sets that column on every terminal transition
and clears it on resume, so PENDING, ENQUEUED, and DELAYED rows survive regardless of age.
Work parked across a long deployment is therefore never pruned out from under the recovery
sweep in `sweep.py`.

The prune goes through DBOS's public API: `DBOS.list_workflows(completed_before=...)` reads
one batch of eligible IDs and `DBOS.delete_workflows` removes them with their inputs,
outputs and step rows, until a batch comes back empty. DBOS's own collector has no public
binding, so this replaces it, and accepts what that costs:

- No retention advisory lock. Two prunes are not expected to overlap: one schedule enqueues
  them on the maintenance queue, a tick a day; and a second one running at once would only
  find fewer rows to delete.
- No VACUUM of the workflow tables after a prune; autovacuum reclaims the space.
- The list and the delete are separate calls. A workflow resumed by hand in between is
  deleted although it is live again. Only an operator resume of a record older than the
  retention period can hit that window.
- Only this application's rows, and unowned ones, are listed: `list_workflows` defaults
  to the calling application, where DBOS's collector swept the whole system database.

`delete_children` is not set: a child workflow carries its own `completed_at` and ages out
on its own.

DBOS does not prune the ledger datasource's `datasource_outputs`, one row per datasource
transaction per workflow, which lives in the ledger database rather than the system one;
nothing in DBOS deletes from it except a rewind. This module prunes it after the workflow
records, and only rows whose workflow the system database no longer holds: a checkpoint of
a workflow that can still be recovered or rewound is what keeps its ledger write
exactly-once. Those rows are read and deleted with SQL: the table is the datasource's own
tracking table, not part of DBOS's API.
"""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from dbos import DBOS, WorkflowStatusString
from sqlalchemy import Connection, Engine, bindparam, create_engine, text
from sqlalchemy.pool import NullPool

from curate_d.config import Settings
from curate_d.observability.logging import get_logger
from curate_d.runtime.datasource import TRACKING_SCHEMA
from curate_d.runtime.dbos_app import system_database_url

logger = get_logger(__name__)

MILLISECONDS_PER_SECOND = 1000

# Workflow IDs one pass examines, for both the workflow records and the checkpoints; each
# pass deletes in its own transaction. Not a setting: it bounds how many IDs one query binds
# and how long one delete holds its locks, not what is pruned. That is the retention
# period's job, which is why the period is configuration. Revisit only when a measured prune
# contends with live ledger transactions.
CHECKPOINT_BATCH_SIZE = 1000

# The statuses DBOS stamps `completed_at` on; the only ones a prune may delete.
TERMINAL_STATUSES: Final = (
    WorkflowStatusString.SUCCESS.value,
    WorkflowStatusString.ERROR.value,
    WorkflowStatusString.CANCELLED.value,
    WorkflowStatusString.MAX_RECOVERY_ATTEMPTS_EXCEEDED.value,
)


@dataclass(frozen=True)
class RetentionWindow:
    """The boundary one prune will apply, kept separate from applying it."""

    retention_days: int
    cutoff: datetime

    @property
    def cutoff_epoch_ms(self) -> int:
        """Return the cutoff in milliseconds since the epoch, the unit of checkpoint timestamps."""
        return int(self.cutoff.timestamp() * MILLISECONDS_PER_SECOND)


def retention_window(settings: Settings, *, now: datetime | None = None) -> RetentionWindow:
    """Resolve the prune boundary from configuration.

    Args:
        settings: Source of the retention period. Read here rather than defaulted in the
            call below, so that changing the period is a configuration change and a
            misconfigured period fails at settings validation instead of silently
            pruning the wrong amount.
        now: Present instant, injectable so the boundary arithmetic is testable without
            waiting 30 days for a fixture to age.

    Raises:
        ValueError: If the resolved period is not positive. Settings validation already
            rejects that, so reaching this means the window was built by hand.
    """
    if settings.workflow_retention_days < 1:
        raise ValueError(
            f"workflow retention of {settings.workflow_retention_days} days would prune "
            f"records the moment they complete; the period must be at least one day"
        )
    moment = now or datetime.now(UTC)
    return RetentionWindow(
        retention_days=settings.workflow_retention_days,
        cutoff=moment - timedelta(days=settings.workflow_retention_days),
    )


async def prune_workflow_history(
    settings: Settings, *, now: datetime | None = None
) -> RetentionWindow:
    """Delete workflow records that completed longer ago than the configured period.

    Then delete the ledger datasource's checkpoints of the workflows that are gone. Audit
    entries are untouched: both deletions stay inside DBOS's own tables, which the audit
    trail is not part of. Needs DBOS launched in this process.

    Returns:
        The window that was applied, so a caller can log or assert on the boundary
        actually used rather than recomputing it and hoping the two agree.
    """
    window = retention_window(settings, now=now)
    # Synchronous, and it deletes in committed batches; a thread keeps a long prune from
    # stalling the event loop that the worker's I/O queues are running on.
    workflows = await asyncio.to_thread(prune_completed_workflows, window.cutoff)
    checkpoints = await asyncio.to_thread(
        prune_datasource_checkpoints, settings, window.cutoff_epoch_ms
    )
    logger.info(
        "workflow_history.pruned",
        retention_days=window.retention_days,
        cutoff=window.cutoff.isoformat(),
        workflows=workflows,
        datasource_checkpoints=checkpoints,
    )
    return window


def prune_completed_workflows(cutoff: datetime, *, batch_size: int = CHECKPOINT_BATCH_SIZE) -> int:
    """Delete terminal workflow records that completed at or before `cutoff`, in batches.

    Synchronous: DBOS refuses its synchronous calls on a thread running an event loop.

    Returns:
        How many workflow records were deleted.
    """
    deleted = 0
    while True:
        batch = DBOS.list_workflows(
            status=list(TERMINAL_STATUSES),
            completed_before=cutoff.isoformat(),
            load_input=False,
            load_output=False,
            limit=batch_size,
        )
        if not batch:
            return deleted
        DBOS.delete_workflows([status.workflow_id for status in batch])
        deleted += len(batch)


def prune_datasource_checkpoints(
    settings: Settings, cutoff_epoch_ms: int, *, batch_size: int = CHECKPOINT_BATCH_SIZE
) -> int:
    """Delete datasource checkpoints older than the cutoff whose workflow no longer exists.

    Run after `prune_completed_workflows`, so the workflows it just removed are the ones
    found missing. Only rows older than the cutoff go: a deterministic workflow ID enqueued
    again after its first run was pruned writes fresh checkpoints, and those must survive.
    Synchronous, like `prune_completed_workflows`: call it off the event loop.

    Returns:
        How many checkpoint rows were deleted; zero when no datasource has ever created
        the tracking table.
    """
    # The tracking table lives in the ledger database, which also holds DBOS's (dbos_app.py).
    ledger = create_engine(system_database_url(settings), poolclass=NullPool)
    try:
        return _prune_orphaned_checkpoints(ledger, cutoff_epoch_ms, batch_size)
    finally:
        ledger.dispose()


def _live_workflow_ids(candidates: Sequence[str]) -> set[str]:
    """Return which of `candidates` the system database still holds, in any application."""
    statuses = DBOS.list_workflows(
        workflow_ids=list(candidates), load_input=False, load_output=False
    )
    return {status.workflow_id for status in statuses}


def _prune_orphaned_checkpoints(ledger: Engine, cutoff_epoch_ms: int, batch_size: int) -> int:
    """Walk aged checkpoints by workflow ID, deleting those whose workflow row is gone."""
    with ledger.connect() as connection:
        if not _tracking_table_exists(connection):
            return 0
    candidates_sql = text(
        f"SELECT DISTINCT workflow_id FROM {TRACKING_SCHEMA}.datasource_outputs"
        " WHERE created_at < :cutoff AND workflow_id > :after"
        " ORDER BY workflow_id LIMIT :limit"
    )
    delete_sql = text(
        f"DELETE FROM {TRACKING_SCHEMA}.datasource_outputs"
        " WHERE workflow_id IN :ids AND created_at < :cutoff"
    ).bindparams(bindparam("ids", expanding=True))

    deleted = 0
    after = ""
    while True:
        with ledger.connect() as connection:
            candidates = list(
                connection.scalars(
                    candidates_sql,
                    {"cutoff": cutoff_epoch_ms, "after": after, "limit": batch_size},
                )
            )
        if not candidates:
            return deleted
        live = _live_workflow_ids(candidates)
        orphaned = [workflow_id for workflow_id in candidates if workflow_id not in live]
        if orphaned:
            with ledger.begin() as connection:
                result = connection.execute(
                    delete_sql, {"ids": orphaned, "cutoff": cutoff_epoch_ms}
                )
                deleted += result.rowcount
        after = candidates[-1]


def _tracking_table_exists(connection: Connection) -> bool:
    """Return whether a datasource has created its tracking table in this database yet."""
    found = connection.scalar(
        text("SELECT to_regclass(:name)"), {"name": f"{TRACKING_SCHEMA}.datasource_outputs"}
    )
    return found is not None
