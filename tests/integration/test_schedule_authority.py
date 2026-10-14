"""The schedule authority against PostgreSQL and a launched DBOS.

What `apply_schedules` decides to write is unit-tested against an in-memory store. What
only a real server can show is that the advisory lock excludes a second session until
the first lets go, that it times out on a holder that never does, that it excludes the
transaction lock the previous build took, that a writer whose session is terminated
finds out afterwards and the newer worker's next reconciliation repairs what it wrote,
and that re-applying keeps a paused schedule paused.
"""

import threading
import time
from collections.abc import Callable, Iterator
from typing import cast
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dbos import DBOS, ScheduleInput

from curate_d.config import Settings
from curate_d.runtime.dbos_app import (
    APPLICATION_NAME,
    application_name,
    build_dbos_config,
    system_database_url,
)
from curate_d.runtime.lease import PostgresLeaseSession, connect_dedicated, lease_conninfo
from curate_d.runtime.profiles import WorkerProfile
from curate_d.runtime.schedule_authority import (
    AuthoritySession,
    ScheduleAuthority,
    ScheduleAuthorityUnavailableError,
    schedule_authority,
    schedule_authority_key,
)
from curate_d.worker import schedules as worker_schedules
from curate_d.worker.schedules import Reconciliation, reconcile_schedules
from curate_d.workflows import retention

pytestmark = pytest.mark.integration

_PREFIX = "test-authority-"
_SETTLE_SECONDS = 10.0
# Crons that do not fire while a test runs: DBOS's scheduler threads pick rows up at once.
_JAN_1, _JAN_2 = "0 0 1 1 *", "0 0 2 1 *"
_KEY = schedule_authority_key(APPLICATION_NAME)


@pytest.fixture
def system_engine(settings: Settings) -> Iterator[sa.Engine]:
    """Yield a plain engine on the DBOS system database, outside DBOS's own pool."""
    engine = sa.create_engine(system_database_url(settings))
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def dbos(settings: Settings, system_engine: sa.Engine) -> Iterator[list[str]]:
    """Launch DBOS on the suite's version, promoted to latest; yield a list of versions to drop.

    Versions a test registers, and every schedule under the test prefix, are removed
    afterwards, and the suite's version is promoted again so later tests stay latest.
    """
    DBOS.destroy()
    DBOS(config=build_dbos_config(settings, executor_id="schedule-authority-test"))
    DBOS.launch()
    DBOS.set_latest_application_version(DBOS.application_version)
    registered: list[str] = []
    try:
        yield registered
    finally:
        for stored in DBOS.list_schedules(schedule_name_prefix=_PREFIX):
            DBOS.delete_schedule(stored["schedule_name"])
        with system_engine.begin() as connection:
            for version in registered:
                connection.execute(
                    sa.text("DELETE FROM dbos.application_versions WHERE version_name = :name"),
                    {"name": version},
                )
        DBOS.set_latest_application_version(DBOS.application_version)
        DBOS.destroy()


def _register_newer_version(engine: sa.Engine, settings: Settings, registered: list[str]) -> str:
    """Register a version newer than every other, as a newer build's first worker does."""
    version = f"9.9.9-authority-{uuid4().hex[:8]}"
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO dbos.application_versions"
                " (version_id, version_name, version_timestamp, application_name)"
                " SELECT :id, :name, max(version_timestamp) + 1, :app"
                " FROM dbos.application_versions"
            ),
            {
                "id": str(uuid4()),
                "name": version,
                "app": application_name(settings.environment),
            },
        )
    registered.append(version)
    return version


def _entry(name: str, cron: str = _JAN_1) -> ScheduleInput:
    """Return a declaration of a real registered workflow on DBOS's internal queue."""
    return {"schedule_name": name, "workflow_fn": retention.prune_history, "schedule": cron}


def _stored(prefix: str) -> dict[str, str]:
    """Return the committed schedules under `prefix`, by name, with their crons."""
    return {
        stored["schedule_name"]: stored["schedule"]
        for stored in DBOS.list_schedules(schedule_name_prefix=prefix)
    }


def _everything_else_stored(*excluded: str) -> frozenset[str]:
    """Return the stored names minus `excluded`, declared too so a test deletes nothing else."""
    return frozenset(stored["schedule_name"] for stored in DBOS.list_schedules()) - set(excluded)


def test_a_second_worker_times_out_on_a_holder_that_never_lets_go(
    dbos: list[str], settings: Settings
) -> None:
    """One worker reconciles at a time, and a stuck holder fails startup naming itself."""
    with (
        schedule_authority(settings),
        pytest.raises(ScheduleAuthorityUnavailableError, match=r"over 0\.3s: pid \d+"),
        schedule_authority(settings, timeout_seconds=0.3),
    ):
        pass
    with schedule_authority(settings, timeout_seconds=1):
        pass


def test_a_waiting_worker_proceeds_once_the_holder_lets_go(
    dbos: list[str], settings: Settings
) -> None:
    """Leaving the block releases the lock for the worker queued behind it."""
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        """Hold the authority until released."""
        with schedule_authority(settings):
            held.set()
            release.wait(_SETTLE_SECONDS)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(_SETTLE_SECONDS)
    threading.Timer(0.5, release.set).start()
    started = time.monotonic()
    try:
        with schedule_authority(settings, timeout_seconds=_SETTLE_SECONDS):
            waited = time.monotonic() - started
    finally:
        release.set()
        holder.join(_SETTLE_SECONDS)
    assert waited >= 0.4


def test_the_session_lock_and_the_previous_builds_transaction_lock_exclude_each_other(
    dbos: list[str], settings: Settings, system_engine: sa.Engine
) -> None:
    """Mixed builds serialize during a rollout: either lock kind on the key keeps the other out."""
    with system_engine.begin() as connection:
        connection.execute(
            sa.text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))"), {"key": _KEY}
        )
        with (
            pytest.raises(ScheduleAuthorityUnavailableError),
            schedule_authority(settings, timeout_seconds=0.3),
        ):
            pass
    with schedule_authority(settings, timeout_seconds=1), system_engine.begin() as connection:
        taken: object = connection.execute(
            sa.text("SELECT pg_try_advisory_xact_lock(CAST(:key AS bigint))"), {"key": _KEY}
        ).scalar_one()
        assert taken is False


def test_apply_schedules_upserts_retires_and_defers_to_a_newer_version(
    dbos: list[str], settings: Settings, system_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's own entry point, end to end against DBOS's schedule rows."""
    run = f"{_PREFIX}{uuid4().hex[:8]}-"
    DBOS.apply_schedules([_entry(f"{run}retired"), _entry(f"{run}kept", _JAN_2)])
    declared = [_entry(f"{run}kept"), _entry(f"{run}added")]
    names = _everything_else_stored(f"{run}retired") | {f"{run}added"}
    monkeypatch.setattr(worker_schedules, "schedules_for", lambda profile, settings: declared)
    monkeypatch.setattr(worker_schedules, "declared_schedule_names", lambda settings: names)

    for _ in range(2):  # a restart re-applies and finds nothing left to delete
        worker_schedules.apply_schedules(WorkerProfile.IO, settings)
        assert _stored(run) == {f"{run}kept": _JAN_1, f"{run}added": _JAN_1}

    _register_newer_version(system_engine, settings, dbos)
    DBOS.apply_schedules([_entry(f"{run}from-the-newer-version")])
    worker_schedules.apply_schedules(WorkerProfile.IO, settings)
    assert _stored(run) == {
        f"{run}kept": _JAN_1,
        f"{run}added": _JAN_1,
        f"{run}from-the-newer-version": _JAN_1,
    }


def test_a_paused_schedule_stays_paused_when_reapplied(
    dbos: list[str], settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator's pause outlives every startup and periodic reconciliation."""
    name = f"{_PREFIX}{uuid4().hex[:8]}-paused"
    DBOS.apply_schedules([_entry(name)])
    DBOS.pause_schedule(name)
    names = _everything_else_stored() | {name}
    monkeypatch.setattr(
        worker_schedules, "schedules_for", lambda profile, settings: [_entry(name, _JAN_2)]
    )
    monkeypatch.setattr(worker_schedules, "declared_schedule_names", lambda settings: names)

    worker_schedules.apply_schedules(WorkerProfile.IO, settings)

    stored = DBOS.get_schedule(name)
    assert stored is not None
    assert (stored["status"], stored["schedule"]) == ("PAUSED", _JAN_2)


class _StallingAuthority:
    """An authority that stalls its worker once, right after the version check."""

    def __init__(self, authority: ScheduleAuthority, stall: Callable[[], None]) -> None:
        self._authority = authority
        self._stall = stall
        self._checked = False

    def latest_version(self) -> str:
        """Read the latest version, then stall the first time: the worker passed its check."""
        latest = self._authority.latest_version()
        if not self._checked:
            self._checked = True
            self._stall()
        return latest

    def __getattr__(self, name: str) -> object:
        """Delegate everything else to the real authority."""
        return getattr(self._authority, name)


def test_a_terminated_session_is_reported_and_repaired_by_the_newer_worker(
    dbos: list[str], settings: Settings, system_engine: sa.Engine
) -> None:
    """Detect and heal: a stale writer's rows land, it reports them, the newer worker repairs.

    The older worker takes the authority on the latest version and stalls after its check.
    Its session is terminated, which releases the lock; the newer version registers and
    reconciles. The older worker then resumes and writes, which DBOS commits on its own
    pool, and finds afterwards that it lost the authority and is no longer latest. The
    newer worker's next reconciliation restores its own declarations.
    """
    run = f"{_PREFIX}{uuid4().hex[:8]}-"
    DBOS.apply_schedules([_entry(f"{run}retired"), _entry(f"{run}changed")])
    others = _everything_else_stored(f"{run}retired", f"{run}changed")
    older_declares = [_entry(f"{run}changed", _JAN_1), _entry(f"{run}older-only")]
    newer_declares = [
        _entry(f"{run}changed", _JAN_2),
        _entry(f"{run}retired"),
        _entry(f"{run}newer-only"),
    ]
    newer_names = others | {entry["schedule_name"] for entry in newer_declares}
    newer_rows = {f"{run}changed": _JAN_2, f"{run}retired": _JAN_1, f"{run}newer-only": _JAN_1}
    stalled, resume = threading.Event(), threading.Event()
    older_pid: list[int] = []
    older_outcome: list[Reconciliation] = []

    def connect_older() -> AuthoritySession:
        """Open the older worker's session, noting its backend so the test can end it."""
        connection = connect_dedicated(lease_conninfo(settings), application_name="older")
        older_pid.append(connection.info.backend_pid)
        return PostgresLeaseSession(connection)

    def stall() -> None:
        """Hold the older worker until the test resumes it."""
        stalled.set()
        assert resume.wait(_SETTLE_SECONDS)

    def run_older() -> None:
        """Reconcile as the older worker, stalling after its check."""
        with schedule_authority(settings, connect=connect_older) as authority:
            older_outcome.append(
                reconcile_schedules(
                    cast(ScheduleAuthority, _StallingAuthority(authority, stall)),
                    version=DBOS.application_version,
                    schedules=older_declares,
                    declared=others | {f"{run}changed", f"{run}older-only"},
                )
            )

    newer_outcome: list[Reconciliation] = []

    def run_newer(version: str) -> None:
        """Reconcile as a worker of the newer version."""
        with schedule_authority(settings, timeout_seconds=_SETTLE_SECONDS) as authority:
            newer_outcome.append(
                reconcile_schedules(
                    authority, version=version, schedules=newer_declares, declared=newer_names
                )
            )

    older = threading.Thread(target=run_older)
    older.start()
    newer: threading.Thread | None = None
    try:
        assert stalled.wait(_SETTLE_SECONDS)
        newer_version = _register_newer_version(system_engine, settings, dbos)
        newer = threading.Thread(target=run_newer, args=(newer_version,))
        newer.start()
        newer.join(0.5)
        assert newer.is_alive(), "the newer worker reconciled while the older one held authority"

        with system_engine.connect() as connection:
            terminated: object = connection.execute(
                sa.text("SELECT pg_terminate_backend(:pid)"), {"pid": older_pid[0]}
            ).scalar_one()
        assert terminated is True
        newer.join(_SETTLE_SECONDS)
        assert not newer.is_alive(), "the newer worker never got the authority"
        assert newer_outcome[0].applied and newer_outcome[0].verified
        assert _stored(run) == newer_rows
    finally:
        resume.set()
        older.join(_SETTLE_SECONDS)
        if newer is not None:
            newer.join(_SETTLE_SECONDS)

    assert not older.is_alive()
    [stale] = older_outcome
    assert stale.applied, "the older worker never wrote, so nothing was left to detect"
    assert not stale.verified
    assert stale.held is False
    assert stale.latest_after == newer_version
    assert _stored(run) == {f"{run}changed": _JAN_1, f"{run}older-only": _JAN_1}

    run_newer(newer_version)  # the newer worker's next periodic reconciliation
    assert newer_outcome[1].verified
    assert _stored(run) == newer_rows
