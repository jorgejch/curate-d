"""Apply a build's declared schedules and retire the ones it dropped, at deployment time.

The deploy job runs `reconcile` after a version's workers have rolled out. The workers
register the version when they launch and the queues right after, and DBOS fires every
schedule on its owner's latest version, so a version's declarations count once it is the
latest. `reconcile` therefore waits, bounded, for its version to be the latest and for
the queues its schedules name to exist, then:

1. applies every declared schedule in one transaction (`DBOSClient.apply_schedules`, an
   upsert by name that keeps each row's id, pause status and firing history), and
2. deletes, one transaction each, every stored schedule this application owns that no
   declaration names. Unclaimed rows (no owner) are logged and left alone; other
   applications' rows are never listed.

Each step is idempotent, and every transaction is fenced (`runtime/schedule_fence.py`),
so a failure part-way leaves either the old or the new definitions applied and some
retired rows still present, and running the command again converges. A command whose
version stops being the latest fails its next transaction and writes nothing more.

Rollback: roll the workers back first, then run `promote`, which makes the restored
version the latest (relaunching a registered version does not) and reconciles.
"""

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Final, Protocol

from dbos import ClientScheduleInput, Queue, VersionInfo, WorkflowSchedule

from curate_d.observability import get_logger

logger = get_logger(__name__)

# Long enough for a rollout's workers to launch and register queues after the deploy job
# starts; short enough that a job whose workers never come up fails within the deploy.
DEFAULT_WAIT_SECONDS: Final = 300.0
DEFAULT_POLL_SECONDS: Final = 2.0

# The fields a declaration sets, with the value a stored row has when it does not.
_DEFINITION_DEFAULTS: Final[dict[str, object]] = {
    "workflow_name": None,
    "workflow_class_name": None,
    "schedule": None,
    "context": None,
    "automatic_backfill": False,
    "cron_timezone": None,
    "queue_name": None,
}


class ScheduleClient(Protocol):
    """The `DBOSClient` calls reconciliation makes, all public DBOS API."""

    def list_application_versions(self) -> list[VersionInfo]:
        """Return this application's versions and unclaimed ones, newest first."""
        ...

    def set_latest_application_version(
        self, version_name: str, *, application_name: str | None = None
    ) -> None:
        """Make `version_name` the latest version."""
        ...

    def list_queues(self, *, application_name: str | None = None) -> list[Queue]:
        """Return this application's queues and unclaimed ones."""
        ...

    def apply_schedules(self, schedules: list[ClientScheduleInput]) -> None:
        """Create or replace `schedules` in one transaction."""
        ...

    def list_schedules(self, *, application_name: str | None = None) -> list[WorkflowSchedule]:
        """Return the schedules `application_name` owns and unclaimed ones."""
        ...

    def delete_schedule(self, name: str) -> None:
        """Delete the schedule named `name`; a missing one is not an error."""
        ...


class Fence(Protocol):
    """What makes the client's write transactions check they still own the schedules."""

    def armed(self) -> AbstractContextManager[None]:
        """Fence every transaction opened in the block."""
        ...


class ScheduleDeploymentError(RuntimeError):
    """The command stopped before writing anything further; `exit_code` says why."""

    exit_code = 1


class VersionNotRegisteredError(ScheduleDeploymentError):
    """The command's version was not registered within the wait: its workers are not up."""

    exit_code = 3


class VersionSupersededError(ScheduleDeploymentError):
    """Another version is the latest: a newer deployment, or a rollback, owns the schedules."""

    exit_code = 4


class QueuesNotDeclaredError(ScheduleDeploymentError):
    """A queue a schedule names was not registered within the wait."""

    exit_code = 5


@dataclass(frozen=True, slots=True)
class StoredSchedule:
    """A persisted schedule's name and the workflow it starts."""

    name: str
    workflow: str


@dataclass(frozen=True, slots=True)
class ScheduleReconciliation:
    """What one reconciliation committed.

    Attributes:
        applied: The declared schedule names, all created or replaced.
        deleted: This application's stored schedules deleted because none declares them.
        unclaimed_left: Undeclared schedules with no owner, left in place.
    """

    applied: tuple[str, ...]
    deleted: tuple[StoredSchedule, ...] = ()
    unclaimed_left: tuple[StoredSchedule, ...] = ()


@dataclass(frozen=True, slots=True)
class SchedulePlan:
    """What a reconciliation would change, without writing: the `show` dry run.

    Attributes:
        latest_version: DBOS's latest version of the application, None if none is.
        created: Declared schedules not stored yet.
        updated: Declared schedules stored with another definition, or unclaimed.
        unchanged: Declared schedules stored as declared.
        retired: This application's stored schedules no declaration names.
        unclaimed: Undeclared schedules with no owner, which reconciliation leaves alone.
    """

    latest_version: str | None
    created: tuple[str, ...]
    updated: tuple[str, ...]
    unchanged: tuple[str, ...]
    retired: tuple[StoredSchedule, ...]
    unclaimed: tuple[StoredSchedule, ...]

    @property
    def changes(self) -> bool:
        """Return whether reconciling would write anything."""
        return bool(self.created or self.updated or self.retired)


@dataclass(frozen=True, slots=True)
class Patience:
    """How long, and how often, the command waits for its version and queues."""

    seconds: float = DEFAULT_WAIT_SECONDS
    poll_seconds: float = DEFAULT_POLL_SECONDS
    clock: Callable[[], float] = field(default=time.monotonic)
    sleep: Callable[[float], None] = field(default=time.sleep)


def _version_names(client: ScheduleClient) -> list[str]:
    """Return the application's version names, latest first."""
    return [version["version_name"] for version in client.list_application_versions()]


def await_version(
    client: ScheduleClient,
    version: str,
    *,
    deadline: float,
    patience: Patience,
    latest: bool = True,
) -> None:
    """Wait until `version` is registered and, if `latest`, is the latest version.

    Raises:
        VersionNotRegisteredError: `version` was not registered by `deadline`.
        VersionSupersededError: `latest` is set and another version is the latest.
    """
    while True:
        names = _version_names(client)
        if version in names:
            if not latest or names[0] == version:
                return
            raise VersionSupersededError(
                f"version {version!r} is registered but {names[0]!r} is the latest: a newer "
                "deployment owns the schedules. If this version is meant to be current "
                "(a rollback), run `promote`."
            )
        if patience.clock() >= deadline:
            raise VersionNotRegisteredError(
                f"version {version!r} was not registered within {patience.seconds:g}s "
                f"(latest: {names[0] if names else None!r}); are its workers up?"
            )
        patience.sleep(patience.poll_seconds)


def await_queues(
    client: ScheduleClient, queues: Iterable[str], *, deadline: float, patience: Patience
) -> None:
    """Wait until every queue in `queues` is registered for the application.

    Raises:
        QueuesNotDeclaredError: One was still missing at `deadline`.
    """
    wanted = frozenset(queues)
    while True:
        missing = wanted - {queue.name for queue in client.list_queues()}
        if not missing:
            return
        if patience.clock() >= deadline:
            raise QueuesNotDeclaredError(
                f"queues {sorted(missing)} were not registered within {patience.seconds:g}s; "
                "workers register them at launch"
            )
        patience.sleep(patience.poll_seconds)


def _undeclared(
    stored: Sequence[WorkflowSchedule], application: str, declared: frozenset[str]
) -> tuple[tuple[StoredSchedule, ...], tuple[StoredSchedule, ...]]:
    """Split stored schedules no declaration names into this application's and unclaimed."""
    owned: list[StoredSchedule] = []
    unclaimed: list[StoredSchedule] = []
    for row in stored:
        if row["schedule_name"] in declared:
            continue
        entry = StoredSchedule(row["schedule_name"], row["workflow_name"])
        if row["application_name"] == application:
            owned.append(entry)
        elif row["application_name"] is None:
            unclaimed.append(entry)
    return tuple(owned), tuple(unclaimed)


def reconcile(
    client: ScheduleClient,
    fence: Fence,
    *,
    application: str,
    version: str,
    declared: Sequence[ClientScheduleInput],
    patience: Patience | None = None,
) -> ScheduleReconciliation:
    """Apply `declared` and delete this application's undeclared schedules, as `version`.

    Args:
        client: A client acting for `application`, its transactions checked by `fence`.
        fence: Armed for every write, so each commits only while `version` is the latest.
        application: The application that owns the schedules.
        version: This build's application version.
        declared: Every schedule this build declares.
        patience: How long to wait for `version` and the queues.

    Raises:
        VersionNotRegisteredError: `version` was not registered in time.
        VersionSupersededError: Another version is the latest. Nothing was written.
        QueuesNotDeclaredError: A declared queue was not registered in time.
        StaleScheduleDeploymentError: Another version became the latest part-way; what
            committed before stays, and nothing more is written.
        ScheduleAuthorityUnavailableError: Another writer held the lock too long.
    """
    patience = patience or Patience()
    deadline = patience.clock() + patience.seconds
    await_version(client, version, deadline=deadline, patience=patience)
    queues = {queue for entry in declared if (queue := entry.get("queue_name"))}
    await_queues(client, queues, deadline=deadline, patience=patience)

    names = tuple(entry["schedule_name"] for entry in declared)
    with fence.armed():
        client.apply_schedules(list(declared))
        logger.info("schedules.applied", version=version, schedules=list(names))
        retired, unclaimed = _undeclared(
            client.list_schedules(application_name=application), application, frozenset(names)
        )
        for stored in retired:
            client.delete_schedule(stored.name)
            logger.info("schedules.deleted", schedule=stored.name, workflow=stored.workflow)
    if unclaimed:
        logger.warning(
            "schedules.unclaimed_left",
            schedules=[stored.name for stored in unclaimed],
        )
    return ScheduleReconciliation(applied=names, deleted=retired, unclaimed_left=unclaimed)


def promote(
    client: ScheduleClient,
    fence: Fence,
    *,
    application: str,
    version: str,
    declared: Sequence[ClientScheduleInput],
    patience: Patience | None = None,
) -> ScheduleReconciliation:
    """Make `version` the latest, then reconcile as it: the rollback path.

    DBOS keeps a relaunched version's original timestamp, so workers rolled back to an
    older version do not make it the latest again; this does.

    Raises:
        VersionNotRegisteredError: `version` was never registered within the wait.
        As `reconcile` raises otherwise.
    """
    patience = patience or Patience()
    deadline = patience.clock() + patience.seconds
    await_version(client, version, deadline=deadline, patience=patience, latest=False)
    client.set_latest_application_version(version)
    logger.info("schedules.promoted", version=version)
    return reconcile(
        client,
        fence,
        application=application,
        version=version,
        declared=declared,
        patience=patience,
    )


def _definition(entry: ClientScheduleInput | WorkflowSchedule) -> dict[str, object]:
    """Return the fields of a declaration or stored row that reconciliation writes."""
    source: Mapping[str, object] = entry
    return {key: source.get(key, default) for key, default in _DEFINITION_DEFAULTS.items()}


def plan(
    client: ScheduleClient, *, application: str, declared: Sequence[ClientScheduleInput]
) -> SchedulePlan:
    """Compare `declared` with the stored schedules, writing nothing."""
    versions = _version_names(client)
    rows = client.list_schedules(application_name=application)
    stored = {row["schedule_name"]: row for row in rows}
    created: list[str] = []
    updated: list[str] = []
    unchanged: list[str] = []
    for entry in declared:
        name = entry["schedule_name"]
        row = stored.get(name)
        if row is None:
            created.append(name)
        elif row["application_name"] is None or _definition(row) != _definition(entry):
            # An unclaimed row is claimed by the apply, which is a write too.
            updated.append(name)
        else:
            unchanged.append(name)
    retired, unclaimed = _undeclared(
        list(stored.values()), application, frozenset(entry["schedule_name"] for entry in declared)
    )
    return SchedulePlan(
        latest_version=versions[0] if versions else None,
        created=tuple(created),
        updated=tuple(updated),
        unchanged=tuple(unchanged),
        retired=retired,
        unclaimed=unclaimed,
    )
