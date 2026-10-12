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
until it finishes, so the executor cannot start and run startup recovery on its rows
mid-sweep. This relies on every executor holding its lease, which workers do from before
launch; a process that runs DBOS without one is indistinguishable from a dead executor.

A version's work is forked only while no worker runs that version: the sweep takes the
version lease exclusively (refused while any worker of it holds it shared) and holds it
across that version's forks. A build that predates the version lease holds none, so a
version also counts as running while a live executor holding no version lease still owns
PENDING work on it. A live executor that holds a version lease runs that version, so its
PENDING rows on an older one (left by its own restart onto a new build) do not block the
fork. A running version's rows are skipped and reported, never forked: cancelling an
original does not stop a step still executing in another process.

A sweep scoped to one executor recovers only that executor's rows, and is refused
outright while the executor holds its lease: a live executor recovers its own work.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256

from dbos import DBOS, SetWorkflowID
from dbos._dbos import _get_dbos_instance
from dbos._utils import INTERNAL_QUEUE_NAME
from sqlalchemy.exc import IntegrityError

from curate_d.config import Settings
from curate_d.observability.logging import get_logger
from curate_d.runtime import dbos_app
from curate_d.runtime.lease import ExecutorLease, VersionLease
from curate_d.runtime.lineage import confirmed_successor, is_workflow_id_conflict

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
    skipped: tuple[tuple[str, str], ...] = ()
    """Pairs of (workflow identifier, reason) left for a later sweep: their version runs."""
    running_versions: tuple[str, ...] = ()
    """Versions whose work was not forked because a worker still runs them."""
    refused: str | None = None
    """Why a scoped sweep did nothing at all; None when it ran."""

    @property
    def recovered_count(self) -> int:
        """Return how many workflows were re-enqueued or forked."""
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


async def _confirmed_successor(original: str, successor: str) -> bool:
    """Return whether `successor` exists as a fork of `original`; raise if it is unrelated."""
    status = await DBOS.get_workflow_status_async(successor)
    return confirmed_successor(status, original, successor)


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
                if not is_workflow_id_conflict(error):
                    raise
                if not await _confirmed_successor(original, successor):
                    raise
            if not await _confirmed_successor(original, successor):
                raise ValueError(f"fork of {original!r} did not publish its expected successor")
    await DBOS.cancel_workflow_async(original)
    return successor, created


type LeaseFactory = Callable[[str], ExecutorLease]
type VersionLeaseFactory = Callable[[str], VersionLease]


async def _reenqueue(workflow: PendingWorkflow, executor_id: str) -> bool:
    """Return one PENDING row to the internal queue if `executor_id` still owns it."""
    return await asyncio.to_thread(
        _get_dbos_instance()._sys_db.reenqueue_for_recovery,
        workflow.workflow_id,
        [executor_id],
        INTERNAL_QUEUE_NAME,
    )


class _Claims:
    """The executor leases one sweep has tried, held or refused, until it finishes.

    Each identity is tried once: a dead executor's lease, once taken, stays held until
    `release_all`, so it cannot start and recover rows the sweep is working on; a live
    one's answer stands for the rest of the sweep.
    """

    def __init__(self, factory: LeaseFactory) -> None:
        """Bind the factory building one executor's lease."""
        self._factory = factory
        self._leases: dict[str, ExecutorLease] = {}
        self._dead: dict[str, bool] = {}

    async def is_dead(self, executor_id: str) -> bool:
        """Return whether the sweep holds `executor_id`'s lease, taking it if it is free.

        Raises:
            Exception: liveness could not be checked; it is never assumed dead.
        """
        if executor_id in self._dead:
            return self._dead[executor_id]
        lease = self._leases.get(executor_id)
        if lease is None:
            lease = self._leases[executor_id] = self._factory(executor_id)
        dead = await asyncio.to_thread(lease.try_acquire)
        self._dead[executor_id] = dead
        return dead

    async def has_version_lease(self, executor_id: str) -> bool:
        """Return whether live `executor_id`'s lease session also holds a version lease."""
        return await asyncio.to_thread(self._leases[executor_id].holder_has_version_lease)

    async def release_all(self) -> None:
        """Release every lease taken and close every session opened."""
        for lease in self._leases.values():
            await asyncio.to_thread(lease.release)


@dataclass
class _Results:
    """What a sweep did so far."""

    reenqueued: list[str] = field(default_factory=list)
    forked: list[tuple[str, str]] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[tuple[str, str]] = field(default_factory=list)
    running_versions: list[str] = field(default_factory=list)
    left_running: int = 0

    def outcome(self) -> SweepOutcome:
        """Return the results as a `SweepOutcome`."""
        return SweepOutcome(
            reenqueued=tuple(self.reenqueued),
            forked=tuple(self.forked),
            retired=tuple(self.retired),
            left_running=self.left_running,
            failed=tuple(self.failed),
            skipped=tuple(self.skipped),
            running_versions=tuple(self.running_versions),
        )


async def _reenqueue_orphans(
    plan: Sequence[StrandedWorkflow], claims: _Claims, results: _Results
) -> None:
    """Re-enqueue every planned PENDING row whose executor's lease the sweep can hold."""
    # Target only planned rows. Bulk executor recovery would also re-enqueue an
    # ancestor awaiting retirement if an operator rolled back to its old version.
    by_executor: dict[str | None, list[PendingWorkflow]] = {}
    for entry in plan:
        if entry.action is RecoveryAction.REENQUEUE:
            by_executor.setdefault(entry.workflow.executor_id, []).append(entry.workflow)

    for executor_id, workflows in by_executor.items():
        if executor_id is None:
            results.failed.extend(
                (
                    workflow.workflow_id,
                    "re-enqueueing failed: DBOS cannot conditionally re-enqueue a row "
                    "without an executor",
                )
                for workflow in workflows
            )
            continue
        try:
            dead = await claims.is_dead(executor_id)
        except Exception as error:  # Unknown liveness is never treated as dead.
            results.failed.extend(
                (workflow.workflow_id, f"checking executor {executor_id!r} failed: {error}")
                for workflow in workflows
            )
            logger.error(
                "recovery_sweep.liveness_check_failed", executor=executor_id, error=str(error)
            )
            continue
        if not dead:
            results.left_running += len(workflows)
            logger.info(
                "recovery_sweep.executor_live", executor=executor_id, pending=len(workflows)
            )
            continue
        for workflow in workflows:
            try:
                changed = await _reenqueue(workflow, executor_id)
                if changed:
                    results.reenqueued.append(workflow.workflow_id)
                logger.info(
                    "recovery_sweep.reenqueued",
                    workflow_id=workflow.workflow_id,
                    executor=executor_id,
                    changed=changed,
                )
            except Exception as error:  # Reported per workflow, never swallowed.
                results.failed.append((workflow.workflow_id, f"re-enqueueing failed: {error}"))
                logger.error(
                    "recovery_sweep.reenqueue_failed",
                    workflow_id=workflow.workflow_id,
                    error=str(error),
                )


async def _running_reason(
    version: str,
    pending: Sequence[PendingWorkflow],
    claims: _Claims,
    lease: VersionLease,
) -> str | None:
    """Return why `version` counts as running, or None with its version lease now held.

    A live executor holding no version lease predates it, so its PENDING rows on
    `version` may be executing there; one holding a version lease runs that version
    instead, which the version lease itself answers for.
    """
    owners = sorted(
        {
            workflow.executor_id
            for workflow in pending
            if workflow.application_version == version
            and workflow.status == PENDING
            and workflow.executor_id is not None
        }
    )
    for owner in owners:
        try:
            if await claims.is_dead(owner) or await claims.has_version_lease(owner):
                continue
        except Exception as error:  # Unknown liveness is never treated as dead.
            return (
                f"liveness of executor {owner!r}, which owns PENDING work on it, is "
                f"unknown: {error}"
            )
        return (
            f"executor {owner!r} is alive without a version lease and still owns PENDING work on it"
        )
    try:
        taken = await asyncio.to_thread(lease.try_acquire)
    except Exception as error:  # An unchecked version is treated as running.
        return f"its version lease could not be checked: {error}"
    return None if taken else "its version lease is held: a worker runs it, or a recovery forks it"


async def _recover_versions(
    plan: Sequence[StrandedWorkflow],
    pending: Sequence[PendingWorkflow],
    version: str,
    claims: _Claims,
    version_leases: VersionLeaseFactory,
    results: _Results,
) -> None:
    """Fork or retire planned rows, one source version at a time, under its version lease."""
    by_version: dict[str | None, list[PendingWorkflow]] = {}
    for entry in plan:
        if entry.action in {RecoveryAction.FORK, RecoveryAction.RETIRE}:
            by_version.setdefault(entry.workflow.application_version, []).append(entry.workflow)

    for source_version, workflows in by_version.items():
        # Retiring an ancestor on the current version, or on none, cancels a row whose
        # successor runs already; there is no other version to wait out.
        lease = None
        if source_version is not None and source_version != version:
            lease = version_leases(source_version)
            reason = await _running_reason(source_version, pending, claims, lease)
            if reason is not None:
                results.running_versions.append(source_version)
                results.skipped.extend(
                    (workflow.workflow_id, f"version {source_version!r} is running: {reason}")
                    for workflow in workflows
                )
                logger.warning(
                    "recovery_sweep.version_running",
                    version=source_version,
                    reason=reason,
                    skipped=len(workflows),
                )
                continue
        try:
            for workflow in workflows:
                await _recover_one(workflow, version, results)
        finally:
            if lease is not None:
                await asyncio.to_thread(lease.release)


async def _recover_one(workflow: PendingWorkflow, version: str, results: _Results) -> None:
    """Fork or retire one row, recording a failure rather than raising it."""
    original = workflow.workflow_id
    try:
        successor, created = await _recover_version(workflow, version)
        if created:
            results.forked.append((original, successor))
        else:
            results.retired.append(original)
        logger.info(
            "recovery_sweep.forked" if created else "recovery_sweep.retired",
            workflow_id=original,
            forked_workflow_id=successor,
            from_version=workflow.application_version,
            to_version=version,
        )
    except Exception as error:  # One bad row must not strand the rest.
        reason = (
            f"recovering workflow {original!r} from version "
            f"{workflow.application_version!r} onto {version!r} failed: {error}"
        )
        results.failed.append((original, reason))
        logger.error("recovery_sweep.fork_failed", workflow_id=original, error=str(error))


async def run_recovery_sweep(
    settings: Settings,
    *,
    executor_id: str | None = None,
    current_version: str | None = None,
    lease_factory: LeaseFactory | None = None,
    version_lease_factory: VersionLeaseFactory | None = None,
) -> SweepOutcome:
    """Recover pending and queued work that no live worker will claim.

    Args:
        settings: Resolved configuration; names the database holding executor leases.
        executor_id: Recover only this executor's rows, and nothing while it holds its
            lease. Every executor's when None.
        current_version: The version live workers run. Defaults to this build's.
        lease_factory: Builds the lease for one executor identity. Defaults to a lease on
            the configured DBOS system database.
        version_lease_factory: Builds the lease for one application version. Defaults to
            a lease on the configured DBOS system database.

    Returns:
        What the sweep did, including per-workflow failures and skips. One workflow that
        cannot be recovered does not abort the rest; a sweep that gave up on the first bad
        row would leave the remaining orphans stranded for another interval.
    """
    version = current_version or dbos_app.APPLICATION_VERSION
    claims = _Claims(
        lease_factory
        or (
            lambda executor: ExecutorLease.for_settings(
                settings, executor, purpose="recovery sweep"
            )
        )
    )
    version_leases = version_lease_factory or (
        lambda source: VersionLease.for_settings(settings, source)
    )
    results = _Results()
    try:
        pending = await list_pending_workflows()
        scope: Sequence[PendingWorkflow] = pending
        if executor_id is not None:
            refusal = await _refusal(executor_id, claims)
            if refusal is not None:
                logger.warning("recovery_sweep.refused", executor=executor_id, reason=refusal)
                return SweepOutcome(refused=refusal)
            scope = tuple(workflow for workflow in pending if workflow.executor_id == executor_id)
        plan = plan_recovery(scope, current_version=version)
        await _reenqueue_orphans(plan, claims, results)
        await _recover_versions(plan, pending, version, claims, version_leases, results)
    finally:
        await claims.release_all()
    return results.outcome()


async def _refusal(executor_id: str, claims: _Claims) -> str | None:
    """Return why a sweep scoped to `executor_id` must not run, or None with its lease held."""
    try:
        if await claims.is_dead(executor_id):
            return None
    except Exception as error:  # Unknown liveness is never treated as dead.
        return f"liveness of executor {executor_id} could not be checked: {error}"
    return f"executor {executor_id} is alive (lease held); it recovers its own work"
