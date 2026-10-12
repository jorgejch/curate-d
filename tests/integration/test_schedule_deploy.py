"""Deployment-time schedule reconciliation against PostgreSQL and DBOS's own tables.

What reconciliation decides to write is unit-tested against an in-memory client. What
only a real server can show is that DBOS's client keeps a row's id, pause status and
firing history across an apply, that the fence runs inside the client's own
transactions, that a stale command cannot commit after a newer version registers, that a
connection lost part-way leaves a state a re-run converges from, and that a launching
worker no longer touches the rows at all.

Each test acts as an application of its own (`curate-d-test-<id>`), with its own
versions, queue and schedules, so it neither reads nor deletes the rows of the
application the local stack runs, and no launched DBOS fires its schedules.
"""

import threading
import time
from collections.abc import Callable, Iterator
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dbos import DBOS, ClientScheduleInput, DBOSClient, WorkflowSchedule
from sqlalchemy import event

from curate_d.config import Settings
from curate_d.runtime import dbos_app
from curate_d.runtime.dbos_app import SYSTEM_SCHEMA, application_name, system_database_url
from curate_d.runtime.profiles import WorkerProfile
from curate_d.runtime.schedule_fence import (
    ScheduleAuthorityUnavailableError,
    ScheduleFence,
    StaleScheduleDeploymentError,
    fenced_client,
    fenced_engine,
    schedule_authority_key,
)
from curate_d.schedules.deployment import (
    Patience,
    ScheduleReconciliation,
    VersionSupersededError,
    promote,
    reconcile,
)
from curate_d.worker import main as worker_main

pytestmark = pytest.mark.integration

_SETTLE_SECONDS = 10.0
# Fires on 29 February only, so no scheduler picks a test row up while a test runs.
_LEAP_DAY = "0 0 29 2 *"
_LEAP_DAY_NOON = "0 12 29 2 *"


class _App:
    """One test's application: its name, queue and the versions it registers."""

    def __init__(self, engine: sa.Engine) -> None:
        self.engine = engine
        self.name = f"curate-d-test-{uuid4().hex[:8]}"
        self.queue = f"{self.name}-queue"
        self.prefix = f"{self.name}-"

    def register_version(self) -> str:
        """Register a version newer than every other, as a rollout's first worker does."""
        version = f"9.9.9+{self.name}.{uuid4().hex[:6]}"
        with self.engine.begin() as connection:
            connection.execute(
                sa.text(
                    f"INSERT INTO {SYSTEM_SCHEMA}.application_versions"
                    " (version_id, version_name, version_timestamp, application_name)"
                    " SELECT :id, :name,"
                    "  GREATEST(COALESCE(max(version_timestamp), 0) + 1,"
                    "           CAST(extract(epoch FROM now()) * 1000 AS bigint)), :app"
                    f" FROM {SYSTEM_SCHEMA}.application_versions"
                ),
                {"id": str(uuid4()), "name": version, "app": self.name},
            )
        return version

    def entry(
        self,
        name: str,
        cron: str = _LEAP_DAY,
        *,
        context: object = None,
        cron_timezone: str | None = None,
        automatic_backfill: bool = False,
    ) -> ClientScheduleInput:
        """Return a declaration of `name` (prefixed) on this application's queue."""
        return ClientScheduleInput(
            schedule_name=f"{self.prefix}{name}",
            workflow_name="prune_history",
            schedule=cron,
            context=context,
            cron_timezone=cron_timezone,
            automatic_backfill=automatic_backfill,
            queue_name=self.queue,
        )

    def stored(self, client: DBOSClient) -> dict[str, WorkflowSchedule]:
        """Return the rows under this application's prefix, whoever owns them."""
        return {
            row["schedule_name"].removeprefix(self.prefix): row
            for row in client.list_schedules(application_name=[])
            if row["schedule_name"].startswith(self.prefix)
        }

    def cleanup(self) -> None:
        """Delete every row this application or its prefix left in DBOS's tables."""
        with self.engine.begin() as connection:
            for table, column in (
                ("workflow_schedules", "schedule_name"),
                ("queues", "name"),
            ):
                connection.execute(
                    sa.text(
                        f"DELETE FROM {SYSTEM_SCHEMA}.{table}"
                        f" WHERE {column} LIKE :prefix OR application_name = :app"
                    ),
                    {"prefix": f"{self.prefix}%", "app": self.name},
                )
            connection.execute(
                sa.text(
                    f"DELETE FROM {SYSTEM_SCHEMA}.application_versions"
                    " WHERE application_name = :app"
                ),
                {"app": self.name},
            )


@pytest.fixture
def system_engine(settings: Settings) -> Iterator[sa.Engine]:
    """Yield a plain engine on the DBOS system database."""
    engine = sa.create_engine(system_database_url(settings))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def observer(settings: Settings) -> Iterator[DBOSClient]:
    """Yield an unfenced client acting for no application: it sees every row."""
    client = DBOSClient(
        system_database_url=system_database_url(settings), dbos_system_schema=SYSTEM_SCHEMA
    )
    try:
        yield client
    finally:
        client.destroy()


@pytest.fixture
def app(settings: Settings, system_engine: sa.Engine) -> Iterator[_App]:
    """Yield a fresh application with its queue registered; remove all of it afterwards."""
    created = _App(system_engine)
    client = DBOSClient(
        system_database_url=system_database_url(settings),
        dbos_system_schema=SYSTEM_SCHEMA,
        application_name=created.name,
    )
    try:
        client.register_queue(created.queue)
        yield created
    finally:
        client.destroy()
        created.cleanup()


def _fence(app: _App, version: str, timeout_seconds: float = _SETTLE_SECONDS) -> ScheduleFence:
    """Return the fence a command of `version` runs behind."""
    return ScheduleFence(application=app.name, version=version, timeout_seconds=timeout_seconds)


def _patience() -> Patience:
    """Return a short wait: every test registers its version and queue up front."""
    return Patience(seconds=2, poll_seconds=0.1)


def _deploy(
    settings: Settings,
    app: _App,
    version: str,
    declared: list[ClientScheduleInput],
    *,
    timeout_seconds: float = _SETTLE_SECONDS,
) -> ScheduleReconciliation:
    """Run one reconcile as a deployment command of `version`."""
    fence = _fence(app, version, timeout_seconds)
    with fenced_client(settings, fence) as client:
        return reconcile(
            client,
            fence,
            application=app.name,
            version=version,
            declared=declared,
            patience=_patience(),
        )


def test_reconcile_stores_rows_exactly_as_declared(
    settings: Settings, app: _App, observer: DBOSClient
) -> None:
    """Cron, timezone, context, queue and backfill land as declared, owned by the app."""
    version = app.register_version()
    declared = [
        app.entry(
            "full",
            cron=_LEAP_DAY_NOON,
            cron_timezone="Europe/Lisbon",
            context={"scope": "everything"},
            automatic_backfill=True,
        ),
        app.entry("plain"),
    ]
    outcome = _deploy(settings, app, version, declared)

    assert outcome.applied == (f"{app.prefix}full", f"{app.prefix}plain")
    stored = app.stored(observer)
    full = stored["full"]
    assert (
        full["workflow_name"],
        full["schedule"],
        full["cron_timezone"],
        full["context"],
        full["queue_name"],
        full["automatic_backfill"],
        full["application_name"],
        full["status"],
    ) == (
        "prune_history",
        _LEAP_DAY_NOON,
        "Europe/Lisbon",
        {"scope": "everything"},
        app.queue,
        True,
        app.name,
        "ACTIVE",
    )
    assert (stored["plain"]["cron_timezone"], stored["plain"]["automatic_backfill"]) == (
        None,
        False,
    )


def test_a_second_run_keeps_id_pause_and_firing_history(
    settings: Settings, app: _App, observer: DBOSClient, system_engine: sa.Engine
) -> None:
    """An operator's pause and the last tick survive a redeploy that changes the cron."""
    version = app.register_version()
    _deploy(settings, app, version, [app.entry("kept")])
    name = f"{app.prefix}kept"
    observer.pause_schedule(name)
    with system_engine.begin() as connection:
        connection.execute(
            sa.text(
                f"UPDATE {SYSTEM_SCHEMA}.workflow_schedules"
                " SET last_fired_at = '2026-02-28T00:00:00+00:00' WHERE schedule_name = :name"
            ),
            {"name": name},
        )
    before = app.stored(observer)["kept"]

    outcome = _deploy(settings, app, version, [app.entry("kept", cron=_LEAP_DAY_NOON)])

    after = app.stored(observer)["kept"]
    assert outcome.deleted == ()
    assert after["schedule_id"] == before["schedule_id"]
    assert after["status"] == "PAUSED"
    assert after["last_fired_at"] == before["last_fired_at"] is not None
    assert after["schedule"] == _LEAP_DAY_NOON


def test_retires_only_this_applications_undeclared_rows(
    settings: Settings, app: _App, observer: DBOSClient
) -> None:
    """Another application's row and an unclaimed row survive; a retired one goes."""
    version = app.register_version()
    _deploy(settings, app, version, [app.entry("kept"), app.entry("retired")])
    other_app = f"{app.name}-other"
    for name, owner in (("another-apps", other_app), ("unclaimed", None)):
        observer.create_schedule(
            schedule_name=f"{app.prefix}{name}",
            workflow_name="prune_history",
            schedule=_LEAP_DAY,
            application_name=owner,
        )

    outcome = _deploy(settings, app, version, [app.entry("kept")])

    assert [stored.name for stored in outcome.deleted] == [f"{app.prefix}retired"]
    assert [stored.name for stored in outcome.unclaimed_left] == [f"{app.prefix}unclaimed"]
    stored = app.stored(observer)
    assert set(stored) == {"kept", "another-apps", "unclaimed"}
    assert stored["another-apps"]["application_name"] == other_app
    assert stored["unclaimed"]["application_name"] is None


def _terminate_once_after_apply(engine: sa.Engine, fence: ScheduleFence) -> list[bool]:
    """Kill the backend of the first armed transaction after one has committed."""
    state = {"committed": False}
    killed: list[bool] = []

    def on_commit(_connection: sa.Connection) -> None:
        if fence.is_armed:
            state["committed"] = True

    def on_begin(connection: sa.Connection) -> None:
        if fence.is_armed and state["committed"] and not killed:
            killed.append(True)
            connection.exec_driver_sql("SELECT pg_terminate_backend(pg_backend_pid())")

    event.listen(engine, "commit", on_commit)
    event.listen(engine, "begin", on_begin)
    return killed


def test_a_connection_lost_after_the_apply_converges_on_a_rerun(
    settings: Settings, app: _App, observer: DBOSClient
) -> None:
    """The apply stays committed, the retirements wait for the re-run, which completes."""
    version = app.register_version()
    _deploy(settings, app, version, [app.entry("changed"), app.entry("retired")])
    declared = [app.entry("changed", cron=_LEAP_DAY_NOON), app.entry("added")]

    fence = _fence(app, version)
    engine = fenced_engine(settings, fence)
    killed = _terminate_once_after_apply(engine, fence)
    client = DBOSClient(
        system_database_engine=engine,
        dbos_system_schema=SYSTEM_SCHEMA,
        application_name=app.name,
        retry_connection_errors=False,
    )
    try:
        with pytest.raises(sa.exc.OperationalError):
            reconcile(
                client,
                fence,
                application=app.name,
                version=version,
                declared=declared,
                patience=_patience(),
            )
    finally:
        client.destroy()
        engine.dispose()
    assert killed == [True]
    midway = app.stored(observer)
    assert set(midway) == {"changed", "added", "retired"}
    assert midway["changed"]["schedule"] == _LEAP_DAY_NOON

    outcome = _deploy(settings, app, version, declared)

    assert [stored.name for stored in outcome.deleted] == [f"{app.prefix}retired"]
    assert set(app.stored(observer)) == {"changed", "added"}


def test_a_stale_command_cannot_write_after_a_newer_version_registers(
    settings: Settings, app: _App, observer: DBOSClient
) -> None:
    """The older command holding the fence goes first; the newer one's writes land after.

    The older command passes its check and stalls inside its apply transaction, holding
    the lock. The newer version registers and its command waits on the lock. The older
    apply then commits, its next transaction fails the check, and the newer command
    applies and retires over it.
    """
    older = app.register_version()
    _deploy(settings, app, older, [app.entry("changed"), app.entry("kept")])
    older_declares = [app.entry("changed"), app.entry("kept"), app.entry("older-only")]
    newer_declares = [app.entry("changed", cron=_LEAP_DAY_NOON), app.entry("newer-only")]

    held, release = threading.Event(), threading.Event()
    older_failure: list[BaseException] = []
    older_fence = _fence(app, older)

    def stall_in_the_first_armed_transaction(_connection: sa.Connection) -> None:
        if older_fence.is_armed and not held.is_set():
            held.set()
            assert release.wait(_SETTLE_SECONDS)

    def run_older() -> None:
        engine = fenced_engine(settings, older_fence)
        event.listen(engine, "begin", stall_in_the_first_armed_transaction)
        client = DBOSClient(
            system_database_engine=engine,
            dbos_system_schema=SYSTEM_SCHEMA,
            application_name=app.name,
            retry_connection_errors=False,
        )
        try:
            reconcile(
                client,
                older_fence,
                application=app.name,
                version=older,
                declared=older_declares,
                patience=_patience(),
            )
        except BaseException as failure:
            older_failure.append(failure)
        finally:
            client.destroy()
            engine.dispose()

    newer_outcome: list[ScheduleReconciliation] = []
    older_thread = threading.Thread(target=run_older)
    older_thread.start()
    newer_thread: threading.Thread | None = None
    try:
        assert held.wait(_SETTLE_SECONDS)
        newer = app.register_version()
        newer_thread = threading.Thread(
            target=lambda: newer_outcome.append(_deploy(settings, app, newer, newer_declares))
        )
        newer_thread.start()
        newer_thread.join(0.5)
        assert newer_thread.is_alive(), "the newer command wrote while the older held the fence"
    finally:
        release.set()
        older_thread.join(_SETTLE_SECONDS)
        if newer_thread is not None:
            newer_thread.join(_SETTLE_SECONDS)

    assert [type(failure) for failure in older_failure] == [StaleScheduleDeploymentError]
    assert newer_outcome, "the newer command did not finish"
    stored = app.stored(observer)
    assert set(stored) == {"changed", "newer-only"}
    assert stored["changed"]["schedule"] == _LEAP_DAY_NOON

    # Started after the newer version registered: refused before and at the first write.
    with pytest.raises(VersionSupersededError):
        _deploy(settings, app, older, older_declares)
    fence = _fence(app, older)
    with fenced_client(settings, fence) as client, fence.armed():
        with pytest.raises(StaleScheduleDeploymentError):
            client.apply_schedules(older_declares)
        with pytest.raises(StaleScheduleDeploymentError):
            client.delete_schedule(f"{app.prefix}newer-only")
    assert set(app.stored(observer)) == {"changed", "newer-only"}


def test_competing_commands_of_one_version_both_succeed(
    settings: Settings, app: _App, observer: DBOSClient
) -> None:
    """A retried deploy job racing its first attempt: same end state, no failure."""
    version = app.register_version()
    _deploy(settings, app, version, [app.entry("kept"), app.entry("retired")])
    declared = [app.entry("kept", cron=_LEAP_DAY_NOON), app.entry("added")]
    outcomes: list[ScheduleReconciliation] = []
    failures: list[BaseException] = []
    start = threading.Barrier(2)

    def run() -> None:
        try:
            start.wait(_SETTLE_SECONDS)
            outcomes.append(_deploy(settings, app, version, declared))
        except BaseException as failure:
            failures.append(failure)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(_SETTLE_SECONDS)

    assert failures == []
    assert len(outcomes) == 2
    stored = app.stored(observer)
    assert set(stored) == {"kept", "added"}
    assert stored["kept"]["schedule"] == _LEAP_DAY_NOON


def test_a_rollback_promotes_the_older_version_and_restores_its_schedules(
    settings: Settings, app: _App, observer: DBOSClient
) -> None:
    """After `promote`, the older set is back and the newer version can no longer write."""
    older = app.register_version()
    older_declares = [app.entry("shared"), app.entry("older-only")]
    _deploy(settings, app, older, older_declares)
    newer = app.register_version()
    newer_declares = [app.entry("shared", cron=_LEAP_DAY_NOON), app.entry("newer-only")]
    _deploy(settings, app, newer, newer_declares)
    assert set(app.stored(observer)) == {"shared", "newer-only"}

    fence = _fence(app, older)
    with fenced_client(settings, fence) as client:
        outcome = promote(
            client,
            fence,
            application=app.name,
            version=older,
            declared=older_declares,
            patience=_patience(),
        )
        assert client.get_latest_application_version()["version_name"] == older

    assert [stored.name for stored in outcome.deleted] == [f"{app.prefix}newer-only"]
    stored = app.stored(observer)
    assert set(stored) == {"shared", "older-only"}
    assert stored["shared"]["schedule"] == _LEAP_DAY
    with pytest.raises(VersionSupersededError):
        _deploy(settings, app, newer, newer_declares)
    newer_fence = _fence(app, newer)
    with (
        fenced_client(settings, newer_fence) as client,
        newer_fence.armed(),
        pytest.raises(StaleScheduleDeploymentError),
    ):
        client.apply_schedules(newer_declares)


def test_a_held_authority_times_out_naming_its_holder(
    settings: Settings, app: _App, system_engine: sa.Engine
) -> None:
    """A writer stuck holding the lock fails the command instead of stalling the deploy."""
    version = app.register_version()
    with system_engine.connect() as holder:
        holder.execute(
            sa.text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))"),
            {"key": schedule_authority_key()},
        )
        started = time.monotonic()
        with pytest.raises(ScheduleAuthorityUnavailableError, match=r"over 0\.3s: pid \d+"):
            _deploy(settings, app, version, [app.entry("kept")], timeout_seconds=0.3)
        assert time.monotonic() - started < _SETTLE_SECONDS
        holder.rollback()


def test_the_fence_reads_dbos_version_table_as_dbos_does(
    settings: Settings, app: _App, system_engine: sa.Engine
) -> None:
    """Pins the columns the fence reads, and that its latest version is DBOS's."""
    with system_engine.connect() as connection:
        columns: set[str] = set(
            connection.execute(
                sa.text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_schema = :schema AND table_name = 'application_versions'"
                ),
                {"schema": SYSTEM_SCHEMA},
            ).scalars()
        )
    assert {"version_name", "version_timestamp", "application_name"} <= columns

    app.register_version()
    latest = app.register_version()
    fence = _fence(app, latest)
    with fenced_client(settings, fence) as client:
        assert client.get_latest_application_version()["version_name"] == latest
        with fence.armed():
            assert client.list_schedules(application_name=app.name) == []


@pytest.fixture
def launched_worker(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[[], None]]:
    """Yield a function that launches a real I/O worker without services or Conductor."""
    monkeypatch.setattr(worker_main, "prepare_services", lambda settings, profile, staging: None)
    monkeypatch.setattr(worker_main, "start_conductor", lambda settings, profile: None)
    monkeypatch.setattr(
        worker_main, "resolve_executor_id", lambda: f"schedule-deploy-{uuid4().hex[:8]}"
    )
    DBOS.destroy()

    def launch() -> None:
        worker_main.run(settings, WorkerProfile.IO, block=False)

    try:
        yield launch
    finally:
        DBOS.destroy()


def test_a_launching_worker_leaves_every_schedule_untouched(
    settings: Settings,
    app: _App,
    system_engine: sa.Engine,
    observer: DBOSClient,
    launched_worker: Callable[[], None],
) -> None:
    """Restarting or scaling workers changes no row: neither applies nor retires."""
    # Undeclared and owned by the worker's own application: a worker that still
    # reconciled would delete it.
    own = application_name(settings.environment)
    observer.create_schedule(
        schedule_name=f"{app.prefix}undeclared",
        workflow_name="prune_history",
        schedule=_LEAP_DAY,
        application_name=own,
    )
    query = sa.text(
        f"SELECT schedule_id, schedule_name, schedule, status, last_fired_at, application_name"
        f" FROM {SYSTEM_SCHEMA}.workflow_schedules ORDER BY schedule_name"
    )
    with system_engine.connect() as connection:
        before = connection.execute(query).all()

    launched_worker()
    assert DBOS.application_version == dbos_app.APPLICATION_VERSION

    with system_engine.connect() as connection:
        after = connection.execute(query).all()
    assert after == before
    assert f"{app.prefix}undeclared" in {row.schedule_name for row in after}
