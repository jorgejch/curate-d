"""Executor operations: who is alive, what each holds, and the commands on DBOS workflows.

Liveness is read, never tried: an executor is live while a session holds its lease in
`pg_locks` (runtime/lease.py), and a version runs while a worker holds its version lease
shared. The API never calls `pg_try_advisory_lock`, so reading the view never blocks or
claims anything. Where the API's role cannot see other roles' `pg_stat_activity`,
`lease_since` and `holder` come back null; liveness, which `pg_locks` alone answers, stays
correct.

Cancel, resume, fork and trigger go straight to `DBOSClient`, each idempotent on the
request ID the client sends: cancel by nature, fork and trigger through a workflow ID
derived from it, resume through an attribute on the row naming the request that resumed
it. Resume is refused on a PENDING row: DBOS's resume sets any unfinished row back to
ENQUEUED, which would run a live executor's workflow twice; PENDING work left by a
vanished executor is what `recover` is for. Trigger does not use
`DBOSClient.trigger_schedule`, whose workflow ID carries the time it fired, so a retried
request would fire again.

Recovery runs as a workflow on the I/O workers (workflows/executors.py); the API enqueues
it, waits up to `executor_recovery_wait_seconds` for its report, and otherwise answers
202 with the workflow to poll.

None of these writes an audit entry: they act on DBOS workflow rows, not on decisions
about files (domain/audit.py). The record is DBOS's own (status, fork lineage, the
resume attribute) and the `operator_command.*` log lines, which name the operator and the
request ID.
"""

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Final, Literal, Protocol
from uuid import UUID

from dbos import (
    DBOSClient,
    EnqueueOptions,
    SetWorkflowID,
    VersionInfo,
    WorkflowHandleAsync,
    WorkflowStatus,
)
from dbos._utils import INTERNAL_QUEUE_NAME
from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.configuration import operator
from curate_d.api.errors import (
    CONFLICT,
    NOT_FOUND,
    REJECTED,
    UNAVAILABLE,
    ConflictError,
    NotFoundError,
    RejectedError,
    UnavailableError,
    problems,
)
from curate_d.observability.logging import get_logger
from curate_d.runtime.dbos_app import APPLICATION_NAME
from curate_d.runtime.lease import (
    EXCLUSIVE_MODE,
    EXECUTOR_LEASE_PURPOSE,
    SHARED_MODE,
    join_key,
    lease_key,
    version_lease_key,
)
from curate_d.runtime.lineage import (
    UnrelatedSuccessorError,
    confirmed_successor,
    is_workflow_id_conflict,
)
from curate_d.runtime.queues import SPECS_BY_NAME, QueueName
from curate_d.runtime.workflow_ids import (
    operator_fork_workflow_id,
    operator_trigger_workflow_id,
    recover_all_workflow_id,
    recover_executor_workflow_id,
)
from curate_d.workflows.executors import WORKFLOW_NAME as RECOVERY_WORKFLOW

logger = get_logger(__name__)

PENDING: Final = "PENDING"
ENQUEUED: Final = "ENQUEUED"
RESUMABLE: Final = frozenset({"CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"})
FINISHED: Final = frozenset({"SUCCESS", "ERROR"})

# The attribute a resume leaves on the row, naming the request that made it.
RESUMED_BY: Final = "curate_d_resumed_by_request"

# A worker's lease session's name in pg_stat_activity, before the executor identity.
_LEASE_SESSION_PREFIX: Final = f"{APPLICATION_NAME} {EXECUTOR_LEASE_PURPOSE} "

_ADVISORY_LOCKS: Final = text(
    "SELECT l.classid::bigint AS classid, l.objid::bigint AS objid, l.mode, l.pid,"
    " a.application_name, a.backend_start"
    " FROM pg_locks AS l LEFT JOIN pg_stat_activity AS a ON a.pid = l.pid"
    " WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1"
    " AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())"
)


class PendingWork(BaseModel):
    """How many of one executor's workflows share a version, queue and status."""

    application_version: str | None
    queue_name: str | None
    status: str
    count: int


class ExecutorView(BaseModel):
    """One executor identity: whether it is alive, and the work it owns."""

    executor_id: str
    live: bool
    lease_since: AwareDatetime | None = None
    """When the session holding the lease started; null if the API cannot see it."""
    holder: str | None = None
    """The holding session's application name, or its pid when the name is not visible."""
    version: str | None = None
    """The version whose lease the holding session also holds; null for a build without one."""
    pending: list[PendingWork] = []
    oldest_pending_age_seconds: float | None = None
    stranded: bool = False
    """Owns PENDING work and holds no lease: nothing will run that work until recovered."""


class QueueDepth(BaseModel):
    """How many queued workflows share a queue, partition and status."""

    queue_name: str
    partition: str | None
    status: str
    count: int


class VersionView(BaseModel):
    """One registered application version."""

    version: str
    created_at: AwareDatetime
    latest: bool
    running: bool
    """A worker holds its version lease, or a live executor without one owns PENDING work on it."""


class ExecutorsView(BaseModel):
    """Every executor, queue depth and application version."""

    executors: list[ExecutorView]
    queues: list[QueueDepth]
    versions: list[VersionView]
    latest_version: str | None


class CommandRequest(BaseModel):
    """A command's request ID, chosen once by the client so a retry is recognised."""

    request_id: UUID


class ResumeRequest(CommandRequest):
    """A resume, onto `queue_name`, else the row's queue, else DBOS's internal queue."""

    queue_name: QueueName | None = None


class ForkRequest(CommandRequest):
    """A fork from `start_step` onto a version, the latest when unset."""

    start_step: int = Field(ge=1)
    application_version: str | None = None
    queue_name: QueueName | None = None


class CommandResult(BaseModel):
    """What a cancel or resume found and did."""

    command: str
    workflow_id: str
    request_id: UUID
    changed: bool
    status: str


class ForkResult(BaseModel):
    """The fork a request made, or the one an earlier attempt of it made."""

    workflow_id: str
    forked_workflow_id: str
    request_id: UUID
    application_version: str | None
    changed: bool


class TriggerResult(BaseModel):
    """The run a trigger enqueued, or the one an earlier attempt of it enqueued."""

    schedule_name: str
    workflow_id: str
    request_id: UUID
    changed: bool


class RecoveryReport(BaseModel):
    """What one recovery did, as the sweep reports it (runtime/sweep.py `SweepOutcome`)."""

    workflow_id: str
    executor_id: str | None
    reenqueued: list[str] = []
    forked: list[tuple[str, str]] = []
    retired: list[str] = []
    left_running: int = 0
    failed: list[tuple[str, str]] = []
    skipped: list[tuple[str, str]] = []
    running_versions: list[str] = []
    refused: str | None = None


class RecoveryStarted(BaseModel):
    """A recovery still running; poll its workflow for the report."""

    workflow_id: str
    executor_id: str | None


@dataclass(frozen=True)
class AdvisoryLock:
    """One granted session-level bigint advisory lock, as pg_locks reports it."""

    key: int
    mode: str
    pid: int
    application_name: str | None = None
    backend_start: datetime | None = None


def _epoch_ms(value: int | None) -> datetime | None:
    """Return a DBOS epoch-milliseconds timestamp as an aware datetime."""
    return None if value is None else datetime.fromtimestamp(value / 1000, UTC)


def _candidates(rows: Sequence[WorkflowStatus], locks: Sequence[AdvisoryLock]) -> set[str]:
    """Return every executor identity a row names or a lease session announces."""
    named = {row.executor_id for row in rows if row.executor_id}
    announced = {
        lock.application_name.removeprefix(_LEASE_SESSION_PREFIX)
        for lock in locks
        if lock.application_name and lock.application_name.startswith(_LEASE_SESSION_PREFIX)
    }
    return named | announced


def _pending_of(rows: Sequence[WorkflowStatus]) -> list[PendingWork]:
    """Count rows by version, queue and status."""
    counts: dict[tuple[str | None, str | None, str], int] = {}
    for row in rows:
        group = (row.app_version, row.queue_name, row.status)
        counts[group] = counts.get(group, 0) + 1
    return [
        PendingWork(application_version=version, queue_name=queue, status=state, count=count)
        for (version, queue, state), count in sorted(counts.items(), key=lambda item: str(item[0]))
    ]


def _oldest_pending_age(rows: Sequence[WorkflowStatus], now: datetime) -> float | None:
    """Return how long the oldest PENDING row has been running, in seconds."""
    started = [
        _epoch_ms(row.dequeued_at or row.created_at) for row in rows if row.status == PENDING
    ]
    known = [moment for moment in started if moment is not None]
    return None if not known else max(0.0, (now - min(known)).total_seconds())


def executors_view(
    rows: Sequence[WorkflowStatus],
    queued: Sequence[WorkflowStatus],
    locks: Sequence[AdvisoryLock],
    versions: Sequence[VersionInfo],
    latest: str | None,
    now: datetime,
) -> ExecutorsView:
    """Combine workflow rows, held advisory locks and versions into the executor view.

    Args:
        rows: PENDING and ENQUEUED workflows.
        queued: Workflows on a queue, for queue depth.
        locks: Granted bigint advisory locks in the system database.
        versions: Registered application versions.
        latest: The latest version's name, if any is registered.
        now: The time ages are measured to.
    """
    executor_keys = {lease_key(APPLICATION_NAME, name): name for name in _candidates(rows, locks)}
    version_keys = {
        version_lease_key(APPLICATION_NAME, info["version_name"]): info["version_name"]
        for info in versions
    }
    holders: dict[str, AdvisoryLock] = {}
    versions_by_pid: dict[int, set[str]] = {}
    sharing_pids: set[int] = set()
    for lock in locks:
        if lock.mode == EXCLUSIVE_MODE and lock.key in executor_keys:
            holders[executor_keys[lock.key]] = lock
        elif lock.mode == SHARED_MODE:
            sharing_pids.add(lock.pid)
            if lock.key in version_keys:
                versions_by_pid.setdefault(lock.pid, set()).add(version_keys[lock.key])

    running = {name for names in versions_by_pid.values() for name in names}
    by_executor: dict[str, list[WorkflowStatus]] = {}
    for row in rows:
        if row.executor_id:
            by_executor.setdefault(row.executor_id, []).append(row)
    executors: list[ExecutorView] = []
    for executor_id in sorted(set(executor_keys.values())):
        owned = by_executor.get(executor_id, [])
        lease = holders.get(executor_id)
        if lease is not None and lease.pid not in sharing_pids:
            # A build that predates the version lease: its PENDING work's versions run.
            running |= {
                row.app_version for row in owned if row.status == PENDING and row.app_version
            }
        leased_versions = sorted(versions_by_pid.get(lease.pid, ())) if lease else []
        executors.append(
            ExecutorView(
                executor_id=executor_id,
                live=lease is not None,
                lease_since=lease.backend_start if lease else None,
                holder=(lease.application_name or f"pid {lease.pid}") if lease else None,
                version=leased_versions[0] if leased_versions else None,
                pending=_pending_of(owned),
                oldest_pending_age_seconds=_oldest_pending_age(owned, now),
                stranded=lease is None and any(row.status == PENDING for row in owned),
            )
        )

    depth: dict[tuple[str, str | None, str], int] = {}
    for row in queued:
        if row.queue_name is not None:
            group = (row.queue_name, row.queue_partition_key, row.status)
            depth[group] = depth.get(group, 0) + 1
    return ExecutorsView(
        executors=executors,
        queues=[
            QueueDepth(queue_name=queue, partition=partition, status=state, count=count)
            for (queue, partition, state), count in sorted(
                depth.items(), key=lambda item: str(item[0])
            )
        ],
        versions=[
            VersionView(
                version=info["version_name"],
                created_at=_epoch_ms(info["created_at"]) or now,
                latest=info["version_name"] == latest,
                running=info["version_name"] in running,
            )
            for info in versions
        ],
        latest_version=latest,
    )


def recovery_report(
    workflow_id: str, executor_id: str | None, outcome: Mapping[str, object]
) -> RecoveryReport:
    """Return a recovery workflow's result as a report."""
    return RecoveryReport.model_validate(
        {**outcome, "workflow_id": workflow_id, "executor_id": executor_id}
    )


def refusal_of(report: RecoveryReport) -> str | None:
    """Return why a finished recovery answers 409, or None when it did what it could.

    A recovery is refused when the sweep refused it, or when all it found to do was fork
    work of versions still running.
    """
    if report.refused is not None:
        return f"{report.refused} (recovery {report.workflow_id})"
    if report.running_versions and not (report.reenqueued or report.forked or report.retired):
        versions = ", ".join(report.running_versions)
        return (
            f"nothing recovered: versions still running: {versions}; their work is forked "
            f"once their workers are gone (recovery {report.workflow_id})"
        )
    return None


class ExecutorsBackend(Protocol):
    """The executor view and the operator's commands on DBOS workflows."""

    async def overview(self) -> ExecutorsView:
        """Return every executor, queue depth and application version."""
        ...

    async def recover(
        self, executor_id: str | None, request_id: UUID, by: str
    ) -> RecoveryReport | RecoveryStarted:
        """Recover what `executor_id` left behind, or every executor's when None.

        Raises:
            ConflictError: the executor holds its lease.
            NotFoundError: the executor holds no lease and owns no PENDING or ENQUEUED
                work, and no earlier attempt of this request recovered it.
        """
        ...

    async def recovery(self, workflow_id: str) -> RecoveryReport | RecoveryStarted:
        """Return a recovery's report, or that it is still running.

        Raises:
            NotFoundError: no recovery has this workflow ID.
        """
        ...

    async def cancel(self, workflow_id: str, request_id: UUID) -> CommandResult:
        """Cancel a workflow; cancelling a finished or cancelled one changes nothing.

        Raises:
            NotFoundError: there is no such workflow.
        """
        ...

    async def resume(self, workflow_id: str, request: ResumeRequest) -> CommandResult:
        """Resume a cancelled workflow, or one out of recovery attempts.

        Raises:
            NotFoundError: there is no such workflow.
            ConflictError: the workflow is in no state to resume, and no earlier attempt
                of this request resumed it.
            RejectedError: the named queue needs a partition the workflow has none for.
        """
        ...

    async def fork(self, workflow_id: str, request: ForkRequest) -> ForkResult:
        """Fork a workflow from a step under the request's successor ID.

        Raises:
            NotFoundError: there is no such workflow.
            ConflictError: the successor ID is held by an unrelated workflow.
            RejectedError: the target queue needs a partition the workflow has none for.
        """
        ...

    async def trigger(self, schedule_name: str, request_id: UUID) -> TriggerResult:
        """Enqueue one run of a schedule's workflow now, once per request.

        Raises:
            NotFoundError: there is no such schedule.
        """
        ...


type Command = Literal["recover", "cancel", "resume", "fork", "trigger"]


def router(backend: Callable[[Request], ExecutorsBackend]) -> APIRouter:
    """Return the executor endpoints over `backend`."""
    routes = APIRouter(tags=["executors"])
    uses = Depends(backend)
    by = Depends(operator)

    def issued(command: Command, target: str, request_id: UUID, who: str) -> None:
        """Log that `who` issued `command` on `target`."""
        logger.info(
            "operator_command.issued",
            command=command,
            target=target,
            request_id=str(request_id),
            operator=who,
        )

    def completed(
        command: Command, target: str, request_id: UUID, who: str, result: object
    ) -> None:
        """Log what `command` on `target` came to."""
        logger.info(
            "operator_command.completed",
            command=command,
            target=target,
            request_id=str(request_id),
            operator=who,
            result=result,
        )

    async def recovered(
        executors: ExecutorsBackend,
        executor_id: str | None,
        command: CommandRequest,
        who: str,
        response: Response,
    ) -> RecoveryReport | RecoveryStarted:
        """Run one recovery, answering 202 while it runs and 409 when it was refused."""
        target = executor_id or "*"
        issued("recover", target, command.request_id, who)
        try:
            outcome = await executors.recover(executor_id, command.request_id, who)
        except (ConflictError, NotFoundError, RejectedError) as error:
            completed("recover", target, command.request_id, who, f"refused: {error}")
            raise
        if isinstance(outcome, RecoveryStarted):
            response.status_code = status.HTTP_202_ACCEPTED
            completed("recover", target, command.request_id, who, "running")
            return outcome
        refusal = refusal_of(outcome)
        completed(
            "recover", target, command.request_id, who, refusal or outcome.model_dump(mode="json")
        )
        if refusal is not None:
            raise ConflictError(refusal)
        return outcome

    @routes.get("/executors", response_model=ExecutorsView)
    async def get_executors(executors: Annotated[ExecutorsBackend, uses]) -> ExecutorsView:
        """Return every executor's liveness and pending work, queue depth and versions."""
        return await executors.overview()

    @routes.get(
        "/executors/{executor_id}", response_model=ExecutorView, responses=problems(NOT_FOUND)
    )
    async def get_executor(
        executor_id: str, executors: Annotated[ExecutorsBackend, uses]
    ) -> ExecutorView:
        """Return one executor's liveness and pending work."""
        for executor in (await executors.overview()).executors:
            if executor.executor_id == executor_id:
                return executor
        raise NotFoundError("executor", executor_id)

    @routes.post(
        "/executors/recover",
        response_model=RecoveryReport | RecoveryStarted,
        responses={
            status.HTTP_202_ACCEPTED: {"model": RecoveryStarted},
            **problems(CONFLICT, UNAVAILABLE),
        },
    )
    async def recover_all(
        command: CommandRequest,
        response: Response,
        executors: Annotated[ExecutorsBackend, uses],
        who: Annotated[str, by],
    ) -> RecoveryReport | RecoveryStarted:
        """Recover every executor's stranded work: dead executors' and retired versions'."""
        return await recovered(executors, None, command, who, response)

    @routes.post(
        "/executors/{executor_id}/recover",
        response_model=RecoveryReport | RecoveryStarted,
        responses={
            status.HTTP_202_ACCEPTED: {"model": RecoveryStarted},
            **problems(NOT_FOUND, CONFLICT, REJECTED, UNAVAILABLE),
        },
    )
    async def recover(
        executor_id: str,
        command: CommandRequest,
        response: Response,
        executors: Annotated[ExecutorsBackend, uses],
        who: Annotated[str, by],
    ) -> RecoveryReport | RecoveryStarted:
        """Recover a vanished executor's work; refused while it holds its lease."""
        return await recovered(executors, executor_id, command, who, response)

    @routes.get(
        "/executors/recoveries/{workflow_id}",
        response_model=RecoveryReport | RecoveryStarted,
        responses={
            status.HTTP_202_ACCEPTED: {"model": RecoveryStarted},
            **problems(NOT_FOUND, UNAVAILABLE),
        },
    )
    async def get_recovery(
        workflow_id: str, response: Response, executors: Annotated[ExecutorsBackend, uses]
    ) -> RecoveryReport | RecoveryStarted:
        """Return a recovery's report once it has finished; 202 while it runs."""
        outcome = await executors.recovery(workflow_id)
        if isinstance(outcome, RecoveryStarted):
            response.status_code = status.HTTP_202_ACCEPTED
        return outcome

    @routes.post(
        "/workflows/{workflow_id:path}/cancel",
        response_model=CommandResult,
        responses=problems(NOT_FOUND),
    )
    async def cancel(
        workflow_id: str,
        command: CommandRequest,
        executors: Annotated[ExecutorsBackend, uses],
        who: Annotated[str, by],
    ) -> CommandResult:
        """Cancel a workflow. Repeating it changes nothing."""
        issued("cancel", workflow_id, command.request_id, who)
        result = await executors.cancel(workflow_id, command.request_id)
        completed("cancel", workflow_id, command.request_id, who, result.model_dump(mode="json"))
        return result

    @routes.post(
        "/workflows/{workflow_id:path}/resume",
        response_model=CommandResult,
        responses=problems(NOT_FOUND, CONFLICT, REJECTED),
    )
    async def resume(
        workflow_id: str,
        command: ResumeRequest,
        executors: Annotated[ExecutorsBackend, uses],
        who: Annotated[str, by],
    ) -> CommandResult:
        """Resume a CANCELLED or MAX_RECOVERY_ATTEMPTS_EXCEEDED workflow; never a PENDING one."""
        issued("resume", workflow_id, command.request_id, who)
        try:
            result = await executors.resume(workflow_id, command)
        except (ConflictError, NotFoundError, RejectedError) as error:
            completed("resume", workflow_id, command.request_id, who, f"refused: {error}")
            raise
        completed("resume", workflow_id, command.request_id, who, result.model_dump(mode="json"))
        return result

    @routes.post(
        "/workflows/{workflow_id:path}/fork",
        response_model=ForkResult,
        responses=problems(NOT_FOUND, CONFLICT, REJECTED),
    )
    async def fork(
        workflow_id: str,
        command: ForkRequest,
        executors: Annotated[ExecutorsBackend, uses],
        who: Annotated[str, by],
    ) -> ForkResult:
        """Fork a workflow from a step; the same request ID returns the same fork."""
        issued("fork", workflow_id, command.request_id, who)
        try:
            result = await executors.fork(workflow_id, command)
        except (ConflictError, NotFoundError, RejectedError) as error:
            completed("fork", workflow_id, command.request_id, who, f"refused: {error}")
            raise
        completed("fork", workflow_id, command.request_id, who, result.model_dump(mode="json"))
        return result

    @routes.post(
        "/schedules/{schedule_name}/trigger",
        response_model=TriggerResult,
        responses=problems(NOT_FOUND, REJECTED),
    )
    async def trigger(
        schedule_name: str,
        command: CommandRequest,
        executors: Annotated[ExecutorsBackend, uses],
        who: Annotated[str, by],
    ) -> TriggerResult:
        """Run a schedule's workflow now; the same request ID enqueues it once."""
        issued("trigger", schedule_name, command.request_id, who)
        try:
            result = await executors.trigger(schedule_name, command.request_id)
        except (NotFoundError, RejectedError) as error:
            completed("trigger", schedule_name, command.request_id, who, f"refused: {error}")
            raise
        completed("trigger", schedule_name, command.request_id, who, result.model_dump(mode="json"))
        return result

    return routes


class LedgerExecutors:
    """The executor view and commands over the workflow system database."""

    def __init__(
        self, engine: AsyncEngine, workflows: DBOSClient, *, recovery_wait_seconds: float
    ) -> None:
        """Bind the system database's engine, the workflow client and the recovery wait."""
        self._engine = engine
        self._workflows = workflows
        self._recovery_wait_seconds = recovery_wait_seconds

    async def _locks(self) -> list[AdvisoryLock]:
        """Return every granted bigint advisory lock in the system database."""
        async with self._engine.connect() as connection:
            result = await connection.execute(_ADVISORY_LOCKS)
            return [
                AdvisoryLock(
                    key=join_key(row.classid, row.objid),
                    mode=row.mode,
                    pid=row.pid,
                    application_name=row.application_name or None,
                    backend_start=row.backend_start,
                )
                for row in result
            ]

    async def _latest(self) -> str | None:
        """Return the latest registered version, or None before any worker registered one."""
        versions = await self._workflows.list_application_versions_async()
        return versions[0]["version_name"] if versions else None

    async def _status(self, workflow_id: str, *, output: bool = False) -> WorkflowStatus | None:
        """Return a workflow's status, or None if there is no such workflow."""
        found = await self._workflows.list_workflows_async(
            workflow_ids=[workflow_id], load_input=output, load_output=output
        )
        return found[0] if found else None

    async def _existing(self, workflow_id: str) -> WorkflowStatus:
        """Return a workflow's status.

        Raises:
            NotFoundError: there is no such workflow.
        """
        found = await self._status(workflow_id)
        if found is None:
            raise NotFoundError("workflow", workflow_id)
        return found

    async def overview(self) -> ExecutorsView:
        """Read rows, queues and versions through the client and leases from pg_locks."""
        rows = await self._workflows.list_workflows_async(
            status=[PENDING, ENQUEUED], load_input=False, load_output=False
        )
        queued = await self._workflows.list_queued_workflows_async(
            load_input=False, load_output=False
        )
        versions = await self._workflows.list_application_versions_async()
        latest = versions[0]["version_name"] if versions else None
        return executors_view(
            rows, queued, await self._locks(), versions, latest, datetime.now(UTC)
        )

    async def _lease_held(self, executor_id: str) -> bool:
        """Return whether pg_locks shows `executor_id`'s lease held."""
        key = lease_key(APPLICATION_NAME, executor_id)
        return any(lock.key == key and lock.mode == EXCLUSIVE_MODE for lock in await self._locks())

    async def _owns_work(self, executor_id: str) -> bool:
        """Return whether `executor_id` owns any PENDING or ENQUEUED workflow."""
        owned = await self._workflows.list_workflows_async(
            status=[PENDING, ENQUEUED],
            executor_id=executor_id,
            limit=1,
            load_input=False,
            load_output=False,
        )
        return bool(owned)

    async def recover(
        self, executor_id: str | None, request_id: UUID, by: str
    ) -> RecoveryReport | RecoveryStarted:
        """Enqueue the recovery under the request's workflow ID, then wait for its report.

        A repeated request finds the first one's workflow and waits on that, so its
        report comes back even once the executor's lease is held again.
        """
        try:
            workflow_id = (
                recover_all_workflow_id(str(request_id))
                if executor_id is None
                else recover_executor_workflow_id(executor_id, str(request_id))
            )
        except ValueError as error:
            raise RejectedError(str(error)) from error
        if await self._status(workflow_id) is None:
            if executor_id is not None and await self._lease_held(executor_id):
                raise ConflictError(
                    f"executor {executor_id} is alive (lease held); it recovers its own work"
                )
            if executor_id is not None and not await self._owns_work(executor_id):
                # As `GET /executors/{id}` answers it: no lease and no work, so no executor.
                raise NotFoundError("executor", executor_id)
            options: EnqueueOptions = {
                "workflow_name": RECOVERY_WORKFLOW,
                "queue_name": QueueName.MAINTENANCE.value,
                "workflow_id": workflow_id,
            }
            latest = await self._latest()
            if latest is not None:
                options["app_version"] = latest
            await self._workflows.enqueue_async(options, executor_id, by, str(request_id))
        handle: WorkflowHandleAsync[
            Mapping[str, object]
        ] = await self._workflows.retrieve_workflow_async(workflow_id)
        try:
            outcome = await asyncio.wait_for(
                handle.get_result(polling_interval_sec=0.5), self._recovery_wait_seconds
            )
        except TimeoutError:
            return RecoveryStarted(workflow_id=workflow_id, executor_id=executor_id)
        except Exception as error:
            raise UnavailableError(
                f"recovery {workflow_id} ended without a report: {error}"
            ) from error
        return recovery_report(workflow_id, executor_id, outcome)

    async def recovery(self, workflow_id: str) -> RecoveryReport | RecoveryStarted:
        """Read a recovery workflow's state and, once it succeeded, its report."""
        found = await self._status(workflow_id, output=True)
        if found is None or found.name != RECOVERY_WORKFLOW:
            raise NotFoundError("recovery", workflow_id)
        executor_id = None
        if found.input is not None and found.input["args"]:
            executor_id = found.input["args"][0]
        if found.status == "SUCCESS" and isinstance(found.output, Mapping):
            return recovery_report(workflow_id, executor_id, found.output)
        if found.status in FINISHED or found.status in RESUMABLE:
            raise UnavailableError(
                f"recovery {workflow_id} ended {found.status} without a report: {found.error}"
            )
        return RecoveryStarted(workflow_id=workflow_id, executor_id=executor_id)

    async def cancel(self, workflow_id: str, request_id: UUID) -> CommandResult:
        """Cancel an unfinished workflow; a finished or cancelled one is left as it is."""
        found = await self._existing(workflow_id)
        changed = found.status not in FINISHED and found.status != "CANCELLED"
        if changed:
            await self._workflows.cancel_workflow_async(workflow_id)
            found = await self._existing(workflow_id)
        return CommandResult(
            command="cancel",
            workflow_id=workflow_id,
            request_id=request_id,
            changed=changed,
            status=found.status,
        )

    async def resume(self, workflow_id: str, request: ResumeRequest) -> CommandResult:
        """Mark the row with the request, then resume it; a marked row is not resumed twice."""
        found = await self._existing(workflow_id)
        attributes = dict(found.attributes or {})
        if found.status in RESUMABLE:
            queue = found.queue_name
            if request.queue_name is not None:
                # DBOS's resume keeps the row's partition key: a partitioned queue needs one.
                queue, _ = self._routing(found, request.queue_name)
            attributes[RESUMED_BY] = str(request.request_id)
            # Marked first: a crash between the two leaves a CANCELLED row a retry resumes.
            await self._workflows.update_workflow_attributes_async(workflow_id, attributes)
            await self._workflows.resume_workflow_async(workflow_id, queue_name=queue)
            found = await self._existing(workflow_id)
            changed = True
        elif attributes.get(RESUMED_BY) == str(request.request_id):
            changed = False
        elif found.status == PENDING:
            raise ConflictError(
                f"workflow {workflow_id} is PENDING: an executor owns it, and resuming would "
                f"run it twice. PENDING work is recovered with `recover`, not resumed"
            )
        else:
            raise ConflictError(
                f"workflow {workflow_id} is {found.status}; only CANCELLED or "
                f"MAX_RECOVERY_ATTEMPTS_EXCEEDED workflows are resumed"
            )
        return CommandResult(
            command="resume",
            workflow_id=workflow_id,
            request_id=request.request_id,
            changed=changed,
            status=found.status,
        )

    def _routing(
        self, original: WorkflowStatus, queue_name: QueueName | None
    ) -> tuple[str | None, str | None]:
        """Return the queue and partition a fork of `original` goes to.

        Raises:
            RejectedError: the queue is partitioned and `original` carries no partition.
        """
        if queue_name is None:
            return original.queue_name, original.queue_partition_key
        if not SPECS_BY_NAME[queue_name].partitioned:
            return queue_name.value, None
        if original.queue_partition_key is None:
            raise RejectedError(
                f"queue {queue_name.value} is partitioned and workflow "
                f"{original.workflow_id} has no partition key"
            )
        return queue_name.value, original.queue_partition_key

    async def fork(self, workflow_id: str, request: ForkRequest) -> ForkResult:
        """Fork under the request's successor ID; a conflict is accepted only from the same fork."""
        original = await self._existing(workflow_id)
        successor = operator_fork_workflow_id(str(request.request_id))
        version = request.application_version or await self._latest()
        try:
            if confirmed_successor(await self._status(successor), workflow_id, successor):
                return ForkResult(
                    workflow_id=workflow_id,
                    forked_workflow_id=successor,
                    request_id=request.request_id,
                    application_version=version,
                    changed=False,
                )
            queue, partition = self._routing(original, request.queue_name)
            changed = True
            try:
                with SetWorkflowID(successor):
                    await self._workflows.fork_workflow_async(
                        workflow_id,
                        request.start_step,
                        application_version=version,
                        queue_name=queue,
                        queue_partition_key=partition,
                    )
            except IntegrityError as error:
                # Only the primary-key conflict means an earlier attempt already forked.
                if not is_workflow_id_conflict(error):
                    raise
                if not confirmed_successor(await self._status(successor), workflow_id, successor):
                    raise
                changed = False
        except UnrelatedSuccessorError as error:
            raise ConflictError(str(error)) from error
        return ForkResult(
            workflow_id=workflow_id,
            forked_workflow_id=successor,
            request_id=request.request_id,
            application_version=version,
            changed=changed,
        )

    async def trigger(self, schedule_name: str, request_id: UUID) -> TriggerResult:
        """Enqueue the schedule's workflow as its scheduler would, under the request's ID."""
        schedule = await self._workflows.get_schedule_async(schedule_name)
        if schedule is None:
            raise NotFoundError("schedule", schedule_name)
        try:
            workflow_id = operator_trigger_workflow_id(schedule_name, str(request_id))
        except ValueError as error:
            raise RejectedError(str(error)) from error
        if await self._status(workflow_id) is not None:
            return TriggerResult(
                schedule_name=schedule_name,
                workflow_id=workflow_id,
                request_id=request_id,
                changed=False,
            )
        options: EnqueueOptions = {
            "workflow_name": schedule["workflow_name"],
            "queue_name": schedule["queue_name"] or INTERNAL_QUEUE_NAME,
            "workflow_id": workflow_id,
        }
        if schedule["workflow_class_name"] is not None:
            options["class_name"] = schedule["workflow_class_name"]
        # The scheduler enqueues on the latest version too, so a live worker dequeues it.
        latest = await self._latest()
        if latest is not None:
            options["app_version"] = latest
        # Enqueuing under an existing ID returns that workflow, so a race collapses too.
        await self._workflows.enqueue_async(options, datetime.now(UTC), schedule["context"])
        return TriggerResult(
            schedule_name=schedule_name,
            workflow_id=workflow_id,
            request_id=request_id,
            changed=True,
        )
