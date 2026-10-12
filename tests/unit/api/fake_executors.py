"""The executor backend over an in-memory stand-in for `DBOSClient` and pg_locks."""

import asyncio
from dataclasses import dataclass, field
from sqlite3 import IntegrityError as SQLiteIntegrityError

from dbos import EnqueueOptions, VersionInfo, WorkflowSchedule, WorkflowStatus
from dbos._context import get_local_dbos_context
from sqlalchemy.exc import IntegrityError

from curate_d.api.executors import AdvisoryLock, LedgerExecutors


def workflow_status(workflow_id: str, **fields: object) -> WorkflowStatus:
    """Return a DBOS workflow status with every field the executor backend reads."""
    status = WorkflowStatus()
    defaults: dict[str, object] = {
        "workflow_id": workflow_id,
        "status": "PENDING",
        "name": "tests.workflow",
        "class_name": None,
        "executor_id": None,
        "app_version": None,
        "queue_name": None,
        "queue_partition_key": None,
        "created_at": None,
        "dequeued_at": None,
        "forked_from": None,
        "was_forked_from": False,
        "attributes": None,
        "input": None,
        "output": None,
        "error": None,
    }
    for name, value in {**defaults, **fields}.items():
        setattr(status, name, value)
    return status


def version(name: str, created_at: int = 1_700_000_000_000) -> VersionInfo:
    """Return a registered version's record."""
    return VersionInfo(
        version_id=name,
        version_name=name,
        version_timestamp=created_at,
        created_at=created_at,
        application_name="curate-d-local",
    )


class _Handle:
    """A workflow handle over a `FakeWorkflowClient` row."""

    def __init__(self, client: "FakeWorkflowClient", workflow_id: str) -> None:
        self._client = client
        self._workflow_id = workflow_id

    async def get_result(self, *, polling_interval_sec: float = 1.0) -> object:
        """Return the row's output once it succeeded; wait otherwise."""
        while True:
            row = self._client.rows[self._workflow_id]
            if row.status == "SUCCESS":
                return row.output
            if row.status == "ERROR":
                raise RuntimeError(str(row.error))
            await asyncio.sleep(polling_interval_sec)


@dataclass
class FakeWorkflowClient:
    """The slice of `DBOSClient` the executor backend uses, over rows in memory."""

    rows: dict[str, WorkflowStatus] = field(default_factory=dict)
    versions: list[VersionInfo] = field(default_factory=list)
    schedules: dict[str, WorkflowSchedule] = field(default_factory=dict)
    recovery_outcome: dict[str, object] | None = None
    """What an enqueued recovery returns at once; it never finishes when None."""
    calls: list[tuple[str, object]] = field(default_factory=list)
    enqueued: list[tuple[dict[str, object], tuple[object, ...]]] = field(default_factory=list)
    """Each enqueue's options and arguments."""

    def add(self, status: WorkflowStatus) -> WorkflowStatus:
        """Store `status` as a row and return it."""
        self.rows[status.workflow_id] = status
        return status

    async def list_workflows_async(
        self,
        *,
        workflow_ids: list[str] | None = None,
        status: list[str] | None = None,
        load_input: bool = True,
        load_output: bool = True,
    ) -> list[WorkflowStatus]:
        """Return the rows matching the IDs and statuses given."""
        return [
            row
            for row in self.rows.values()
            if (workflow_ids is None or row.workflow_id in workflow_ids)
            and (status is None or row.status in status)
        ]

    async def list_queued_workflows_async(
        self, *, load_input: bool = True, load_output: bool = True
    ) -> list[WorkflowStatus]:
        """Return the queued, unfinished rows."""
        return [
            row
            for row in self.rows.values()
            if row.queue_name is not None and row.status in {"ENQUEUED", "PENDING"}
        ]

    async def list_application_versions_async(self) -> list[VersionInfo]:
        """Return the registered versions, newest first."""
        return list(self.versions)

    async def enqueue_async(self, options: EnqueueOptions, *args: object) -> _Handle:
        """Store an ENQUEUED row; an existing ID returns the existing row, as DBOS does."""
        workflow_id = options.get("workflow_id", f"generated-{len(self.rows)}")
        self.calls.append(("enqueue", workflow_id))
        self.enqueued.append((dict(options), args))
        if workflow_id not in self.rows:
            row = self.add(
                workflow_status(
                    workflow_id,
                    status="ENQUEUED",
                    name=options["workflow_name"],
                    queue_name=options["queue_name"],
                    app_version=options.get("app_version"),
                    input={"args": args, "kwargs": {}},
                )
            )
            if options["workflow_name"] == "recover_executor" and self.recovery_outcome:
                row.status, row.output = "SUCCESS", self.recovery_outcome
        return _Handle(self, workflow_id)

    async def retrieve_workflow_async(self, workflow_id: str) -> _Handle:
        """Return a handle on an existing row."""
        return _Handle(self, workflow_id)

    async def cancel_workflow_async(self, workflow_id: str) -> None:
        """Cancel a row, clearing its queue as DBOS does."""
        self.calls.append(("cancel", workflow_id))
        row = self.rows[workflow_id]
        if row.status not in {"SUCCESS", "ERROR"}:
            row.status, row.queue_name = "CANCELLED", None

    async def update_workflow_attributes_async(
        self, workflow_id: str, attributes: dict[str, object] | None
    ) -> None:
        """Replace a row's attributes."""
        self.rows[workflow_id].attributes = attributes

    async def resume_workflow_async(self, workflow_id: str, *, queue_name: str | None) -> None:
        """Set an unfinished row back to ENQUEUED, as DBOS's resume does."""
        self.calls.append(("resume", (workflow_id, queue_name)))
        row = self.rows[workflow_id]
        if row.status not in {"SUCCESS", "ERROR"}:
            row.status, row.queue_name = "ENQUEUED", queue_name or "_dbos_internal_queue"

    async def fork_workflow_async(
        self,
        workflow_id: str,
        start_step: int,
        *,
        application_version: str | None,
        queue_name: str | None,
        queue_partition_key: str | None,
    ) -> None:
        """Copy a row under the ID `SetWorkflowID` assigned, failing on a taken ID."""
        context = get_local_dbos_context()
        assert context is not None and context.id_assigned_for_next_workflow
        successor = context.id_assigned_for_next_workflow
        context.id_assigned_for_next_workflow = ""
        self.calls.append(
            (
                "fork",
                (workflow_id, start_step, application_version, queue_name, queue_partition_key),
            )
        )
        if successor in self.rows:
            raise IntegrityError(
                "insert",
                {},
                SQLiteIntegrityError("UNIQUE constraint failed: workflow_status.workflow_uuid"),
            )
        self.add(
            workflow_status(
                successor,
                status="ENQUEUED",
                forked_from=workflow_id,
                app_version=application_version,
                queue_name=queue_name,
                queue_partition_key=queue_partition_key,
            )
        )
        self.rows[workflow_id].was_forked_from = True

    async def get_schedule_async(self, name: str) -> WorkflowSchedule | None:
        """Return a schedule by name."""
        return self.schedules.get(name)


class _NoEngine:
    """An engine the fake backend never connects to."""


class FakeExecutors(LedgerExecutors):
    """The ledger executor backend over a `FakeWorkflowClient` and advisory locks in memory."""

    def __init__(
        self, workflows: FakeWorkflowClient | None = None, *, wait_seconds: float = 0.2
    ) -> None:
        """Bind a fresh fake client unless one is given; recoveries are waited on briefly."""
        self.workflows = workflows if workflows is not None else FakeWorkflowClient()
        self.locks: list[AdvisoryLock] = []
        super().__init__(
            _NoEngine(),  # type: ignore[arg-type]
            self.workflows,  # type: ignore[arg-type]
            recovery_wait_seconds=wait_seconds,
        )

    async def _locks(self) -> list[AdvisoryLock]:
        """Return the locks set on the fake."""
        return list(self.locks)
