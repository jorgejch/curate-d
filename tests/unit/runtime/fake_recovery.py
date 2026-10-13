"""In-memory stand-ins for what the recovery sweep reads and writes in DBOS.

`FakeRecoveryControl` keeps workflow rows as DBOS reports them and applies forks,
cancels and re-enqueues the way DBOS does, recording every call. `FakeRegistrations`,
`FakeVersionHolds` and `FakePromotionFence` stand in for the version registry, the
exclusive version lock and the shared promotion fence.
"""

from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from sqlite3 import IntegrityError as SQLiteIntegrityError

from dbos import WorkflowStatus
from dbos._sys_db import WorkflowStatuses
from sqlalchemy.exc import IntegrityError

from curate_d.runtime.sweep import ENQUEUED, PENDING, PendingWorkflow
from curate_d.runtime.version_lease import LockMode, VersionHold

EPOCH = datetime(2026, 10, 1, tzinfo=UTC)


def workflow_status(
    workflow_id: str,
    *,
    status: WorkflowStatuses = "PENDING",
    executor_id: str | None = "retired",
    version: str | None = "v1",
    queue_name: str | None = "stage",
    queue_partition_key: str | None = None,
    forked_from: str | None = None,
    was_forked_from: bool = False,
    schedule_name: str | None = None,
) -> WorkflowStatus:
    """Return a DBOS status row with the fields the sweep reads."""
    row = WorkflowStatus()
    row.workflow_id = workflow_id
    row.status = status
    row.executor_id = executor_id
    row.app_version = version
    row.queue_name = queue_name
    row.queue_partition_key = queue_partition_key
    row.forked_from = forked_from
    row.was_forked_from = was_forked_from
    row.schedule_name = schedule_name
    return row


def registered(*versions: str) -> dict[str, datetime]:
    """Return registrations for `versions`, each a day after the previous one."""
    return {version: EPOCH + timedelta(days=index) for index, version in enumerate(versions)}


class FakeRecoveryControl:
    """A `RecoveryControl` over in-memory rows, failing on demand."""

    def __init__(
        self,
        rows: list[WorkflowStatus] | None = None,
        *,
        latest: str | list[str] = "v2",
        steps: Mapping[str, list[int]] | None = None,
    ) -> None:
        """Hold `rows`; `latest` may list successive answers, the last one repeating."""
        self.rows: dict[str, WorkflowStatus] = {row.workflow_id: row for row in rows or []}
        self._latest = [latest] if isinstance(latest, str) else list(latest)
        self.steps: dict[str, list[int]] = dict(steps or {})
        self.calls: list[tuple[str, ...]] = []
        self.errors: dict[tuple[str, str], Exception] = {}
        """Exceptions to raise, keyed by (operation, workflow ID or '*')."""

    def _raise_for(self, operation: str, workflow_id: str = "*") -> None:
        """Raise the error registered for this operation and workflow, if any."""
        error = self.errors.get((operation, workflow_id)) or self.errors.get((operation, "*"))
        if error is not None:
            raise error

    async def list_pending(self) -> tuple[PendingWorkflow, ...]:
        """List PENDING and ENQUEUED rows as the DBOS adapter maps them."""
        self.calls.append(("list_pending",))
        self._raise_for("list_pending")
        return tuple(
            PendingWorkflow(
                workflow_id=row.workflow_id,
                executor_id=row.executor_id,
                application_version=row.app_version,
                status=row.status,
                queue_name=row.queue_name,
                queue_partition_key=row.queue_partition_key,
                was_forked_from=row.was_forked_from,
                schedule_name=row.schedule_name,
            )
            for row in self.rows.values()
            if row.status in {PENDING, ENQUEUED}
        )

    async def step_ids(self, workflow_id: str) -> list[int]:
        """Return the recorded step IDs of `workflow_id`."""
        self.calls.append(("step_ids", workflow_id))
        self._raise_for("step_ids", workflow_id)
        return list(self.steps.get(workflow_id, []))

    async def status(self, workflow_id: str) -> WorkflowStatus | None:
        """Return the row of `workflow_id`, or None."""
        self.calls.append(("status", workflow_id))
        self._raise_for("status", workflow_id)
        return self.rows.get(workflow_id)

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
        """Create the successor ENQUEUED and mark the original forked, as DBOS does.

        Raises:
            IntegrityError: `successor_id` already exists, shaped like DBOS's primary-key
                conflict.
        """
        self.calls.append(("fork", workflow_id, successor_id, str(start_step)))
        self._raise_for("fork", workflow_id)
        if successor_id in self.rows:
            raise IntegrityError(
                "INSERT INTO workflow_status",
                {},
                SQLiteIntegrityError("UNIQUE constraint failed: workflow_status.workflow_uuid"),
            )
        self.rows[successor_id] = workflow_status(
            successor_id,
            status="ENQUEUED",
            executor_id=None,
            version=application_version,
            queue_name=queue_name,
            queue_partition_key=queue_partition_key,
            forked_from=workflow_id,
        )
        self.rows[workflow_id].was_forked_from = True

    async def cancel(self, workflow_id: str) -> None:
        """Cancel `workflow_id`, clearing its queue as DBOS does."""
        self.calls.append(("cancel", workflow_id))
        self._raise_for("cancel", workflow_id)
        row = self.rows[workflow_id]
        row.status = "CANCELLED"
        row.queue_name = None

    async def latest_version(self) -> str:
        """Return the next scripted latest version, repeating the last."""
        self.calls.append(("latest_version",))
        self._raise_for("latest_version")
        return self._latest.pop(0) if len(self._latest) > 1 else self._latest[0]

    async def reenqueue(self, workflow_id: str, executor_id: str) -> bool:
        """Return a PENDING row owned by `executor_id` to its own queue."""
        self.calls.append(("reenqueue", workflow_id, executor_id))
        self._raise_for("reenqueue", workflow_id)
        row = self.rows.get(workflow_id)
        if row is None or row.status != PENDING or row.executor_id != executor_id:
            return False
        row.status = "ENQUEUED"
        row.executor_id = None
        return True

    def writes(self) -> list[tuple[str, ...]]:
        """Return the fork, cancel and re-enqueue calls, in order."""
        return [call for call in self.calls if call[0] in {"fork", "cancel", "reenqueue"}]


class FakeRegistrations:
    """The version registry: a fixed mapping, or an error."""

    def __init__(
        self, entries: Mapping[str, datetime] | None = None, error: Exception | None = None
    ) -> None:
        """Answer with `entries`, or raise `error`."""
        self.entries = dict(entries or {})
        self.error = error
        self.reads = 0

    async def __call__(self) -> Mapping[str, datetime]:
        """Return the registrations, or raise the configured error."""
        self.reads += 1
        if self.error is not None:
            raise self.error
        return dict(self.entries)


class FakeVersionHolds:
    """Exclusive version holds that succeed unless a version is held or failing.

    Records `hold:<version>` when a hold is taken and `release:<version>` when its block
    exits, however it exits, so a test can assert the lock never outlives its rows.
    """

    def __init__(
        self,
        *,
        held_elsewhere: Mapping[str, LockMode] | None = None,
        errors: Mapping[str, Exception] | None = None,
    ) -> None:
        """Refuse the versions in `held_elsewhere`; raise for the versions in `errors`."""
        self.held_elsewhere = dict(held_elsewhere or {})
        self.errors = dict(errors or {})
        self.events: list[str] = []
        self.held: set[str] = set()

    def __call__(self, version: str) -> AbstractAsyncContextManager[VersionHold]:
        """Try to hold `version` exclusively for the duration of the block."""
        return self._hold(version)

    @asynccontextmanager
    async def _hold(self, version: str) -> AsyncIterator[VersionHold]:
        """Yield the outcome of one attempt, releasing a taken hold on every exit."""
        if version in self.errors:
            raise self.errors[version]
        if version in self.held_elsewhere:
            yield VersionHold(version, held=False, holder_mode=self.held_elsewhere[version])
            return
        self.held.add(version)
        self.events.append(f"hold:{version}")
        try:
            yield VersionHold(version, held=True)
        finally:
            self.held.discard(version)
            self.events.append(f"release:{version}")


class FakePromotionFence:
    """The promotion fence, taken shared unless a promotion holds it or it fails.

    Records `fence:hold` when taken and `fence:release` when its block exits, however it
    exits, in `events`, which a test may share with `FakeVersionHolds` to check nesting.
    """

    def __init__(
        self,
        *,
        promoting: bool = False,
        error: Exception | None = None,
        events: list[str] | None = None,
    ) -> None:
        """Refuse the fence while `promoting`; raise `error` on every attempt."""
        self.promoting = promoting
        self.error = error
        self.events = events if events is not None else []
        self.held = False
        self.attempts = 0

    def __call__(self) -> AbstractAsyncContextManager[bool]:
        """Try once to hold the fence shared for the duration of the block."""
        return self._hold()

    @asynccontextmanager
    async def _hold(self) -> AsyncIterator[bool]:
        """Yield whether the fence was taken, releasing a taken fence on every exit."""
        self.attempts += 1
        if self.error is not None:
            raise self.error
        if self.promoting:
            yield False
            return
        self.held = True
        self.events.append("fence:hold")
        try:
            yield True
        finally:
            self.held = False
            self.events.append("fence:release")
