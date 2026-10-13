"""Worker startup.

The ordering here is load-bearing and invisible when wrong: a process that registers
queues before launching, or listens after launching, still starts and still logs
"ready". It just briefly drains queues it has no sidecar or thread budget for, or
overwrites a limit the control plane set. DBOS is substituted at the boundary so the
sequence itself is what gets asserted.
"""

import os
import threading

import pytest
from dbos import DBOS
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from curate_d.config import Settings
from curate_d.runtime import dbos_app
from curate_d.runtime.identity import EXECUTOR_ID_ENV, POD_NAME_ENV, ExecutorIdentityError
from curate_d.runtime.lease import (
    ExecutorLease,
    ExecutorLeaseLostError,
    ExecutorLeaseUnavailableError,
)
from curate_d.runtime.profiles import WorkerProfile
from curate_d.runtime.version_lease import VersionRegistryError
from curate_d.worker import main as worker_main
from tests.unit.runtime.fake_lease import FakeLockServer


@pytest.fixture(autouse=True)
def _registrations(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record version registrations instead of writing them to a database."""
    registered: list[str] = []
    monkeypatch.setattr(
        worker_main, "register_version", lambda settings, version: registered.append(version)
    )
    return registered


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    """Record the startup sequence instead of performing it.

    Returns:
        The list that each patched call appends its `(name, argument)` entry to, in
        call order.
    """
    recorded: list[tuple[str, object]] = []

    monkeypatch.setattr(
        worker_main,
        "configure",
        lambda settings, executor_id: recorded.append(("configure", executor_id)),
    )
    monkeypatch.setattr(
        DBOS,
        "listen_queues",
        lambda queues: recorded.append(("listen_queues", tuple(queues))),
    )
    monkeypatch.setattr(DBOS, "launch", lambda: recorded.append(("launch", None)))
    monkeypatch.setattr(
        worker_main, "register_all", lambda: recorded.append(("register_all", None))
    )
    monkeypatch.setattr(
        worker_main,
        "prepare_services",
        lambda settings, profile, staging: recorded.append(("prepare_services", profile)),
    )
    monkeypatch.setattr(
        worker_main,
        "apply_schedules",
        lambda profile, settings: recorded.append(("apply_schedules", profile)),
    )

    class _Reconciler:
        """Record when the worker stops waiting for its schedule reconciler."""

        def __init__(self, stop: threading.Event) -> None:
            self._stop = stop

        def join(self, timeout: float) -> bool:
            """Record the join and whether the worker's stop event was set first."""
            recorded.append(("reconciler_joined", self._stop.is_set()))
            return True

    def start_reconciler(
        profile: WorkerProfile, settings: Settings, *, stop: threading.Event
    ) -> _Reconciler:
        """Record the start and return a reconciler that records its join."""
        recorded.append(("start_schedule_reconciler", profile))
        return _Reconciler(stop)

    monkeypatch.setattr(worker_main, "start_schedule_reconciler", start_reconciler)
    monkeypatch.setattr(worker_main, "resolve_executor_id", lambda: "worker-io-0")
    monkeypatch.setattr(
        worker_main,
        "register_version",
        lambda settings, version: recorded.append(("register_version", version)),
    )
    server = FakeLockServer()

    def lease_for(
        settings: Settings, executor_id: str, *, version: str | None = None
    ) -> ExecutorLease:
        recorded.append(("lease", (executor_id, version)))
        return server.lease(executor_id, version=version)

    monkeypatch.setattr(ExecutorLease, "for_settings", lease_for)
    return recorded


@pytest.fixture
def locks() -> FakeLockServer:
    """Return a lock server whose events record the lease's life."""
    return FakeLockServer()


class _DrainClient:
    """Answer the drain's PENDING query from a fixed list, or fail as a lost database would."""

    def __init__(self) -> None:
        """Start with nothing left running and no failure."""
        self.pending: list[str] = []
        self.failure: Exception | None = None
        self.queries: list[dict[str, object]] = []
        self.retries: list[bool] = []
        self.destroyed = 0

    def list_workflows(
        self,
        *,
        status: str,
        executor_id: list[str],
        limit: int,
        load_input: bool,
        load_output: bool,
    ) -> list[str]:
        """Record the query and return up to `limit` pending rows, or raise the failure."""
        self.queries.append(
            {
                "status": status,
                "executor_id": executor_id,
                "limit": limit,
                "load_input": load_input,
                "load_output": load_output,
            }
        )
        if self.failure is not None:
            raise self.failure
        return self.pending[:limit]

    def destroy(self) -> None:
        """Count the client's teardown."""
        self.destroyed += 1


@pytest.fixture
def drain_client(monkeypatch: pytest.MonkeyPatch) -> _DrainClient:
    """Substitute the drain's system-database client with one that finds nothing running."""
    fake = _DrainClient()

    def build(settings: Settings, *, retry_connection_errors: bool = True) -> _DrainClient:
        """Return the shared fake, recording whether connection errors were to be retried."""
        fake.retries.append(retry_connection_errors)
        return fake

    monkeypatch.setattr(dbos_app, "client", build)
    return fake


def test_queues_are_restricted_before_launch(calls: list[tuple[str, object]]) -> None:
    """DBOS reads the listen set at launch; setting it after leaves a window open."""
    worker_main.run(Settings(), WorkerProfile.IO, block=False)
    names = [name for name, _ in calls]
    assert names.index("listen_queues") < names.index("launch")


def test_queues_are_registered_after_launch(calls: list[tuple[str, object]]) -> None:
    """Registration writes to the system database, which launch establishes."""
    worker_main.run(Settings(), WorkerProfile.IO, block=False)
    names = [name for name, _ in calls]
    assert names.index("launch") < names.index("register_all")


@pytest.mark.parametrize("profile", list(WorkerProfile))
def test_the_full_startup_sequence(calls: list[tuple[str, object]], profile: WorkerProfile) -> None:
    """Take the lease, create services, configure, listen, launch, register, schedule."""
    worker_main.run(Settings(), profile, block=False)
    assert [name for name, _ in calls] == [
        "register_version",
        "lease",
        "prepare_services",
        "configure",
        "listen_queues",
        "launch",
        "register_all",
        "apply_schedules",
        "start_schedule_reconciler",
        "reconciler_joined",
    ]


def test_the_reconciler_is_stopped_before_the_drain(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """It writes through DBOS, so it must be stopped and joined before DBOS is destroyed."""
    monkeypatch.setattr(
        DBOS,
        "destroy",
        lambda workflow_completion_timeout_sec: calls.append(("destroy", None)),
    )
    stop = threading.Event()
    stop.set()
    worker_main.run(Settings(), WorkerProfile.IO, stop=stop, block=True)
    names = [name for name, _ in calls]
    assert names.index("apply_schedules") < names.index("start_schedule_reconciler")
    assert names.index("reconciler_joined") < names.index("destroy")
    assert ("reconciler_joined", True) in calls, "joined without being told to stop"


def test_a_non_blocking_return_stops_the_reconciler(calls: list[tuple[str, object]]) -> None:
    """A worker returning early sets its stop event, so the reconciler thread ends too."""
    worker_main.run(Settings(), WorkerProfile.IO, block=False)
    assert calls[-1] == ("reconciler_joined", True)


@pytest.mark.parametrize("profile", list(WorkerProfile))
def test_a_worker_listens_only_to_its_own_profile_queues(
    calls: list[tuple[str, object]], profile: WorkerProfile
) -> None:
    """Restrict listen_queues to exactly the given profile's queues."""
    worker_main.run(Settings(), profile, block=False)
    listened = next(value for name, value in calls if name == "listen_queues")
    assert listened == tuple(queue.value for queue in profile.queues)


def test_the_executor_identity_is_passed_to_dbos(calls: list[tuple[str, object]]) -> None:
    """Per-worker limits are counted against it; a default would double the budget."""
    worker_main.run(Settings(), WorkerProfile.IO, block=False)
    assert ("configure", "worker-io-0") in calls


def test_a_blocking_worker_stops_when_signalled(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch, drain_client: _DrainClient
) -> None:
    """SIGTERM must drain rather than orphan workflows as PENDING on a retired version."""
    destroyed: list[int] = []
    monkeypatch.setattr(
        DBOS,
        "destroy",
        lambda workflow_completion_timeout_sec: destroyed.append(workflow_completion_timeout_sec),
    )

    stop = threading.Event()
    stop.set()
    worker_main.run(Settings(), WorkerProfile.IO, stop=stop, block=True)

    assert destroyed == [30], "in-flight workflows were not given time to finish"


def test_the_lease_is_held_before_launch_runs_startup_recovery(
    calls: list[tuple[str, object]],
) -> None:
    """Launch recovers this identity's PENDING rows; a second live holder must not."""
    worker_main.run(Settings(), WorkerProfile.IO, block=False)
    names = [name for name, _ in calls]
    assert names.index("lease") < names.index("configure") < names.index("launch")
    assert ("lease", ("worker-io-0", dbos_app.APPLICATION_VERSION)) in calls


def test_a_worker_without_an_identity_refuses_to_start(
    monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
) -> None:
    """No hostname fallback: a random pod name never recovers its own workflows."""
    monkeypatch.delenv(EXECUTOR_ID_ENV, raising=False)
    monkeypatch.delenv(POD_NAME_ENV, raising=False)
    launched: list[None] = []
    monkeypatch.setattr(DBOS, "launch", lambda: launched.append(None))
    with pytest.raises(ExecutorIdentityError):
        worker_main.run(
            Settings(staging_s3_endpoint_url=None),
            WorkerProfile.IO,
            block=False,
            lease_factory=locks.factory,
        )
    assert launched == []
    assert locks.sessions == []


def test_a_contended_identity_never_launches(
    calls: list[tuple[str, object]], locks: FakeLockServer
) -> None:
    """Another live process holds the identity; this one must not run recovery for it."""
    assert locks.lease("worker-io-0").try_acquire()
    stop = threading.Event()
    stop.set()
    with pytest.raises(ExecutorLeaseUnavailableError):
        worker_main.run(
            Settings(staging_s3_endpoint_url=None),
            WorkerProfile.IO,
            stop=stop,
            lease_factory=locks.factory,
        )
    assert "launch" not in [name for name, _ in calls]


def test_a_clean_shutdown_releases_the_lease_after_the_drain(
    calls: list[tuple[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    locks: FakeLockServer,
    drain_client: _DrainClient,
) -> None:
    """Release last, so a successor cannot recover work still draining here."""
    monkeypatch.setattr(
        DBOS,
        "destroy",
        lambda workflow_completion_timeout_sec: locks.events.append("drain"),
    )
    stop = threading.Event()
    stop.set()
    worker_main.run(
        Settings(staging_s3_endpoint_url=None),
        WorkerProfile.IO,
        stop=stop,
        lease_factory=locks.factory,
    )
    assert locks.events == [
        "lease-acquired",
        "version-shared-acquired",
        "drain",
        "version-shared-released",
        "lease-released",
    ]


def test_an_unfinished_drain_keeps_the_lease_until_exit(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
) -> None:
    """Workflows still running at the deadline still run under this identity."""
    monkeypatch.setattr(
        worker_main, "_drain_dbos", lambda settings, executor_id, timeout_seconds: False
    )
    stop = threading.Event()
    stop.set()
    worker_main.run(
        Settings(staging_s3_endpoint_url=None),
        WorkerProfile.IO,
        stop=stop,
        lease_factory=locks.factory,
    )
    assert "lease-released" not in locks.events
    assert locks.holders, "the lease was released while workflows may still be running"


def test_losing_the_lease_stops_the_worker_without_draining(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
) -> None:
    """Fail fast: another process may already be recovering this identity's workflows."""
    drains: list[int] = []
    destroyed: list[int] = []

    def drain(settings: Settings, executor_id: str, timeout_seconds: int) -> bool:
        drains.append(timeout_seconds)
        return True

    monkeypatch.setattr(worker_main, "_drain_dbos", drain)
    monkeypatch.setattr(
        DBOS,
        "destroy",
        lambda workflow_completion_timeout_sec: destroyed.append(workflow_completion_timeout_sec),
    )
    monkeypatch.setattr(worker_main, "_install_shutdown_handler", lambda stop: None)
    locks.fail_probes = True
    with pytest.raises(ExecutorLeaseLostError):
        worker_main.run(
            Settings(staging_s3_endpoint_url=None),
            WorkerProfile.IO,
            stop=threading.Event(),
            lease_factory=locks.factory,
        )
    assert destroyed == [0], "DBOS was not shut down at once"
    assert drains == [], "the drain asks the database, the likeliest thing to have been lost"


def test_main_exits_at_once_when_the_lease_is_lost(monkeypatch: pytest.MonkeyPatch) -> None:
    """A normal exit would join DBOS's threads and keep running the lost identity's work."""
    exits: list[int] = []

    def hard_exit(status: int) -> None:
        exits.append(status)
        raise SystemExit(status)

    def lose(settings: Settings, profile: WorkerProfile) -> None:
        raise ExecutorLeaseLostError("lost")

    monkeypatch.setattr(worker_main, "configure_logging", lambda level: None)
    monkeypatch.setattr(worker_main, "get_settings", Settings)
    monkeypatch.setattr(worker_main, "resolve_profile", lambda: WorkerProfile.IO)
    monkeypatch.setattr(worker_main, "run", lose)
    monkeypatch.setattr(os, "_exit", hard_exit)
    with pytest.raises(SystemExit):
        worker_main.main()
    assert exits == [worker_main.EXIT_LEASE_LOST]


def test_the_drain_reports_workflows_left_running(
    monkeypatch: pytest.MonkeyPatch, drain_client: _DrainClient
) -> None:
    """A PENDING row under this executor, or no answer at all, is not a finished drain."""
    destroyed: list[int] = []
    monkeypatch.setattr(
        DBOS,
        "destroy",
        lambda workflow_completion_timeout_sec: destroyed.append(workflow_completion_timeout_sec),
    )
    settings = Settings()

    drain_client.pending = ["still-running"]
    assert worker_main._drain_dbos(settings, "drain-pin", 5) is False
    drain_client.pending = []
    assert worker_main._drain_dbos(settings, "drain-pin", 5) is True
    drain_client.failure = OperationalError("SELECT", {}, ConnectionError("unreachable"))
    with capture_logs() as events:
        assert worker_main._drain_dbos(settings, "drain-pin", 5) is False

    assert destroyed == [5, 5, 5], "DBOS must be shut down before the database is asked"
    assert drain_client.queries[0] == {
        "status": "PENDING",
        "executor_id": ["drain-pin"],
        "limit": 1,
        "load_input": False,
        "load_output": False,
    }
    assert drain_client.retries == [False, False, False]
    assert drain_client.destroyed == 3
    assert [event["event"] for event in events] == ["worker.drain_state_unknown"]
    assert events[0]["log_level"] == "error"


def test_the_version_is_registered_before_the_lease(
    calls: list[tuple[str, object]], _registrations: list[str]
) -> None:
    """An unregistered version is one the sweep must leave alone, so register first."""
    worker_main.run(Settings(), WorkerProfile.IO, block=False)
    names = [name for name, _ in calls]
    assert names.index("register_version") < names.index("lease") < names.index("launch")
    assert ("register_version", dbos_app.APPLICATION_VERSION) in calls


def test_a_failed_registration_never_takes_the_lease_or_launches(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
) -> None:
    """A worker that cannot register exits before its lease and DBOS."""

    def refuse(settings: Settings, version: str) -> None:
        raise VersionRegistryError(f"registering version {version!r} failed")

    monkeypatch.setattr(worker_main, "register_version", refuse)
    with pytest.raises(VersionRegistryError):
        worker_main.run(
            Settings(staging_s3_endpoint_url=None),
            WorkerProfile.IO,
            block=False,
            lease_factory=locks.factory,
        )
    assert "launch" not in [name for name, _ in calls]
    assert locks.sessions == []


def test_an_unfinished_drain_keeps_the_version_key_too(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
) -> None:
    """Workflows still running mean the version is still live; the sweep must see it."""
    monkeypatch.setattr(
        worker_main, "_drain_dbos", lambda settings, executor_id, timeout_seconds: False
    )
    stop = threading.Event()
    stop.set()
    worker_main.run(
        Settings(staging_s3_endpoint_url=None),
        WorkerProfile.IO,
        stop=stop,
        lease_factory=locks.factory,
    )
    assert "version-shared-released" not in locks.events
    assert any(locks.shared.values()), "the version key was released with workflows running"


def test_a_worker_never_promotes_its_version(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an operator promotes; a worker cannot tell a rollback from a stalled rollout."""
    promoted: list[str] = []
    monkeypatch.setattr(
        DBOS, "set_latest_application_version", lambda version, **kwargs: promoted.append(version)
    )
    worker_main.run(Settings(), WorkerProfile.IO, block=False)
    assert promoted == []


def test_startup_failure_releases_both_worker_locks(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
) -> None:
    """A startup failure before launch gives back both keys, so the version is not live."""

    def fail(settings: Settings, profile: WorkerProfile, staging: object) -> None:
        """Fail once both keys are held."""
        assert locks.holders
        assert any(locks.shared.values())
        raise RuntimeError("service preparation failed")

    monkeypatch.setattr(worker_main, "prepare_services", fail)
    with pytest.raises(RuntimeError, match="service preparation failed"):
        worker_main.run(
            Settings(staging_s3_endpoint_url=None),
            WorkerProfile.IO,
            block=False,
            lease_factory=locks.factory,
        )
    assert "launch" not in [name for name, _ in calls]
    assert locks.holders == {}
    assert not any(locks.shared.values())
    assert all(session.closed for session in locks.sessions)
