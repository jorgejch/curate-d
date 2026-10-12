"""The executor lease against PostgreSQL.

The retry, deadline and monitoring logic is unit-tested against an in-memory lock
server; what only a real server can show is that the advisory lock excludes a second
session, that pg_locks is read with the right key split, and that a terminated backend
is noticed as a lost lease.
"""

import threading
from collections.abc import Iterator
from uuid import uuid4

import psycopg
import pytest

from curate_d.config import Settings
from curate_d.runtime.dbos_app import APPLICATION_NAME
from curate_d.runtime.lease import (
    ExecutorLease,
    ExecutorLeaseUnavailableError,
    VersionLease,
    connect_session,
    lease_conninfo,
    lease_key,
    version_lease_key,
)

pytestmark = pytest.mark.integration


def _identity(*, negative: bool) -> str:
    """Return a fresh identity whose key has the requested sign, to cover both key splits."""
    while True:
        candidate = f"lease-test-{uuid4().hex[:12]}"
        if (lease_key(APPLICATION_NAME, candidate) < 0) == negative:
            return candidate


@pytest.fixture(params=[False, True], ids=["positive-key", "negative-key"])
def executor_id(request: pytest.FixtureRequest) -> str:
    """Return a unique identity per test, with a positive or a negative lock key."""
    negative = request.param
    assert isinstance(negative, bool)
    return _identity(negative=negative)


@pytest.fixture
def leases(settings: Settings) -> Iterator[list[ExecutorLease]]:
    """Collect leases created by a test and release them all afterwards."""
    created: list[ExecutorLease] = []
    yield created
    for lease in created:
        lease.release()


def _lease(
    settings: Settings,
    executor_id: str,
    leases: list[ExecutorLease],
    *,
    probe_interval: float | None = None,
) -> ExecutorLease:
    """Return a lease on the configured database, released after the test."""
    if probe_interval is None:
        lease = ExecutorLease.for_settings(settings, executor_id)
    else:
        conninfo = lease_conninfo(settings)
        lease = ExecutorLease(
            executor_id,
            connect=lambda: connect_session(conninfo, application_name=f"lease-test {executor_id}"),
            probe_interval=probe_interval,
        )
    leases.append(lease)
    return lease


def test_a_second_holder_cannot_acquire_until_the_first_releases(
    settings: Settings, executor_id: str, leases: list[ExecutorLease]
) -> None:
    """At most one live process per identity, and a clean release frees it at once."""
    first = _lease(settings, executor_id, leases)
    second = _lease(settings, executor_id, leases)
    first.acquire(timeout=10)

    assert not second.try_acquire()
    with pytest.raises(ExecutorLeaseUnavailableError, match="held by pid"):
        second.acquire(timeout=0.3, initial_backoff=0.1)

    first.release()
    assert second.try_acquire()


def test_a_waiting_holder_acquires_once_the_first_releases(
    settings: Settings, executor_id: str, leases: list[ExecutorLease]
) -> None:
    """A replacement waits out its predecessor rather than failing at the first refusal."""
    first = _lease(settings, executor_id, leases)
    second = _lease(settings, executor_id, leases)
    first.acquire(timeout=10)
    releaser = threading.Timer(0.3, first.release)
    releaser.start()
    try:
        second.acquire(timeout=10, initial_backoff=0.05, max_backoff=0.1)
    finally:
        releaser.join()
    assert second.held


def test_distinct_identities_do_not_exclude_each_other(
    settings: Settings, leases: list[ExecutorLease]
) -> None:
    """Grant leases on two identities at once."""
    first = _lease(settings, _identity(negative=False), leases)
    second = _lease(settings, _identity(negative=True), leases)
    assert first.try_acquire() and second.try_acquire()


def test_the_holding_session_is_found_in_pg_locks(settings: Settings, executor_id: str) -> None:
    """The monitor's check reads the key back from pg_locks; a wrong split never matches."""
    conninfo = lease_conninfo(settings)
    key = lease_key(APPLICATION_NAME, executor_id)
    holder = connect_session(conninfo, application_name="lease-test holder")
    other = connect_session(conninfo, application_name="lease-test other")
    try:
        assert not holder.holds(key)
        assert holder.try_lock(key)
        assert holder.holds(key)
        assert not other.holds(key)
        described = other.holder(key)
        assert described is not None and "lease-test holder" in described
        holder.unlock(key)
        assert not holder.holds(key)
        assert other.holder(key) is None
    finally:
        holder.close()
        other.close()


def test_a_terminated_lease_session_is_reported_lost_and_frees_the_identity(
    settings: Settings, executor_id: str, leases: list[ExecutorLease]
) -> None:
    """The server ending the session ends the lease; the worker must hear about it."""
    lease = _lease(settings, executor_id, leases, probe_interval=0.05)
    lease.acquire(timeout=10)
    lost = threading.Event()
    lease.monitor(lost.set)

    halves = lease_key(APPLICATION_NAME, executor_id) & 0xFFFF_FFFF_FFFF_FFFF
    with psycopg.connect(lease_conninfo(settings), autocommit=True) as admin:
        terminated = admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_locks"
            " WHERE locktype = 'advisory' AND objsubid = 1"
            " AND classid = %s::bigint::oid AND objid = %s::bigint::oid",
            (halves >> 32, halves & 0xFFFF_FFFF),
        ).fetchall()
    assert terminated == [(True,)]

    assert lost.wait(10), "a terminated lease session went unnoticed"
    assert not lease.held
    successor = _lease(settings, executor_id, leases)
    assert successor.try_acquire()


def _version() -> str:
    """Return a version no other test or deployment leases."""
    return f"9.9.9-lease-{uuid4().hex[:8]}"


def test_workers_of_one_version_share_its_lease(
    settings: Settings, leases: list[ExecutorLease]
) -> None:
    """Both hold it shared on their own lease sessions, as pg_locks reports."""
    version = _version()
    first = ExecutorLease.for_settings(settings, _identity(negative=False), version=version)
    second = ExecutorLease.for_settings(settings, _identity(negative=True), version=version)
    leases.extend((first, second))
    assert first.try_acquire() and second.try_acquire()
    probe = connect_session(lease_conninfo(settings), application_name="lease-test probe")
    try:
        assert probe.holder_shares(first.key) and probe.holder_shares(second.key)
    finally:
        probe.close()


def test_recovery_takes_a_version_only_once_no_worker_holds_it(
    settings: Settings, leases: list[ExecutorLease]
) -> None:
    """Exclusive against every shared holder, and granted once the last one releases."""
    version = _version()
    workers = [
        ExecutorLease.for_settings(settings, _identity(negative=sign), version=version)
        for sign in (False, True)
    ]
    leases.extend(workers)
    assert all(worker.try_acquire() for worker in workers)
    recovery = VersionLease.for_settings(settings, version)
    try:
        assert not recovery.try_acquire()
        workers[0].release()
        assert not recovery.try_acquire()
        workers[1].release()
        assert recovery.try_acquire()
    finally:
        recovery.release()


def test_a_worker_waits_while_its_version_is_being_recovered(
    settings: Settings, leases: list[ExecutorLease]
) -> None:
    """A worker of the version cannot start mid-fork; it starts once recovery lets go."""
    version = _version()
    recovery = VersionLease.for_settings(settings, version)
    assert recovery.try_acquire()
    worker = ExecutorLease.for_settings(settings, _identity(negative=False), version=version)
    leases.append(worker)
    try:
        with pytest.raises(ExecutorLeaseUnavailableError, match="being recovered"):
            worker.acquire(timeout=0.3, initial_backoff=0.1)
        assert not worker.held
        releaser = threading.Timer(0.3, recovery.release)
        releaser.start()
        try:
            worker.acquire(timeout=10, initial_backoff=0.05, max_backoff=0.1)
        finally:
            releaser.join()
        assert worker.held
    finally:
        recovery.release()


def test_a_worker_lease_reads_back_both_locks(
    settings: Settings, leases: list[ExecutorLease]
) -> None:
    """The monitor's checks find the identity exclusive and the version shared."""
    version = _version()
    executor_id = _identity(negative=True)
    conninfo = lease_conninfo(settings)
    session = connect_session(conninfo, application_name="lease-test both")
    try:
        key = lease_key(APPLICATION_NAME, executor_id)
        shared = version_lease_key(APPLICATION_NAME, version)
        assert session.try_lock(key) and session.try_lock_shared(shared)
        assert session.holds(key) and not session.holds(key, shared=True)
        assert session.holds(shared, shared=True) and not session.holds(shared)
        session.unlock_shared(shared)
        assert not session.holds(shared, shared=True)
    finally:
        session.close()
