"""Worker startup.

The ordering here is load-bearing and invisible when wrong: a process that registers
queues before launching, or listens after launching, still starts and still logs
"ready". It just briefly drains queues it has no sidecar or thread budget for, or
overwrites a limit the control plane set. DBOS is substituted at the boundary so the
sequence itself is what gets asserted.
"""

import os
import threading
from unittest.mock import Mock

import pytest
from dbos import DBOS
from dbos._dbos import _get_dbos_instance

from curate_d.config import Settings
from curate_d.runtime import dbos_app
from curate_d.runtime.dbos_app import configure
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
        lambda settings, executor_id, profile: recorded.append(
            ("configure", (executor_id, profile))
        ),
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
    monkeypatch.setattr(
        worker_main,
        "start_conductor",
        lambda settings, profile: recorded.append(("start_conductor", profile)),
    )
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
    """Take the lease, create services, configure, listen, launch, register, schedule, report."""
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
        "start_conductor",
    ]
    assert ("start_conductor", profile) in calls


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
    assert ("configure", ("worker-io-0", WorkerProfile.IO)) in calls


@pytest.mark.parametrize("profile", list(WorkerProfile))
def test_the_profile_is_passed_to_dbos(
    calls: list[tuple[str, object]], profile: WorkerProfile
) -> None:
    """Conductor lists each executor with its profile, so DBOS must be configured with it."""
    worker_main.run(Settings(), profile, block=False)
    assert ("configure", ("worker-io-0", profile)) in calls


def test_a_blocking_worker_stops_when_signalled(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch
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
    # DBOS's destroy stops the Conductor thread start_conductor hands it.
    assert [name for name, _ in calls][-1] == "start_conductor"


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
    assert "start_conductor" not in [name for name, _ in calls]


def test_a_failed_launch_never_reports_to_conductor(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Conductor speaks to a launched executor; a failed launch opens no connection."""

    def fail() -> None:
        raise RuntimeError("launch failed")

    monkeypatch.setattr(DBOS, "launch", fail)
    with pytest.raises(RuntimeError, match="launch failed"):
        worker_main.run(Settings(staging_s3_endpoint_url=None), WorkerProfile.IO, block=False)
    assert "start_conductor" not in [name for name, _ in calls]


def test_a_clean_shutdown_releases_the_lease_after_the_drain(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
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
    monkeypatch.setattr(worker_main, "_drain_dbos", lambda timeout_seconds: False)
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

    def drain(timeout_seconds: int) -> bool:
        drains.append(timeout_seconds)
        return True

    monkeypatch.setattr(worker_main, "_drain_dbos", drain)
    monkeypatch.setattr(worker_main, "_install_shutdown_handler", lambda stop: None)
    locks.fail_probes = True
    with pytest.raises(ExecutorLeaseLostError):
        worker_main.run(
            Settings(staging_s3_endpoint_url=None),
            WorkerProfile.IO,
            stop=threading.Event(),
            lease_factory=locks.factory,
        )
    assert drains == [0]


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


def test_the_drain_reports_workflows_left_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins DBOS internals: ``_active_workflows_set.activeList()`` on the instance."""
    DBOS.destroy()
    monkeypatch.setenv(EXECUTOR_ID_ENV, "drain-pin")
    configure(Settings())
    _get_dbos_instance()._active_workflows_set.add("still-running")
    assert worker_main._drain_dbos(0) is False
    configure(Settings())
    assert worker_main._drain_dbos(0) is True
    assert worker_main._drain_dbos(0) is True, "no instance at all means nothing is running"


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
    monkeypatch.setattr(worker_main, "_drain_dbos", lambda timeout_seconds: False)
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


@pytest.mark.parametrize("profile", list(WorkerProfile))
def test_both_worker_profiles_lease_the_registered_build_version(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch, profile: WorkerProfile
) -> None:
    """The version registered before startup is the same one held throughout execution."""
    monkeypatch.setattr(dbos_app, "APPLICATION_VERSION", "build-under-test")
    worker_main.run(Settings(), profile, block=False)
    assert ("register_version", "build-under-test") in calls
    assert ("lease", ("worker-io-0", "build-under-test")) in calls


def test_startup_failure_releases_both_worker_locks(
    calls: list[tuple[str, object]], monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
) -> None:
    """A service preparation failure must not leave the unlaunched version looking live."""
    error = RuntimeError("service preparation failed")

    def fail(settings: Settings, profile: WorkerProfile, staging: object) -> None:
        """Fail after both worker locks have been acquired."""
        assert locks.holders
        assert any(locks.shared.values())
        raise error

    monkeypatch.setattr(worker_main, "prepare_services", fail)
    with pytest.raises(RuntimeError) as raised:
        worker_main.run(
            Settings(staging_s3_endpoint_url=None),
            WorkerProfile.IO,
            block=False,
            lease_factory=locks.factory,
        )
    assert raised.value is error
    assert "launch" not in [name for name, _ in calls]
    assert locks.holders == {}
    assert not any(locks.shared.values())
    assert all(session.closed for session in locks.sessions)


@pytest.mark.parametrize("profile", list(WorkerProfile))
def test_registration_failure_stops_prepared_staging(
    calls: list[tuple[str, object]],
    monkeypatch: pytest.MonkeyPatch,
    locks: FakeLockServer,
    profile: WorkerProfile,
) -> None:
    """Neither worker profile leaks its staging supervisor when version registration fails."""
    staging = Mock(spec=worker_main.StagingLifecycle)
    monkeypatch.setattr(worker_main, "_start_staging", lambda *args: staging)
    error = VersionRegistryError("registration unavailable")
    monkeypatch.setattr(worker_main, "register_version", Mock(side_effect=error))

    with pytest.raises(VersionRegistryError) as raised:
        worker_main.run(Settings(), profile, block=False, lease_factory=locks.factory)
    assert raised.value is error
    staging.stop.assert_called_once_with()
    assert locks.sessions == []
    assert calls == []
