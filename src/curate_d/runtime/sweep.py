"""Recover retired executors and versions without creating competing workflow lineages.

Pending work owned by a retired executor can be re-enqueued in place, on its own queue.
Pending or queued work pinned to a retired version needs a fork, retaining its queue and
provider partition. Delayed work is left to DBOS's scheduler; once due it becomes
ENQUEUED and is eligible for the next sweep. Forking a DELAYED row would drop its delay.

Each original gets one deterministic successor ID, independent of the target version.
The DBOS primary key arbitrates simultaneous sweeps, and its fork transaction copies
checkpoints and sets ``was_forked_from`` atomically. Only after confirming that successor
exists do we cancel the original. A retry repairs a crash between those operations,
even after another deployment or rollback. Later deployments recover the surviving leaf
instead of creating another sibling from an ancestor.

Executor liveness is the executor lease (lease.py): an executor is dead only if the sweep
can take its lease, and the sweep holds that lease while re-enqueueing the executor's
rows, so the executor cannot start and run startup recovery on them mid-sweep.

Version liveness is the version lease (version_lease.py). The target is DBOS's latest
version, read when the sweep starts; only a sweeper running that version forks or
retires. A non-target version's rows are forked or retired only if that version
registered before the target, and only while the sweep holds its lock exclusively, which
no live worker of it allows. A build that predates the lease never registered and is left
alone. After taking a version's lock the sweep reads the latest version again, and stops
forking if it changed.

A stale tick of the sweep's own schedule is cancelled under the same gates rather than
forked or re-enqueued: the next tick does its work. A row whose queue is NULL or DBOS's
internal queue is never forked or re-enqueued, since every worker dequeues the internal
queue and the row could run on the wrong profile; retiring one still cancels it.

DBOS calls made through `RecoveryControl` are not checkpointed when the sweep runs in a
step: a re-run repeats the whole sweep, which is safe because every fork is keyed by the
deterministic successor ID and every cancel and re-enqueue is gated on a re-read status or
on executor ownership.
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
from sqlite3 import IntegrityError as SQLiteIntegrityError
from typing import Final, Protocol

from dbos import DBOS, DBOSClient, SetWorkflowID, WorkflowStatus
from dbos._dbos import _get_dbos_instance
from dbos._utils import INTERNAL_QUEUE_NAME
from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError

from curate_d.config import Settings
from curate_d.observability.logging import get_logger
from curate_d.runtime import version_lease
from curate_d.runtime.lease import ExecutorLease
from curate_d.runtime.version_lease import VersionHold

logger = get_logger(__name__)

PENDING = "PENDING"
ENQUEUED = "ENQUEUED"

RECOVERY_SCHEDULE_NAME: Final = "recovery-sweep"
"""The scheduled sweep's own schedule; its stale ticks are retired, never recovered."""


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
    schedule_name: str | None = None

    @property
    def is_own_tick(self) -> bool:
        """Return whether this row is a tick of the recovery sweep's own schedule."""
        return self.schedule_name == RECOVERY_SCHEDULE_NAME


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
    skipped: tuple[str, ...] = ()
    """Fork and retire rows left because this sweeper is not on the target version."""
    unregistered: tuple[str, ...] = ()
    """Rows of a version that never registered in `lease_versions`."""
    newer_than_target: tuple[str, ...] = ()
    """Rows of a version registered after the target, such as a rolled-back build."""
    version_live: int = 0
    """Fork and retire rows left because some process holds their version's lock."""
    retired_ticks: tuple[str, ...] = ()
    """Stale ticks of the sweep's own schedule, cancelled rather than recovered."""

    @property
    def recovered_count(self) -> int:
        """Return how many rows were re-enqueued or forked."""
        return len(self.reenqueued) + len(self.forked)


def plan_recovery(
    pending: Sequence[PendingWorkflow],
    *,
    current_version: str,
) -> tuple[StrandedWorkflow, ...]:
    """Select the recovery each workflow needs, deferring liveness to the sweep.

    Version decides a fork and lineage decides a retirement, both independent of which
    executors and versions are alive. Every other PENDING row becomes a re-enqueue
    candidate, which the sweep carries out only after taking its executor's lease.

    Args:
        pending: Active workflows, as `RecoveryControl.list_pending` reports them.
        current_version: The target version, DBOS's latest.

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
                        f"which is not the target {current_version!r}; re-enqueueing would "
                        f"leave it undequeueable"
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


class RecoveryControl(Protocol):
    """The DBOS operations a sweep needs, from a launched instance or a client."""

    async def list_pending(self) -> tuple[PendingWorkflow, ...]:
        """List PENDING and ENQUEUED workflows with routing, lineage and schedule."""
        ...

    async def step_ids(self, workflow_id: str) -> list[int]:
        """Return the function IDs of the steps recorded for `workflow_id`."""
        ...

    async def status(self, workflow_id: str) -> WorkflowStatus | None:
        """Return `workflow_id`'s status, or None if it does not exist."""
        ...

    async def fork(
        self,
        workflow_id: str,
        successor_id: str,
        start_step: int,
        *,
        application_version: str,
        queue_name: str | None,
        queue_partition_key: str | None,
    ) -> None:
        """Fork `workflow_id` from `start_step` under `successor_id`."""
        ...

    async def cancel(self, workflow_id: str) -> None:
        """Cancel `workflow_id`."""
        ...

    async def latest_version(self) -> str:
        """Return DBOS's latest application version for this application."""
        ...

    async def reenqueue(self, workflow_id: str, executor_id: str) -> bool:
        """Re-enqueue one PENDING row on its own queue if `executor_id` still owns it."""
        ...


def _pending(status: WorkflowStatus) -> PendingWorkflow:
    """Map a DBOS status row to the fields the sweep plans with."""
    return PendingWorkflow(
        workflow_id=status.workflow_id,
        executor_id=status.executor_id,
        application_version=status.app_version,
        status=status.status,
        queue_name=status.queue_name,
        queue_partition_key=status.queue_partition_key,
        was_forked_from=status.was_forked_from,
        schedule_name=status.schedule_name,
    )


class DBOSRecoveryControl:
    """`RecoveryControl` over the launched DBOS instance, for the scheduled sweep."""

    async def list_pending(self) -> tuple[PendingWorkflow, ...]:
        """List PENDING and ENQUEUED workflows with routing, lineage and schedule."""
        statuses = await DBOS.list_workflows_async(
            status=[PENDING, ENQUEUED], load_input=False, load_output=False
        )
        return tuple(_pending(status) for status in statuses)

    async def step_ids(self, workflow_id: str) -> list[int]:
        """Return the function IDs of the steps recorded for `workflow_id`."""
        steps = await DBOS.list_workflow_steps_async(workflow_id, load_output=False)
        return [step["function_id"] for step in steps]

    async def status(self, workflow_id: str) -> WorkflowStatus | None:
        """Return `workflow_id`'s status, or None if it does not exist."""
        return await DBOS.get_workflow_status_async(workflow_id)

    async def fork(
        self,
        workflow_id: str,
        successor_id: str,
        start_step: int,
        *,
        application_version: str,
        queue_name: str | None,
        queue_partition_key: str | None,
    ) -> None:
        """Fork `workflow_id` from `start_step` under `successor_id`."""
        with SetWorkflowID(successor_id):
            await DBOS.fork_workflow_async(
                workflow_id,
                start_step,
                application_version=application_version,
                queue_name=queue_name,
                queue_partition_key=queue_partition_key,
            )

    async def cancel(self, workflow_id: str) -> None:
        """Cancel `workflow_id`."""
        await DBOS.cancel_workflow_async(workflow_id)

    async def latest_version(self) -> str:
        """Return DBOS's latest application version for this application."""
        return (await DBOS.get_latest_application_version_async())["version_name"]

    async def reenqueue(self, workflow_id: str, executor_id: str) -> bool:
        """Re-enqueue one PENDING row on its own queue if `executor_id` still owns it.

        DBOS keeps the row's queue (`coalesce(queue_name, <default>)`); the default is
        never used, because the sweep refuses rows without a named queue first.
        """
        return await asyncio.to_thread(
            _get_dbos_instance()._sys_db.reenqueue_for_recovery,
            workflow_id,
            [executor_id],
            INTERNAL_QUEUE_NAME,
        )


class ClientRecoveryControl:
    """`RecoveryControl` over a `DBOSClient`, for the operator sweep.

    `before_write` runs before every write, so a lost standalone version lease stops the
    sweep before its next fork, cancel or re-enqueue.
    """

    def __init__(self, client: DBOSClient, *, before_write: Callable[[], None]) -> None:
        """Bind the adapter to `client` and a guard called before each recovery write.

        `before_write` must raise to refuse a write; its return value is ignored and
        its exceptions propagate from fork, cancel and re-enqueue calls.
        """
        self._client = client
        self._before_write = before_write

    async def list_pending(self) -> tuple[PendingWorkflow, ...]:
        """List PENDING and ENQUEUED workflows with routing, lineage and schedule."""
        statuses = await self._client.list_workflows_async(
            status=[PENDING, ENQUEUED], load_input=False, load_output=False
        )
        return tuple(_pending(status) for status in statuses)

    async def step_ids(self, workflow_id: str) -> list[int]:
        """Return the function IDs of the steps recorded for `workflow_id`."""
        steps = await self._client.list_workflow_steps_async(workflow_id, load_output=False)
        return [step["function_id"] for step in steps]

    async def status(self, workflow_id: str) -> WorkflowStatus | None:
        """Return `workflow_id`'s status, or None if it does not exist."""
        statuses = await self._client.list_workflows_async(
            workflow_ids=[workflow_id], load_input=False, load_output=False
        )
        return statuses[0] if statuses else None

    async def fork(
        self,
        workflow_id: str,
        successor_id: str,
        start_step: int,
        *,
        application_version: str,
        queue_name: str | None,
        queue_partition_key: str | None,
    ) -> None:
        """Fork `workflow_id` from `start_step` under `successor_id`."""
        self._before_write()
        with SetWorkflowID(successor_id):
            await self._client.fork_workflow_async(
                workflow_id,
                start_step,
                application_version=application_version,
                queue_name=queue_name,
                queue_partition_key=queue_partition_key,
            )

    async def cancel(self, workflow_id: str) -> None:
        """Cancel `workflow_id`."""
        self._before_write()
        await self._client.cancel_workflow_async(workflow_id)

    async def latest_version(self) -> str:
        """Return DBOS's latest application version for this application."""
        return (await self._client.get_latest_application_version_async())["version_name"]

    async def reenqueue(self, workflow_id: str, executor_id: str) -> bool:
        """Re-enqueue one PENDING row on its own queue if `executor_id` still owns it.

        `DBOSClient` has no public re-enqueue; `_sys_db` is private and pinned in
        test_sweep_execution.py.
        """
        self._before_write()
        return await asyncio.to_thread(
            self._client._sys_db.reenqueue_for_recovery,
            workflow_id,
            [executor_id],
            INTERNAL_QUEUE_NAME,
        )


async def _fork_start_step(control: RecoveryControl, workflow_id: str) -> int:
    """Return the step a fork should resume from: the one after the last output on record.

    Fork copies the outputs of every step below this number, so choosing the last
    recorded step plus one preserves committed work instead of repeating it.
    """
    return max(await control.step_ids(workflow_id), default=0) + 1


def recovery_successor_id(workflow_id: str) -> str:
    """Return one successor ID per original, including when sweep versions disagree."""
    return f"recovery:{sha256(workflow_id.encode()).hexdigest()}"


def _is_workflow_id_conflict(error: IntegrityError) -> bool:
    """Return whether `error` is the workflow primary-key conflict a concurrent fork raises."""
    cause = error.orig
    if isinstance(cause, UniqueViolation):
        return cause.diag.constraint_name == "workflow_status_pkey"
    return isinstance(cause, SQLiteIntegrityError) and str(cause) == (
        "UNIQUE constraint failed: workflow_status.workflow_uuid"
    )


async def _confirmed_successor(control: RecoveryControl, original: str, successor: str) -> bool:
    """Return whether `successor` exists as `original`'s fork.

    Raises:
        ValueError: `successor` exists but was not forked from `original`.
    """
    status = await control.status(successor)
    if status is None:
        return False
    if status.forked_from != original:
        raise ValueError(f"recovery ID {successor!r} belongs to an unrelated workflow")
    return True


async def _recover_version(
    control: RecoveryControl, workflow: PendingWorkflow, version: str
) -> tuple[str, bool]:
    """Commit or reuse one successor, then retire its source; never cancel on fork failure.

    Database uniqueness is the cross-process lock: only a committed matching successor
    makes an insertion conflict recoverable. Other database errors still surface.
    Control errors also propagate, including cancellation failures after the successor
    has been committed.

    Returns:
        The successor ID and whether this call created it, rather than reusing a
        previously committed or concurrently created successor.

    Raises:
        ValueError: The successor belongs to another source, the source is no longer
            eligible without a matching successor, or the fork published no successor.
        IntegrityError: The fork violated a constraint other than the workflow primary
            key, or a primary-key conflict had no confirmed matching successor.
    """
    original = workflow.workflow_id
    successor = recovery_successor_id(original)
    created = False
    if not await _confirmed_successor(control, original, successor):
        # Re-read after planning: another sweep may have retired this row, or an operator
        # may have completed/cancelled it. Never fork a terminal snapshot back to life.
        status = await control.status(original)
        if status is None or status.status not in {PENDING, ENQUEUED}:
            if not await _confirmed_successor(control, original, successor):
                raise ValueError(f"workflow {original!r} is no longer eligible for recovery")
        elif status.was_forked_from:
            if not await _confirmed_successor(control, original, successor):
                raise ValueError(
                    f"workflow {original!r} already has a fork but no recovery successor; "
                    "inspect its existing lineage before recovering it"
                )
        else:
            try:
                await control.fork(
                    original,
                    successor,
                    await _fork_start_step(control, original),
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
                if not await _confirmed_successor(control, original, successor):
                    raise
            if not await _confirmed_successor(control, original, successor):
                raise ValueError(f"fork of {original!r} did not publish its expected successor")
    await control.cancel(original)
    return successor, created


async def _retire_tick(control: RecoveryControl, workflow: PendingWorkflow) -> bool:
    """Cancel a stale tick of the sweep's own schedule if it is still active.

    Returns:
        Whether it was still active and is now cancelled.
    """
    status = await control.status(workflow.workflow_id)
    if status is None or status.status not in {PENDING, ENQUEUED}:
        return False
    await control.cancel(workflow.workflow_id)
    return True


type LeaseFactory = Callable[[str], ExecutorLease]
type VersionHolds = Callable[[str], AbstractAsyncContextManager[VersionHold]]
"""Tries once to hold one version's lock exclusively for the duration of a block."""
type Registrations = Callable[[], Awaitable[Mapping[str, datetime]]]
"""Reads every registered version with its first registration time."""


def _queue_refusal(workflow: PendingWorkflow) -> str | None:
    """Return why a fork or re-enqueue of `workflow` would misroute it, if it would."""
    if workflow.queue_name is None:
        return "it names no queue"
    if workflow.queue_name == INTERNAL_QUEUE_NAME:
        return "it names DBOS's internal queue"
    return None


@dataclass
class _Tally:
    """What the sweep did so far, frozen into a `SweepOutcome` at the end."""

    reenqueued: list[str] = field(default_factory=list)
    forked: list[tuple[str, str]] = field(default_factory=list)
    retired: list[str] = field(default_factory=list)
    left_running: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    unregistered: list[str] = field(default_factory=list)
    newer_than_target: list[str] = field(default_factory=list)
    version_live: int = 0
    retired_ticks: list[str] = field(default_factory=list)

    def outcome(self) -> SweepOutcome:
        """Return the tally as an immutable outcome."""
        return SweepOutcome(
            reenqueued=tuple(self.reenqueued),
            forked=tuple(self.forked),
            retired=tuple(self.retired),
            left_running=self.left_running,
            failed=tuple(self.failed),
            skipped=tuple(self.skipped),
            unregistered=tuple(self.unregistered),
            newer_than_target=tuple(self.newer_than_target),
            version_live=self.version_live,
            retired_ticks=tuple(self.retired_ticks),
        )


class _Sweep:
    """One sweep's state: the target, its collaborators, and the running tally."""

    def __init__(
        self,
        *,
        control: RecoveryControl,
        target: str,
        leases: LeaseFactory,
        holds: VersionHolds,
    ) -> None:
        """Bind one sweep to its target and collaborators."""
        self.control = control
        self.target = target
        self.leases = leases
        self.holds = holds
        self.tally = _Tally()

    def refuse_misrouted(self, entries: list[StrandedWorkflow]) -> list[StrandedWorkflow]:
        """Fail fork and re-enqueue rows with no queue or DBOS's internal queue.

        Return the remaining entries, including retirements and this sweep's own ticks,
        which bypass the queue check.
        """
        kept: list[StrandedWorkflow] = []
        for entry in entries:
            workflow = entry.workflow
            refusal = (
                None
                if entry.action is RecoveryAction.RETIRE or workflow.is_own_tick
                else _queue_refusal(workflow)
            )
            if refusal is None:
                kept.append(entry)
                continue
            reason = f"{entry.action.value} of {workflow.workflow_id!r} refused: {refusal}"
            self.tally.failed.append((workflow.workflow_id, reason))
            logger.error(
                "recovery_sweep.queue_refused",
                workflow_id=workflow.workflow_id,
                action=entry.action.value,
                queue=workflow.queue_name,
            )
        return kept

    async def reenqueue_dead_executors(self, entries: list[StrandedWorkflow]) -> None:
        """Re-enqueue, or retire if a stale tick, each dead executor's PENDING rows."""
        by_executor: dict[str | None, list[PendingWorkflow]] = {}
        for entry in entries:
            by_executor.setdefault(entry.workflow.executor_id, []).append(entry.workflow)

        for executor_id, workflows in by_executor.items():
            if executor_id is None:
                self.tally.failed.extend(
                    (
                        workflow.workflow_id,
                        "re-enqueueing failed: DBOS cannot conditionally re-enqueue a row "
                        "without an executor",
                    )
                    for workflow in workflows
                )
                continue
            lease = self.leases(executor_id)
            try:
                try:
                    claimed = await asyncio.to_thread(lease.try_acquire)
                except Exception as error:  # Unknown liveness is never treated as dead.
                    self.tally.failed.extend(
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
                    self.tally.left_running += len(workflows)
                    logger.info(
                        "recovery_sweep.executor_live", executor=executor_id, pending=len(workflows)
                    )
                    continue
                # The lease stays held until every row is handled, so the executor cannot
                # start and run its own startup recovery on them in between.
                for workflow in workflows:
                    await self._reenqueue_one(workflow, executor_id)
            finally:
                await asyncio.to_thread(lease.release)

    async def _reenqueue_one(self, workflow: PendingWorkflow, executor_id: str) -> None:
        """Re-enqueue one row of a dead executor, or retire it if it is a stale tick."""
        try:
            if workflow.is_own_tick:
                if await _retire_tick(self.control, workflow):
                    self.tally.retired_ticks.append(workflow.workflow_id)
                    logger.info(
                        "recovery_sweep.retired_tick",
                        workflow_id=workflow.workflow_id,
                        executor=executor_id,
                    )
                return
            changed = await self.control.reenqueue(workflow.workflow_id, executor_id)
            if changed:
                self.tally.reenqueued.append(workflow.workflow_id)
            logger.info(
                "recovery_sweep.reenqueued",
                workflow_id=workflow.workflow_id,
                executor=executor_id,
                queue=workflow.queue_name,
                changed=changed,
            )
        except Exception as error:  # Reported per workflow, never swallowed.
            self.tally.failed.append((workflow.workflow_id, f"re-enqueueing failed: {error}"))
            logger.error(
                "recovery_sweep.reenqueue_failed",
                workflow_id=workflow.workflow_id,
                error=str(error),
            )

    async def recover(self, entry: StrandedWorkflow) -> None:
        """Fork or retire one row, or cancel it if it is a stale tick of this sweep."""
        workflow = entry.workflow
        original = workflow.workflow_id
        try:
            if workflow.is_own_tick and not workflow.was_forked_from:
                if await _retire_tick(self.control, workflow):
                    self.tally.retired_ticks.append(original)
                    logger.info(
                        "recovery_sweep.retired_tick",
                        workflow_id=original,
                        from_version=workflow.application_version,
                    )
                return
            successor, created = await _recover_version(self.control, workflow, self.target)
            if created:
                self.tally.forked.append((original, successor))
            else:
                self.tally.retired.append(original)
            logger.info(
                "recovery_sweep.forked" if created else "recovery_sweep.retired",
                workflow_id=original,
                forked_workflow_id=successor,
                from_version=workflow.application_version,
                to_version=self.target,
            )
        except Exception as error:  # One bad row must not strand the rest.
            reason = (
                f"recovering workflow {original!r} from version "
                f"{workflow.application_version!r} onto {self.target!r} failed: {error}"
            )
            self.tally.failed.append((original, reason))
            logger.error("recovery_sweep.fork_failed", workflow_id=original, error=str(error))

    async def recover_version(self, version: str, entries: list[StrandedWorkflow]) -> bool:
        """Fork or retire one source version's rows under its exclusive lock.

        Returns:
            False if the latest version changed after the lock was taken, in which case
            nothing more is forked or retired by this sweep. True allows the next source
            version to proceed, even if this version was locked elsewhere or failed;
            failures are recorded in the tally.
        """
        ids = [entry.workflow.workflow_id for entry in entries]
        try:
            async with self.holds(version) as hold:
                if not hold.held:
                    self.tally.version_live += len(entries)
                    logger.info(
                        "recovery_sweep.version_live",
                        version=version,
                        rows=len(entries),
                        mode=None if hold.holder_mode is None else hold.holder_mode.value,
                    )
                    return True
                latest = await self.control.latest_version()
                if latest != self.target:
                    self.tally.skipped.extend(ids)
                    logger.warning(
                        "recovery_sweep.skipped",
                        reason="the latest version changed mid-sweep",
                        target=self.target,
                        latest=latest,
                        version=version,
                        rows=len(entries),
                    )
                    return False
                for entry in entries:
                    await self.recover(entry)
        except Exception as error:  # A lock that cannot be taken forks nothing of it.
            done = {
                *self.tally.retired,
                *(original for original, _ in self.tally.forked),
                *self.tally.retired_ticks,
                *(workflow_id for workflow_id, _ in self.tally.failed),
            }
            reason = f"holding version {version!r} for recovery failed: {error}"
            self.tally.failed.extend((wid, reason) for wid in ids if wid not in done)
            logger.error("recovery_sweep.version_lock_failed", version=version, error=str(error))
        return True


async def run_recovery_sweep(
    settings: Settings,
    *,
    control: RecoveryControl,
    own_version: str,
    lease_factory: LeaseFactory | None = None,
    version_holds: VersionHolds | None = None,
    registrations: Registrations | None = None,
) -> SweepOutcome:
    """Recover pending and queued work that no live worker will claim.

    Args:
        settings: Resolved configuration; names the database holding the leases.
        control: The DBOS operations to sweep with: the launched instance's or a client's.
        own_version: The version this sweeper runs. Only a sweeper on the target, DBOS's
            latest version, handles fork and retire candidates. Dead-executor recovery
            runs on every version, including cancellation of those executors' stale ticks.
        lease_factory: Builds the lease for one executor identity. Defaults to a lease on
            the configured DBOS system database.
        version_holds: Tries to hold one version's lock exclusively. Defaults to
            `version_lease.hold_exclusive` on the configured database.
        registrations: Reads the version registry. Defaults to `version_lease.registrations`.

    Returns:
        What the sweep did, including per-workflow failures. One workflow that cannot be
        recovered does not abort the rest; a sweep that gave up on the first bad row
        would leave the remaining orphans stranded for another interval.

    Raises:
        Exception: The initial latest-version read, pending-row listing or registry read
            failed; nothing was written. Errors re-reading the latest version under a
            source lock are instead recorded as per-workflow failures.
    """
    leases = lease_factory or (
        lambda executor_id: ExecutorLease.for_settings(
            settings, executor_id, purpose="recovery sweep"
        )
    )
    holds = version_holds or (lambda version: version_lease.hold_exclusive(settings, version))
    read_registrations = registrations or (lambda: version_lease.registrations(settings))

    target = await control.latest_version()
    pending = await control.list_pending()
    sweep = _Sweep(control=control, target=target, leases=leases, holds=holds)
    plan = sweep.refuse_misrouted(list(plan_recovery(pending, current_version=target)))

    reenqueues = [entry for entry in plan if entry.action is RecoveryAction.REENQUEUE]
    versioned = [entry for entry in plan if entry.action is not RecoveryAction.REENQUEUE]
    on_target = own_version == target
    by_version: dict[str, list[StrandedWorkflow]] = {}
    unlocked: list[StrandedWorkflow] = []
    for entry in versioned:
        version = entry.workflow.application_version
        if version is None or version == target:
            unlocked.append(entry)
        else:
            by_version.setdefault(version, []).append(entry)

    # Read before any write, so an unreadable registry leaves everything as it was.
    registry: Mapping[str, datetime] = (
        await read_registrations() if on_target and by_version else {}
    )

    await sweep.reenqueue_dead_executors(reenqueues)

    if not on_target:
        sweep.tally.skipped.extend(entry.workflow.workflow_id for entry in versioned)
        if versioned:
            logger.info(
                "recovery_sweep.skipped",
                reason="this sweeper is not on the target version",
                own_version=own_version,
                target=target,
                rows=len(versioned),
            )
        return sweep.tally.outcome()

    # The sweeper runs the target and holds its shared lease, so the target is live.
    for entry in unlocked:
        await sweep.recover(entry)

    target_registered = registry.get(target)
    remaining = list(by_version.items())
    while remaining:
        version, entries = remaining.pop(0)
        ids = [entry.workflow.workflow_id for entry in entries]
        registered_at = registry.get(version)
        if registered_at is None:
            sweep.tally.unregistered.extend(ids)
            logger.warning("recovery_sweep.unregistered_version", version=version, rows=len(ids))
            continue
        if target_registered is None:
            reason = f"target version {target!r} is not registered; its deploy order is unknown"
            sweep.tally.failed.extend((wid, reason) for wid in ids)
            logger.error("recovery_sweep.target_unregistered", target=target, version=version)
            continue
        if registered_at >= target_registered:
            sweep.tally.newer_than_target.extend(ids)
            logger.warning(
                "recovery_sweep.newer_than_target", version=version, target=target, rows=len(ids)
            )
            continue
        if not await sweep.recover_version(version, entries):
            sweep.tally.skipped.extend(
                entry.workflow.workflow_id for _, rest in remaining for entry in rest
            )
            break

    return sweep.tally.outcome()
