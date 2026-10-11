"""The schedule authority against PostgreSQL and a launched DBOS.

What `apply_schedules` decides to write is unit-tested against an in-memory transaction.
What only a real server can show is that the advisory lock excludes a second transaction
until the first ends, that it times out on a holder that never lets go, and that a holder
whose backend dies takes every uncommitted write with it, so a newer worker's schedules
survive an older worker resuming afterwards.
"""

import threading
import time
from collections.abc import Callable, Iterator
from typing import cast
from uuid import uuid4

import pytest
import sqlalchemy as sa
from dbos import DBOS, ScheduleInput
from dbos._utils import GlobalParams
from sqlalchemy.exc import DBAPIError

from curate_d.config import Settings
from curate_d.runtime.dbos_app import build_dbos_config, system_database_url
from curate_d.runtime.profiles import WorkerProfile
from curate_d.runtime.schedule_authority import (
    ScheduleAuthority,
    ScheduleAuthorityUnavailableError,
    schedule_authority,
)
from curate_d.worker import main as worker_main
from curate_d.worker.main import Reconciliation, reconcile_schedules
from curate_d.workflows import retention

pytestmark = pytest.mark.integration

_PREFIX = "test-authority-"
_SETTLE_SECONDS = 10.0
# Crons that do not fire while a test runs: DBOS's scheduler threads pick rows up at once.
_JAN_1, _JAN_2 = "0 0 1 1 *", "0 0 2 1 *"


@pytest.fixture
def system_engine(settings: Settings) -> Iterator[sa.Engine]:
    """A plain engine on the DBOS system database, outside DBOS's own pool."""
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


def _register_newer_version(engine: sa.Engine, registered: list[str]) -> str:
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
            {"id": str(uuid4()), "name": version, "app": GlobalParams.app_name},
        )
    registered.append(version)
    return version


def _entry(name: str, cron: str = _JAN_1) -> ScheduleInput:
    """A declaration of a real registered workflow on DBOS's internal queue."""
    return {"schedule_name": name, "workflow_fn": retention.prune_history, "schedule": cron}


def _stored(prefix: str) -> dict[str, str]:
    """Return the committed schedules under `prefix`, by name, with their crons."""
    return {
        stored["schedule_name"]: stored["schedule"]
        for stored in DBOS.list_schedules(schedule_name_prefix=prefix)
    }


def _everything_else_stored(*excluded: str) -> frozenset[str]:
    """Names already stored, minus `excluded`: declared too, so a test deletes nothing else."""
    return frozenset(stored["schedule_name"] for stored in DBOS.list_schedules()) - set(excluded)


def test_a_second_worker_times_out_on_a_holder_that_never_lets_go(dbos: list[str]) -> None:
    """One worker reconciles at a time, and a stuck holder fails startup naming itself."""
    with (
        schedule_authority(),
        pytest.raises(ScheduleAuthorityUnavailableError, match=r"over 0\.3s: pid \d+"),
        schedule_authority(timeout_seconds=0.3),
    ):
        pass
    with schedule_authority(timeout_seconds=1):
        pass


def test_a_waiting_worker_proceeds_once_the_holder_commits(dbos: list[str]) -> None:
    """The commit releases the lock for the worker queued behind it."""
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with schedule_authority():
            held.set()
            release.wait(_SETTLE_SECONDS)

    holder = threading.Thread(target=hold)
    holder.start()
    assert held.wait(_SETTLE_SECONDS)
    threading.Timer(0.5, release.set).start()
    started = time.monotonic()
    try:
        with schedule_authority(timeout_seconds=_SETTLE_SECONDS):
            waited = time.monotonic() - started
    finally:
        release.set()
        holder.join(_SETTLE_SECONDS)
    assert waited >= 0.4


def test_apply_schedules_upserts_retires_and_defers_to_a_newer_version(
    dbos: list[str], system_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker's own entry point, end to end against DBOS's schedule rows."""
    run = f"{_PREFIX}{uuid4().hex[:8]}-"
    DBOS.apply_schedules([_entry(f"{run}retired"), _entry(f"{run}kept", _JAN_2)])
    declared = [_entry(f"{run}kept"), _entry(f"{run}added")]
    names = _everything_else_stored(f"{run}retired") | {f"{run}added"}
    monkeypatch.setattr(worker_main, "schedules_for", lambda profile, settings: declared)
    monkeypatch.setattr(worker_main, "declared_schedule_names", lambda settings: names)

    for _ in range(2):  # a restart re-applies and finds nothing left to delete
        worker_main.apply_schedules(WorkerProfile.IO, Settings())
        assert _stored(run) == {f"{run}kept": _JAN_1, f"{run}added": _JAN_1}

    _register_newer_version(system_engine, dbos)
    DBOS.apply_schedules([_entry(f"{run}from-the-newer-version")])
    worker_main.apply_schedules(WorkerProfile.IO, Settings())
    assert _stored(run) == {
        f"{run}kept": _JAN_1,
        f"{run}added": _JAN_1,
        f"{run}from-the-newer-version": _JAN_1,
    }


class _StallingAuthority:
    """An authority that stalls its worker at one point of the reconciliation."""

    def __init__(self, authority: ScheduleAuthority, stall: Callable[[], None]) -> None:
        self._authority = authority
        self._stall = stall

    def latest_version(self) -> str:
        """Read the latest version, then stall: the worker has passed its check."""
        latest = self._authority.latest_version()
        self._stall()
        return latest

    def __getattr__(self, name: str) -> object:
        return getattr(self._authority, name)


@pytest.mark.parametrize("lost_at", ["after the check", "before the commit"])
def test_an_older_worker_that_loses_its_backend_commits_nothing_over_a_newer_one(
    dbos: list[str], system_engine: sa.Engine, lost_at: str
) -> None:
    """The reviewer's interleaving: the newer version's schedules survive the older resuming.

    The older worker takes the authority on the latest version and stalls, either right
    after its version check or with its upserts and deletions pending. Its backend is
    terminated, which releases the lock; the newer version registers and reconciles; the
    older worker then resumes and must fail without having committed anything.
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
    stalled, resume = threading.Event(), threading.Event()
    older_pid: list[int] = []
    older_outcome: list[Reconciliation] = []
    older_failure: list[BaseException] = []

    def stall() -> None:
        stalled.set()
        assert resume.wait(_SETTLE_SECONDS)

    def run_older() -> None:
        try:
            with schedule_authority() as authority:
                older_pid.append(
                    authority.connection.execute(sa.text("SELECT pg_backend_pid()")).scalar_one()
                )
                reconciling = (
                    cast(ScheduleAuthority, _StallingAuthority(authority, stall))
                    if lost_at == "after the check"
                    else authority
                )
                older_outcome.append(
                    reconcile_schedules(
                        reconciling,
                        version=DBOS.application_version,
                        schedules=older_declares,
                        declared=others | {f"{run}changed", f"{run}older-only"},
                    )
                )
                if lost_at == "before the commit":
                    stall()
        except DBAPIError as failure:
            older_failure.append(failure)

    newer_outcome: list[Reconciliation] = []

    def run_newer(version: str) -> None:
        with schedule_authority(timeout_seconds=_SETTLE_SECONDS) as authority:
            newer_outcome.append(
                reconcile_schedules(
                    authority,
                    version=version,
                    schedules=newer_declares,
                    declared=others | {entry["schedule_name"] for entry in newer_declares},
                )
            )

    older = threading.Thread(target=run_older)
    older.start()
    newer: threading.Thread | None = None
    try:
        assert stalled.wait(_SETTLE_SECONDS)
        if lost_at == "before the commit":
            assert older_outcome[0].applied
            assert [stored.name for stored in older_outcome[0].deleted] == [f"{run}retired"]

        newer = threading.Thread(
            target=run_newer, args=(_register_newer_version(system_engine, dbos),)
        )
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
        assert newer_outcome[0].applied
    finally:
        resume.set()
        older.join(_SETTLE_SECONDS)
        if newer is not None:
            newer.join(_SETTLE_SECONDS)

    assert not older.is_alive()
    assert len(older_failure) == 1, "the older worker carried on without its authority"
    assert _stored(run) == {
        f"{run}changed": _JAN_2,
        f"{run}retired": _JAN_1,
        f"{run}newer-only": _JAN_1,
    }
