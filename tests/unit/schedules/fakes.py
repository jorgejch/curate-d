"""An in-memory stand-in for the DBOS client calls schedule reconciliation makes."""

from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from dbos import ClientScheduleInput, Queue, VersionInfo, WorkflowSchedule

from curate_d.runtime.schedule_fence import ScheduleFence, StaleScheduleDeploymentError

APPLICATION = "curate-d-local"


class ClientFailureError(RuntimeError):
    """A client call failed, as a lost connection would make it."""


class FakeScheduleClient:
    """DBOS's schedule rows, versions and queues as `DBOSClient` sees them for one application.

    Writes behave as DBOS's: `apply_schedules` upserts by name and keeps a row's id,
    status and firing history, and claims an unclaimed row; `delete_schedule` ignores a
    missing name. Every write emulates the fence the real engine runs: refused unless the
    fence is armed, and stale unless the fence's version is the latest.
    """

    def __init__(
        self,
        fence: ScheduleFence,
        *,
        versions: tuple[str, ...] = ("1.0.0",),
        queues: tuple[str, ...] = ("maintenance",),
        application: str = APPLICATION,
    ) -> None:
        self.fence = fence
        self.application = application
        self.versions = list(versions)
        """Version names, latest first."""
        self.queues = set(queues)
        self.rows: dict[str, WorkflowSchedule] = {}
        self.writes: list[tuple[str, object]] = []
        self.fail_deletes: set[str] = set()
        """Names whose next delete fails, once each."""
        self.before_write: list[str] = []
        """Versions to register, newest last, before the next write: a rollout landing."""
        self.on_list_versions: list[tuple[str, ...]] = []
        """Successive version lists the next listings return, as workers register."""
        self.on_list_queues: list[tuple[str, ...]] = []
        """Successive queue sets the next listings return, as workers register them."""

    def store(
        self,
        name: str,
        *,
        owner: str | None = APPLICATION,
        workflow: str = "wf",
        cron: str = "0 0 * * *",
        status: str = "ACTIVE",
        last_fired_at: str | None = None,
    ) -> WorkflowSchedule:
        """Store a schedule row directly, as another writer left it."""
        row = WorkflowSchedule(
            schedule_id=str(uuid4()),
            schedule_name=name,
            workflow_name=workflow,
            workflow_class_name=None,
            schedule=cron,
            status=status,
            context=None,
            last_fired_at=last_fired_at,
            automatic_backfill=False,
            cron_timezone=None,
            queue_name=None,
            application_name=owner,
        )
        self.rows[name] = row
        return row

    def _write(self, kind: str, detail: object) -> None:
        """Run the fence's check for one write transaction, then record the write."""
        assert self.fence.is_armed, f"{kind} ran outside the fence"
        if self.before_write:
            self.versions.insert(0, self.before_write.pop(0))
        if self.versions[0] != self.fence.version:
            raise StaleScheduleDeploymentError(f"{self.versions[0]!r} is the latest")
        self.writes.append((kind, detail))

    def list_application_versions(self) -> list[VersionInfo]:
        """Return the versions, latest first, advancing a scripted registration if any."""
        if self.on_list_versions:
            self.versions = list(self.on_list_versions.pop(0))
        return [
            VersionInfo(
                version_id=name,
                version_name=name,
                version_timestamp=len(self.versions) - index,
                created_at=0,
                application_name=self.application,
            )
            for index, name in enumerate(self.versions)
        ]

    def set_latest_application_version(
        self, version_name: str, *, application_name: str | None = None
    ) -> None:
        """Move `version_name` to the front, as a fresh timestamp does."""
        self.versions.remove(version_name)
        self.versions.insert(0, version_name)
        self.writes.append(("promote", version_name))

    def list_queues(self, *, application_name: str | None = None) -> list[Queue]:
        """Return the registered queues, advancing a scripted registration if any."""
        if self.on_list_queues:
            self.queues = set(self.on_list_queues.pop(0))
        return [cast(Queue, SimpleNamespace(name=name)) for name in sorted(self.queues)]

    def apply_schedules(self, schedules: list[ClientScheduleInput]) -> None:
        """Upsert every schedule in one write, keeping id, status and firing history."""
        self._write("apply", [entry["schedule_name"] for entry in schedules])
        for entry in schedules:
            existing = self.rows.get(entry["schedule_name"])
            owner = existing["application_name"] if existing else None
            assert owner in (None, self.application), "another application's schedule"
            self.rows[entry["schedule_name"]] = WorkflowSchedule(
                schedule_id=existing["schedule_id"] if existing else str(uuid4()),
                schedule_name=entry["schedule_name"],
                workflow_name=entry["workflow_name"],
                workflow_class_name=entry.get("workflow_class_name"),
                schedule=entry["schedule"],
                status=existing["status"] if existing else "ACTIVE",
                context=entry.get("context"),
                last_fired_at=existing["last_fired_at"] if existing else None,
                automatic_backfill=entry.get("automatic_backfill", False),
                cron_timezone=entry.get("cron_timezone"),
                queue_name=entry.get("queue_name"),
                application_name=self.application,
            )

    def list_schedules(self, *, application_name: str | None = None) -> list[WorkflowSchedule]:
        """Return this application's rows and unclaimed ones, never another's."""
        owner = application_name or self.application
        return [
            WorkflowSchedule(**row)
            for row in self.rows.values()
            if row["application_name"] in (owner, None)
        ]

    def delete_schedule(self, name: str) -> None:
        """Delete one row in a write of its own; a missing name is not an error."""
        if name in self.fail_deletes:
            self.fail_deletes.discard(name)
            raise ClientFailureError(f"connection lost deleting {name!r}")
        self._write("delete", name)
        self.rows.pop(name, None)
