"""Recover retired executors and versions without creating competing workflow lineages.

Pending work owned by a retired executor can be re-enqueued in place. Pending or
queued work pinned to a retired version needs a fork, retaining its queue and provider
partition. Delayed work is left to DBOS's scheduler; once due it becomes ENQUEUED and
is eligible for the next sweep. Forking a DELAYED row would drop its delay.

Each original gets one deterministic successor ID, independent of the target version.
The DBOS primary key arbitrates simultaneous sweeps, and its fork transaction copies
checkpoints and sets ``was_forked_from`` atomically. Only after confirming that successor
exists do we cancel the original. A retry repairs a crash between those operations,
even after another deployment or rollback. Later deployments recover the surviving leaf
instead of creating another sibling from an ancestor.

Liveness is the executor lease (lease.py), not a list supplied by the caller: an
executor is dead only if the sweep can take its lease, and the sweep holds that lease
while re-enqueueing the executor's rows, so the executor cannot start and run startup
recovery on them mid-sweep. This relies on every executor holding its lease, which
workers do from before launch; a process that runs DBOS without one is indistinguishable
from a dead executor.

Sweeps require a version whose predecessors are retired. They must not be run during
mixed-version deployments: cancelling an original does not forcibly stop a step still
executing in another process, and a predecessor that predates the lease holds none.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import sha256
from sqlite3 import IntegrityError as SQLiteIntegrityError

from dbos import DBOS, SetWorkflowID
from dbos._dbos import _get_dbos_instance
from dbos._utils import INTERNAL_QUEUE_NAME
from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError

from curate_d.config import Settings
from curate_d.observability.logging import get_logger
from curate_d.runtime.dbos_app import APPLICATION_VERSION
from curate_d.runtime.lease import ExecutorLease

logger = get_logger(__name__)

PENDING = "PENDING"
ENQUEUED = "ENQUEUED"


class RecoveryAction(StrEnum):
    """What a stranded workflow needs before any live worker will run it."""

    RETIRE = "retire"
    """Finish cancelling an ancestor after its successor was committed."""

    REENQUEUE = "reenqueue"
    """Return the row to a queue under its existing identifier, if its owner is dead."""

    FORK = "fork"
    """Copy it forward onto the current version, under a new identifier."""


@dataclass(frozen=True)
class PendingWorkflow:
    """Active workflow metadata used to decide whether any live worker can claim it."""

    workflow_id: str
    executor_id: str | None
    application_version: str | None
    status: str = PENDING
    queue_name: str | None = None
    queue_partition_key: str | None = None
    was_forked_from: bool = False


@dataclass(frozen=True)
class StrandedWorkflow:
    """A pending workflow no live worker will claim, with the remedy it needs and why."""

    workflow: PendingWorkflow
    action: RecoveryAction
    reason: str


@dataclass(frozen=True)
class SweepOutcome:
    """What one sweep actually did, in terms an operator can reconcile against the views."""

    reenqueued: tuple[str, ...] = ()
    forked: tuple[tuple[str, str], ...] = ()
    """Pairs of (original identifier, forked identifier)."""
    retired: tuple[str, ...] = ()
    """Ancestors whose previously committed successor was confirmed before cancellation."""
    left_running: int = 0
    """Pending workflows left alone because their executor's lease is held."""
    failed: tuple[tuple[str, str], ...] = ()
    """Pairs of (workflow identifier, failure reason)."""

    @property
    def recovered_count(self) -> int:
        return len(self.reenqueued) + len(self.forked)


def plan_recovery(
    pending: Sequence[PendingWorkflow],
    *,
    current_version: str,
) -> tuple[StrandedWorkflow, ...]:
    """Select the recovery each workflow needs, deferring executor liveness to the sweep.

    Version decides a fork and lineage decides a retirement, both independent of which
    executors are alive. Every other PENDING row becomes a re-enqueue candidate, which
    the sweep carries out only after taking its executor's lease.

    Args:
        pending: Active workflows, as `list_pending_workflows` reports them.
        current_version: The version live workers run.

    Returns:
        One entry per workflow that needs a remedy, with the reason it was selected.
    """
    plan: list[StrandedWorkflow] = []

    for workflow in pending:
        if workflow.status not in {PENDING, ENQUEUED}:
            continue
        if workflow.was_forked_from:
            plan.append(
                StrandedWorkflow(
                    workflow=workflow,
                    action=RecoveryAction.RETIRE,
                    reason="a successor was already committed; finish retiring the original",
                )
            )
            continue
        # A version-less row is dequeueable by whichever worker runs the latest version,
        # so it is never stranded on version; only its identity can strand it.
        if (
            workflow.application_version is not None
            and workflow.application_version != current_version
        ):
            plan.append(
                StrandedWorkflow(
                    workflow=workflow,
                    action=RecoveryAction.FORK,
                    reason=(
                        f"pinned to application version {workflow.application_version!r}, "
                        f"which no live worker runs (current is {current_version!r}); "
                        f"re-enqueueing would leave it undequeueable"
                    ),
                )
            )
            continue

        if workflow.status != PENDING:
            continue

        plan.append(
            StrandedWorkflow(
                workflow=workflow,
                action=RecoveryAction.REENQUEUE,
                reason=(
                    f"owned by executor {workflow.executor_id!r}; re-enqueued only if no "
                    f"live process holds its lease, since startup recovery scans only a "
                    f"process's own identity"
                ),
            )
        )

    return tuple(plan)


async def list_pending_workflows() -> tuple[PendingWorkflow, ...]:
    """List pending and queued workflows, with routing and durable fork lineage metadata."""
    statuses = await DBOS.list_workflows_async(
        status=[PENDING, ENQUEUED], load_input=False, load_output=False
    )
    return tuple(
        PendingWorkflow(
            workflow_id=status.workflow_id,
            executor_id=status.executor_id,
            application_version=status.app_version,
            status=status.status,
            queue_name=status.queue_name,
            queue_partition_key=status.queue_partition_key,
            was_forked_from=status.was_forked_from,
        )
        for status in statuses
    )


async def _fork_start_step(workflow_id: str) -> int:
    """Return the step a fork should resume from: the one after the last output on record.

    Fork copies the outputs of every step below this number, so choosing the last
    recorded step plus one preserves committed work instead of repeating it.
    """
    steps = await DBOS.list_workflow_steps_async(workflow_id, load_output=False)
    return max((step["function_id"] for step in steps), default=0) + 1


def recovery_successor_id(workflow_id: str) -> str:
    """Return one successor ID per original, including when sweep versions disagree."""
    return f"recovery:{sha256(workflow_id.encode()).hexdigest()}"


def _is_workflow_id_conflict(error: IntegrityError) -> bool:
    cause = error.orig
    if isinstance(cause, UniqueViolation):
        return cause.diag.constraint_name == "workflow_status_pkey"
    return isinstance(cause, SQLiteIntegrityError) and str(cause) == (
        "UNIQUE constraint failed: workflow_status.workflow_uuid"
    )


async def _confirmed_successor(original: str, successor: str) -> bool:
    status = await DBOS.get_workflow_status_async(successor)
    if status is None:
        return False
    if status.forked_from != original:
        raise ValueError(f"recovery ID {successor!r} belongs to an unrelated workflow")
    return True


async def _recover_version(workflow: PendingWorkflow, version: str) -> tuple[str, bool]:
    """Commit or reuse one successor, then retire its source; never cancel on fork failure.

    The return flag distinguishes a new fork from repairing a previous transfer.
    Database uniqueness is the cross-process lock: only a committed matching successor
    makes an insertion conflict recoverable. Other database errors still surface.
    """
    original = workflow.workflow_id
    successor = recovery_successor_id(original)
    created = False
    if not await _confirmed_successor(original, successor):
        # Re-read after planning: another sweep may have retired this row, or an operator
        # may have completed/cancelled it. Never fork a terminal snapshot back to life.
        status = await DBOS.get_workflow_status_async(original)
        if status is None or status.status not in {PENDING, ENQUEUED}:
            if not await _confirmed_successor(original, successor):
                raise ValueError(f"workflow {original!r} is no longer eligible for recovery")
        elif status.was_forked_from:
            if not await _confirmed_successor(original, successor):
                raise ValueError(
                    f"workflow {original!r} already has a fork but no recovery successor; "
                    "inspect its existing lineage before recovering it"
                )
        else:
            try:
                with SetWorkflowID(successor):
                    await DBOS.fork_workflow_async(
                        original,
                        await _fork_start_step(original),
                        application_version=version,
                        queue_name=status.queue_name,
                        queue_partition_key=status.queue_partition_key,
                    )
                created = True
            except IntegrityError as error:
                # Only the workflow primary-key conflict means another sweep won.
                # Other constraints must remain failures, even if a successor exists.
                if not _is_workflow_id_conflict(error):
                    raise
                if not await _confirmed_successor(original, successor):
                    raise
            if not await _confirmed_successor(original, successor):
                raise ValueError(f"fork of {original!r} did not publish its expected successor")
    await DBOS.cancel_workflow_async(original)
    return successor, created


type LeaseFactory = Callable[[str], ExecutorLease]


async def _reenqueue(workflow: PendingWorkflow, executor_id: str) -> bool:
    """Return one PENDING row to the internal queue if `executor_id` still owns it."""
    return await asyncio.to_thread(
        _get_dbos_instance()._sys_db.reenqueue_for_recovery,
        workflow.workflow_id,
        [executor_id],
        INTERNAL_QUEUE_NAME,
    )


async def run_recovery_sweep(
    settings: Settings,
    *,
    current_version: str | None = None,
    lease_factory: LeaseFactory | None = None,
) -> SweepOutcome:
    """Recover pending and queued work that no live worker will claim.

    Args:
        settings: Resolved configuration; names the database holding executor leases.
        current_version: The version live workers run. Defaults to this build's.
        lease_factory: Builds the lease for one executor identity. Defaults to a lease on
            the configured DBOS system database.

    Returns:
        What the sweep did, including per-workflow failures. One workflow that cannot be
        recovered does not abort the rest; a sweep that gave up on the first bad row
        would leave the remaining orphans stranded for another interval.
    """
    version = current_version or APPLICATION_VERSION
    leases = lease_factory or (
        lambda executor_id: ExecutorLease.for_settings(
            settings, executor_id, purpose="recovery sweep"
        )
    )
    pending = await list_pending_workflows()
    plan = plan_recovery(pending, current_version=version)

    reenqueued: list[str] = []
    forked: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    retired: list[str] = []
    left_running = 0

    # Target only planned rows. Bulk executor recovery would also re-enqueue an
    # ancestor awaiting retirement if an operator rolled back to its old version.
    by_executor: dict[str | None, list[PendingWorkflow]] = {}
    for entry in plan:
        if entry.action is RecoveryAction.REENQUEUE:
            by_executor.setdefault(entry.workflow.executor_id, []).append(entry.workflow)

    for executor_id, workflows in by_executor.items():
        if executor_id is None:
            for workflow in workflows:
                failed.append(
                    (
                        workflow.workflow_id,
                        "re-enqueueing failed: DBOS cannot conditionally re-enqueue a row "
                        "without an executor",
                    )
                )
            continue
        lease = leases(executor_id)
        try:
            try:
                claimed = await asyncio.to_thread(lease.try_acquire)
            except Exception as error:  # Unknown liveness is never treated as dead.
                failed.extend(
                    (workflow.workflow_id, f"checking executor {executor_id!r} failed: {error}")
                    for workflow in workflows
                )
                logger.error(
                    "recovery_sweep.liveness_check_failed",
                    executor=executor_id,
                    error=str(error),
                )
                continue
            if not claimed:
                left_running += len(workflows)
                logger.info(
                    "recovery_sweep.executor_live", executor=executor_id, pending=len(workflows)
                )
                continue
            # The lease stays held until every row is re-enqueued, so the executor cannot
            # start and run its own startup recovery on them in between.
            for workflow in workflows:
                try:
                    changed = await _reenqueue(workflow, executor_id)
                    if changed:
                        reenqueued.append(workflow.workflow_id)
                    logger.info(
                        "recovery_sweep.reenqueued",
                        workflow_id=workflow.workflow_id,
                        executor=executor_id,
                        changed=changed,
                    )
                except Exception as error:  # Reported per workflow, never swallowed.
                    failed.append((workflow.workflow_id, f"re-enqueueing failed: {error}"))
                    logger.error(
                        "recovery_sweep.reenqueue_failed",
                        workflow_id=workflow.workflow_id,
                        error=str(error),
                    )
        finally:
            await asyncio.to_thread(lease.release)

    for entry in plan:
        if entry.action not in {RecoveryAction.FORK, RecoveryAction.RETIRE}:
            continue
        original = entry.workflow.workflow_id
        try:
            successor, created = await _recover_version(entry.workflow, version)
            if created:
                forked.append((original, successor))
            else:
                retired.append(original)
            logger.info(
                "recovery_sweep.forked" if created else "recovery_sweep.retired",
                workflow_id=original,
                forked_workflow_id=successor,
                from_version=entry.workflow.application_version,
                to_version=version,
            )
        except Exception as error:  # One bad row must not strand the rest.
            reason = (
                f"recovering workflow {original!r} from version "
                f"{entry.workflow.application_version!r} onto {version!r} failed: {error}"
            )
            failed.append((original, reason))
            logger.error("recovery_sweep.fork_failed", workflow_id=original, error=str(error))

    return SweepOutcome(
        reenqueued=tuple(reenqueued),
        forked=tuple(forked),
        retired=tuple(retired),
        left_running=left_running,
        failed=tuple(failed),
    )
