"""Recover retired executors and versions without creating competing workflow lineages.

Pending work owned by a retired executor is redispatched in place: resumed onto its own
queue under its existing ID (`_redispatch`). Pending or queued work pinned to a retired
version needs a fork, retaining its queue and provider partition. Delayed work is left to
DBOS's scheduler; once due it becomes ENQUEUED and is eligible for the next sweep. Forking
a DELAYED row would drop its delay.

Each original gets one deterministic successor ID, independent of the target version.
The DBOS primary key arbitrates simultaneous sweeps, and its fork transaction copies
checkpoints and sets ``was_forked_from`` atomically. Only after confirming that successor
exists do we cancel the original. A retry repairs a crash between those operations,
even after another deployment or rollback. Later deployments recover the surviving leaf
instead of creating another sibling from an ancestor.

Executor liveness is the executor lease (lease.py): an executor is dead only if the sweep
can take its lease, and the sweep holds that lease while redispatching the executor's
rows, so the executor cannot start and run startup recovery on them mid-sweep. DBOS's
resume is unconditional, so each redispatch re-reads the row first (still PENDING under the
dead executor) and again afterwards (ENQUEUED, or already dequeued by a live worker); any
other answer is a failure. Resume also resets DBOS's recovery-attempt counter, so the sweep
counts its own redispatches of a row in the row's workflow attributes and stops at
`MAX_SWEEP_REDISPATCHES` per application version: a row that kills its worker on every run
is reported, not rescued forever.

Version liveness is the version lease (version_lease.py). The target is DBOS's latest
version, read when the sweep starts; only a sweeper running that version forks or
retires. A non-target version's rows are forked or retired only if that version
registered before the target, and only while the sweep holds its lock exclusively, which
no live worker of it allows. A build that predates the lease never registered and is left
alone.

Every fork and retirement runs under the promotion fence, held shared and taken once
without waiting; a promotion holding it skips them all for this tick. Promotion is the only
way latest moves backwards, so under the fence the target cannot be superseded by an older
version, whose sweeper could never fork the rows back. The sweep re-reads the latest
version once it holds the fence and again after taking each version's lock, and stops
forking if it changed. A roll-forward is not fenced: a new build's first launch can make it
latest mid-batch, and the rest of the batch still goes to the old target. That heals
itself: the old target registered before the new one, so the new target's sweeper forks
those rows once the old target's workers are gone.

DBOS commits each fork and cancel on its own connection, so losing a lock's session does
not undo them. The sweep asks the fence's session, and the source version lock's, before
each fork and before every cancel, a stale tick's included. Lost before the fork, the row
fails and the batch stops, the rest skipped. Lost before the cancel of an original whose
successor is confirmed, whether forked by this call or found committed, the original is
still cancelled, since two live lineages are worse, then the row fails and the batch stops;
a stale tick is left uncancelled. The next tick's plan finishes whatever this one left.
After every fork's cancel the original must read CANCELLED, or the row fails as an
unverified lineage.

A stale tick of the sweep's own schedule is cancelled under the same gates rather than
forked or redispatched: the next tick does its work. A row whose queue is NULL, or names a
queue no curate.d worker dequeues, is never forked or redispatched: DBOS sends a queue-less
resume to its internal queue, which every worker dequeues, so the row could run on the
wrong profile. Retiring one still cancels it.

DBOS calls made through `RecoveryControl` are not checkpointed when the sweep runs in a
step: a re-run repeats the whole sweep, which is safe because every fork is keyed by the
deterministic successor ID, every cancel is gated on a re-read status, and every
redispatch on a re-read status under the executor's lease.
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
from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError

from curate_d.config import Settings
from curate_d.observability.logging import get_logger
from curate_d.runtime import version_lease
from curate_d.runtime.lease import ExecutorLease
from curate_d.runtime.queues import QueueName
from curate_d.runtime.version_lease import FenceHold, VersionHold

logger = get_logger(__name__)

PENDING = "PENDING"
ENQUEUED = "ENQUEUED"
CANCELLED = "CANCELLED"

RECOVERY_SCHEDULE_NAME: Final = "recovery-sweep"
"""The scheduled sweep's own schedule; its stale ticks are retired, never recovered."""

MAX_SWEEP_REDISPATCHES: Final = 10
"""Redispatches of one row under one application version before the sweep stops resuming it."""

REDISPATCH_COUNT_ATTRIBUTE: Final = "curate_d.recovery_redispatches"
"""The workflow attribute counting the sweep's redispatches of a row."""

REDISPATCH_VERSION_ATTRIBUTE: Final = "curate_d.recovery_redispatch_version"
"""The workflow attribute naming the application version that count was recorded under."""

_DEQUEUED_QUEUES: Final = frozenset(queue.value for queue in QueueName)
"""The queues some curate.d worker dequeues; a fork or redispatch onto any other is refused."""


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
    """Fork and retire rows left because this sweeper is not on the target version, the
    latest version changed mid-sweep, or a promotion held the promotion fence."""
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

    async def reenqueue(self, workflow_id: str, *, queue_name: str) -> None:
        """Resume `workflow_id` onto `queue_name`, unconditionally; `_redispatch` gates it."""
        ...

    async def set_attributes(self, workflow_id: str, attributes: dict[str, object]) -> None:
        """Replace `workflow_id`'s workflow attributes with `attributes`."""
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

    async def reenqueue(self, workflow_id: str, *, queue_name: str) -> None:
        """Resume `workflow_id` onto `queue_name`, unconditionally; `_redispatch` gates it.

        The queue is always named: a resume without one lands on DBOS's internal queue.
        """
        await DBOS.resume_workflow_async(workflow_id, queue_name=queue_name)

    async def set_attributes(self, workflow_id: str, attributes: dict[str, object]) -> None:
        """Replace `workflow_id`'s workflow attributes with `attributes`."""
        await DBOS.update_workflow_attributes_async(workflow_id, attributes)


class ClientRecoveryControl:
    """`RecoveryControl` over a `DBOSClient`, for the operator sweep.

    `before_write` runs before every write, so a lost standalone version lease stops the
    sweep before its next fork, cancel, attribute update or redispatch.
    """

    def __init__(self, client: DBOSClient, *, before_write: Callable[[], None]) -> None:
        """Bind the adapter to `client` and the guard run before each write."""
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

    async def reenqueue(self, workflow_id: str, *, queue_name: str) -> None:
        """Resume `workflow_id` onto `queue_name`, unconditionally; `_redispatch` gates it.

        The queue is always named: a resume without one lands on DBOS's internal queue.
        """
        self._before_write()
        await self._client.resume_workflow_async(workflow_id, queue_name=queue_name)

    async def set_attributes(self, workflow_id: str, attributes: dict[str, object]) -> None:
        """Replace `workflow_id`'s workflow attributes with `attributes`."""
        self._before_write()
        await self._client.update_workflow_attributes_async(workflow_id, attributes)


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


type LockCheck = Callable[[], Awaitable[str | None]]
"""Names the lock the sweep forks and retires under that its session lost, or None."""


class _LockLostError(Exception):
    """A lock the sweep forks and retires under was found lost; it stops for this tick."""

    def __init__(self, reason: str, *, unverified_status: str | None = None) -> None:
        """Record `reason`, and the status an already-cancelled original reads, if wrong."""
        super().__init__(reason)
        self.unverified_status = unverified_status


class _LineageUnverifiedError(Exception):
    """The original does not read CANCELLED after its successor's fork and its cancel."""


async def _cancel_verified(control: RecoveryControl, original: str) -> str | None:
    """Cancel `original` and return the status it reads afterwards, if not CANCELLED."""
    await control.cancel(original)
    after = await control.status(original)
    if after is not None and after.status == CANCELLED:
        return None
    return "missing" if after is None else str(after.status)


async def _recover_version(
    control: RecoveryControl, workflow: PendingWorkflow, version: str, *, lost: LockCheck
) -> tuple[str, bool]:
    """Commit or reuse one successor, then retire its source; never cancel on fork failure.

    Database uniqueness is the cross-process lock: only a committed matching successor
    makes an insertion conflict recoverable. Other database errors still surface.
    Control errors also propagate, including cancellation failures after the successor
    has been committed.

    `lost` is asked right before the fork and again before the cancel, whether this call
    forked or found the successor committed. Lost before the cancel, the original is still
    cancelled: its successor is confirmed, and two live lineages are worse than one.

    Returns:
        The successor ID and whether this call created it, rather than reusing a
        previously committed or concurrently created successor.

    Raises:
        ValueError: The successor belongs to another source; the source is no longer
            eligible, or already has a manual fork, and no matching successor exists; or
            the fork published no successor.
        IntegrityError: The fork violated a constraint other than the workflow primary
            key, or a primary-key conflict had no confirmed matching successor.
        _LockLostError: A lock was found lost before the fork, which was then not made,
            or before the cancel, in which case the original was still cancelled.
        _LineageUnverifiedError: The original does not read CANCELLED after its cancel.
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
            start_step = await _fork_start_step(control, original)
            lock = await lost()
            if lock is not None:
                raise _LockLostError(f"{lock} lost before fork")
            try:
                await control.fork(
                    original,
                    successor,
                    start_step,
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
    lock = await lost()
    unverified = await _cancel_verified(control, original)
    if lock is not None:
        stage = "after fork" if created else "before retirement"
        raise _LockLostError(
            f"{lock} lost {stage}; the original was cancelled", unverified_status=unverified
        )
    if unverified is not None:
        raise _LineageUnverifiedError(
            f"successor {successor!r} exists but the original reads {unverified!r} after its cancel"
        )
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
type PromotionFence = Callable[[], AbstractAsyncContextManager[FenceHold]]
"""Tries once to hold the promotion fence shared for the duration of a block."""


def _queue_refusal(queue_name: str | None) -> str | None:
    """Return why a fork or redispatch onto `queue_name` would misroute the row, if it would."""
    if queue_name is None:
        return "it names no queue"
    if queue_name not in _DEQUEUED_QUEUES:
        return f"it names {queue_name!r}, which no curate.d worker dequeues"
    return None


def redispatch_count(status: WorkflowStatus) -> int:
    """Return how often the sweep has redispatched this row under its current version.

    A count recorded under another application version is zero: a fork copies the
    attributes onto its successor, and a new build is when a crashing row may be fixed.
    A missing or malformed count is zero too. A row has no version only before its first
    dequeue, which stamps the executor's version, so a PENDING row's version is never
    None; a hand-written one is counted under None until a dequeue re-keys it.
    """
    attributes = status.attributes or {}
    if attributes.get(REDISPATCH_VERSION_ATTRIBUTE) != status.app_version:
        return 0
    count = attributes.get(REDISPATCH_COUNT_ATTRIBUTE)
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return 0
    return count


class _Redispatch(StrEnum):
    """What one gated redispatch did."""

    REENQUEUED = "reenqueued"
    """Resumed, and read back ENQUEUED on its own queue."""

    DEQUEUED = "dequeued"
    """Resumed, and already running on a live executor when read back."""

    UNOWNED = "unowned"
    """Not resumed: a re-read found it no longer PENDING under the dead executor."""


class _RedispatchError(Exception):
    """A redispatch refused or unverified, with the ERROR event the sweep logs for it."""

    def __init__(self, reason: str, *, event: str) -> None:
        """Record `reason` for the tally and `event` for the log."""
        super().__init__(reason)
        self.event = event


async def _redispatch(
    control: RecoveryControl, workflow: PendingWorkflow, executor_id: str
) -> _Redispatch:
    """Resume one row of a dead executor onto its own queue, gated and verified.

    Call only while holding `executor_id`'s lease. DBOS's resume is unconditional, so the
    premise, PENDING under `executor_id`, is checked twice: on a first re-read that also
    gives the queue and the redispatch count, and on a second one right before the resume,
    after the count is recorded on the row. The count write is a round trip in which an
    operator's cancel may land, and a resume after it would undo the cancel; the second
    re-read narrows that window to the resume's own round trip. A count recorded for a row
    then left alone is an over-count, never an under-count. The row is resumed onto the
    queue it names and read again afterwards to confirm the resume took.

    Raises:
        _RedispatchError: The re-read row names a queue no curate.d worker dequeues, it
            reached `MAX_SWEEP_REDISPATCHES` under its version, or the read-back after
            the resume is neither ENQUEUED nor PENDING on another executor.
    """
    workflow_id = workflow.workflow_id
    status = await control.status(workflow_id)
    if status is None or status.status != PENDING or status.executor_id != executor_id:
        return _Redispatch.UNOWNED
    queue_name = status.queue_name
    if queue_name is None or queue_name not in _DEQUEUED_QUEUES:
        raise _RedispatchError(
            f"redispatch of {workflow_id!r} refused: {_queue_refusal(queue_name)}",
            event="recovery_sweep.queue_refused",
        )
    count = redispatch_count(status)
    if count >= MAX_SWEEP_REDISPATCHES:
        raise _RedispatchError(
            f"redispatch of {workflow_id!r} refused: redispatched {count} times under "
            f"version {status.app_version!r}, the cap of {MAX_SWEEP_REDISPATCHES}",
            event="recovery_sweep.redispatch_cap",
        )
    # Updating attributes replaces the whole mapping, so the row's others are kept.
    await control.set_attributes(
        workflow_id,
        {
            **(status.attributes or {}),
            REDISPATCH_COUNT_ATTRIBUTE: count + 1,
            REDISPATCH_VERSION_ATTRIBUTE: status.app_version,
        },
    )
    current = await control.status(workflow_id)
    if current is None or current.status != PENDING or current.executor_id != executor_id:
        return _Redispatch.UNOWNED
    await control.reenqueue(workflow_id, queue_name=queue_name)
    after = await control.status(workflow_id)
    if after is not None and after.status == ENQUEUED:
        return _Redispatch.REENQUEUED
    if after is not None and after.status == PENDING and after.executor_id != executor_id:
        return _Redispatch.DEQUEUED
    raise _RedispatchError(
        f"redispatch of {workflow_id!r} unverified: it reads "
        f"{None if after is None else after.status!r} under executor "
        f"{None if after is None else after.executor_id!r} after the resume",
        event="recovery_sweep.redispatch_unverified",
    )


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
        self.fence: FenceHold | None = None
        """The promotion fence hold every fork and retirement runs under, once taken."""
        self.tally = _Tally()

    def fail_unfinished(self, ids: Sequence[str], reason: str) -> None:
        """Record `reason` as the failure of every row in `ids` the tally has not settled."""
        tally = self.tally
        done = {
            *tally.retired,
            *(original for original, _ in tally.forked),
            *tally.retired_ticks,
            *(workflow_id for workflow_id, _ in tally.failed),
            *tally.skipped,
            *tally.unregistered,
            *tally.newer_than_target,
        }
        tally.failed.extend((workflow_id, reason) for workflow_id in ids if workflow_id not in done)

    def refuse_misrouted(self, entries: list[StrandedWorkflow]) -> list[StrandedWorkflow]:
        """Fail fork and redispatch rows with no queue, or one no curate.d worker dequeues.

        Return the remaining entries, including retirements and this sweep's own ticks,
        which bypass the queue check.
        """
        kept: list[StrandedWorkflow] = []
        for entry in entries:
            workflow = entry.workflow
            refusal = (
                None
                if entry.action is RecoveryAction.RETIRE or workflow.is_own_tick
                else _queue_refusal(workflow.queue_name)
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
        """Redispatch, or retire if a stale tick, each dead executor's PENDING rows."""
        by_executor: dict[str | None, list[PendingWorkflow]] = {}
        for entry in entries:
            by_executor.setdefault(entry.workflow.executor_id, []).append(entry.workflow)

        for executor_id, workflows in by_executor.items():
            if executor_id is None:
                self.tally.failed.extend(
                    (
                        workflow.workflow_id,
                        "re-enqueueing refused: a row without an executor has no lease "
                        "to prove its owner dead",
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
        """Redispatch one row of a dead executor, or retire it if it is a stale tick."""
        workflow_id = workflow.workflow_id
        try:
            if workflow.is_own_tick:
                if await _retire_tick(self.control, workflow):
                    self.tally.retired_ticks.append(workflow_id)
                    logger.info(
                        "recovery_sweep.retired_tick", workflow_id=workflow_id, executor=executor_id
                    )
                return
            result = await _redispatch(self.control, workflow, executor_id)
        except _RedispatchError as error:
            self.tally.failed.append((workflow_id, str(error)))
            logger.error(
                error.event, workflow_id=workflow_id, executor=executor_id, reason=str(error)
            )
            return
        except Exception as error:  # Reported per workflow, never swallowed.
            self.tally.failed.append((workflow_id, f"re-enqueueing failed: {error}"))
            logger.error(
                "recovery_sweep.reenqueue_failed", workflow_id=workflow_id, error=str(error)
            )
            return
        if result is _Redispatch.REENQUEUED:
            self.tally.reenqueued.append(workflow_id)
        elif result is _Redispatch.DEQUEUED:
            self.tally.left_running += 1
        logger.info(
            "recovery_sweep.reenqueued",
            workflow_id=workflow_id,
            executor=executor_id,
            queue=workflow.queue_name,
            result=result.value,
        )

    async def lost_lock(self, hold: VersionHold | None) -> str | None:
        """Name the lock this sweep forks and retires under that is lost, or return None.

        The fence is checked first, then `hold`, the source version's lock, if any.
        """
        if self.fence is None or not await self.fence.still_held():
            return "promotion fence"
        if hold is not None and not await hold.still_held():
            return "version lock"
        return None

    async def recover(self, entry: StrandedWorkflow, hold: VersionHold | None) -> bool:
        """Fork or retire one row, or cancel it if it is a stale tick of this sweep.

        Args:
            entry: The row and its remedy.
            hold: The source version's exclusive hold; None for version-less and
                on-target rows, which take no version lock.

        Returns:
            False if a lock was found lost, in which case the caller forks and retires
            nothing more this tick. True otherwise, failures included.
        """
        workflow = entry.workflow
        original = workflow.workflow_id
        prefix = (
            f"recovering workflow {original!r} from version "
            f"{workflow.application_version!r} onto {self.target!r} failed"
        )
        try:
            if workflow.is_own_tick and not workflow.was_forked_from:
                lock = await self.lost_lock(hold)
                if lock is not None:
                    raise _LockLostError(f"{lock} lost before retiring a stale tick; it was left")
                if await _retire_tick(self.control, workflow):
                    self.tally.retired_ticks.append(original)
                    logger.info(
                        "recovery_sweep.retired_tick",
                        workflow_id=original,
                        from_version=workflow.application_version,
                    )
                return True
            successor, created = await _recover_version(
                self.control, workflow, self.target, lost=lambda: self.lost_lock(hold)
            )
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
        except _LockLostError as error:
            self.tally.failed.append((original, f"{prefix}: {error}"))
            logger.error(
                "recovery_sweep.version_lock_lost",
                workflow_id=original,
                from_version=workflow.application_version,
                reason=str(error),
            )
            if error.unverified_status is not None:
                logger.error(
                    "recovery_sweep.lineage_unverified",
                    workflow_id=original,
                    status=error.unverified_status,
                )
            return False
        except _LineageUnverifiedError as error:
            self.tally.failed.append((original, f"{prefix}: {error}"))
            logger.error(
                "recovery_sweep.lineage_unverified", workflow_id=original, reason=str(error)
            )
        except Exception as error:  # One bad row must not strand the rest.
            self.tally.failed.append((original, f"{prefix}: {error}"))
            logger.error("recovery_sweep.fork_failed", workflow_id=original, error=str(error))
        return True

    async def recover_version(self, version: str, entries: list[StrandedWorkflow]) -> bool:
        """Fork or retire one source version's rows under its exclusive lock.

        Call only under the promotion fence. The latest version is re-read once the lock
        is taken; under the fence it can only have moved forward, to a build registered
        after the target, which recovers whatever this batch leaves on the target.

        Returns:
            False if the latest version changed after the lock was taken, or a lock was
            found lost mid-batch, in which case nothing more is forked or retired by this
            sweep and the version's remaining rows are skipped. True allows the next
            source version to proceed, even if this version was locked elsewhere or
            failed; failures are recorded in the tally.
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
                for index, entry in enumerate(entries):
                    if not await self.recover(entry, hold):
                        self.tally.skipped.extend(
                            rest.workflow.workflow_id for rest in entries[index + 1 :]
                        )
                        return False
        except Exception as error:  # A lock that cannot be taken forks nothing of it.
            self.fail_unfinished(ids, f"holding version {version!r} for recovery failed: {error}")
            logger.error("recovery_sweep.version_lock_failed", version=version, error=str(error))
        return True

    async def recover_fenced(
        self,
        unlocked: list[StrandedWorkflow],
        by_version: Mapping[str, list[StrandedWorkflow]],
        registry: Mapping[str, datetime],
    ) -> None:
        """Fork or retire every candidate, holding the promotion fence shared.

        Re-read the latest version first: a promotion may have landed between the sweep's
        first read and the fence. Then retire version-less and on-target rows, and recover
        each older registered source version under its own lock.

        Args:
            unlocked: Version-less and on-target rows, which take no version lock.
            by_version: Every other candidate, by source version.
            registry: Each registered version's first registration time.
        """
        ids = [
            entry.workflow.workflow_id
            for entries in (unlocked, *by_version.values())
            for entry in entries
        ]
        try:
            latest = await self.control.latest_version()
        except Exception as error:  # Unknown target: fork nothing, report every row.
            self.fail_unfinished(
                ids, f"re-reading the latest version under the promotion fence failed: {error}"
            )
            logger.error("recovery_sweep.latest_read_failed", error=str(error))
            return
        if latest != self.target:
            self.tally.skipped.extend(ids)
            logger.warning(
                "recovery_sweep.skipped",
                reason="the latest version changed mid-sweep",
                target=self.target,
                latest=latest,
                rows=len(ids),
            )
            return

        # The sweeper runs the target and holds its shared lease, so the target is live.
        for index, entry in enumerate(unlocked):
            if not await self.recover(entry, None):
                # `ids` lists the unlocked rows first, then every version's.
                self.tally.skipped.extend(ids[index + 1 :])
                return

        target_registered = registry.get(self.target)
        remaining = list(by_version.items())
        while remaining:
            version, entries = remaining.pop(0)
            version_ids = [entry.workflow.workflow_id for entry in entries]
            registered_at = registry.get(version)
            if registered_at is None:
                self.tally.unregistered.extend(version_ids)
                logger.warning(
                    "recovery_sweep.unregistered_version", version=version, rows=len(version_ids)
                )
                continue
            if target_registered is None:
                reason = (
                    f"target version {self.target!r} is not registered; its deploy order is unknown"
                )
                self.tally.failed.extend((wid, reason) for wid in version_ids)
                logger.error(
                    "recovery_sweep.target_unregistered", target=self.target, version=version
                )
                continue
            if registered_at >= target_registered:
                self.tally.newer_than_target.extend(version_ids)
                logger.warning(
                    "recovery_sweep.newer_than_target",
                    version=version,
                    target=self.target,
                    rows=len(version_ids),
                )
                continue
            if not await self.recover_version(version, entries):
                self.tally.skipped.extend(
                    entry.workflow.workflow_id for _, rest in remaining for entry in rest
                )
                break


async def run_recovery_sweep(
    settings: Settings,
    *,
    control: RecoveryControl,
    own_version: str,
    lease_factory: LeaseFactory | None = None,
    version_holds: VersionHolds | None = None,
    registrations: Registrations | None = None,
    promotion_fence: PromotionFence | None = None,
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
        promotion_fence: Tries once to hold the promotion fence shared. Every fork and
            retirement runs under it; refused, they are all skipped this tick, while
            dead-executor redispatches still run. Found lost before or after a fork, the
            rest of the forks and retirements are skipped. Defaults to
            `version_lease.hold_fence_shared` on the configured database.

    Returns:
        What the sweep did, including per-workflow failures. One workflow that cannot be
        recovered does not abort the rest; a sweep that gave up on the first bad row
        would leave the remaining orphans stranded for another interval.

    Raises:
        Exception: The initial latest-version read, pending-row listing or registry read
            failed; nothing was written. Errors taking the promotion fence, or re-reading
            the latest version under it or under a source lock, are instead recorded as
            per-workflow failures.
    """
    leases = lease_factory or (
        lambda executor_id: ExecutorLease.for_settings(
            settings, executor_id, purpose="recovery sweep"
        )
    )
    holds = version_holds or (lambda version: version_lease.hold_exclusive(settings, version))
    read_registrations = registrations or (lambda: version_lease.registrations(settings))
    fence = promotion_fence or (lambda: version_lease.hold_fence_shared(settings))

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

    if not versioned:
        return sweep.tally.outcome()

    ids = [entry.workflow.workflow_id for entry in versioned]
    try:
        async with fence() as fenced:
            if fenced.held:
                sweep.fence = fenced
                await sweep.recover_fenced(unlocked, by_version, registry)
            else:
                sweep.tally.skipped.extend(ids)
                logger.warning(
                    "recovery_sweep.skipped",
                    reason="a promotion is in progress",
                    target=target,
                    rows=len(ids),
                )
    except Exception as error:  # A fence that cannot be taken forks nothing.
        sweep.fail_unfinished(ids, f"holding the promotion fence failed: {error}")
        logger.error("recovery_sweep.fence_failed", error=str(error))
    return sweep.tally.outcome()
