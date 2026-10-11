"""The executor lease, against an in-memory lock server.

The SQL itself is exercised against PostgreSQL in tests/integration/test_executor_lease.py;
what is checked here is the lease's own behavior: when it retries, when it gives up, and
that losing it is reported exactly once.
"""

import threading
import time

import psycopg
import pytest

from curate_d.config import Settings
from curate_d.runtime import lease as lease_module
from curate_d.runtime.lease import (
    ExecutorLease,
    ExecutorLeaseUnavailableError,
    connect_session,
    lease_conninfo,
    lease_key,
)
from tests.unit.runtime.fake_lease import FakeLockServer, FakeSession


def test_the_key_is_pinned() -> None:
    """A changed derivation lets an old and a new build hold one identity at once."""
    assert lease_key("curate-d", "worker-io-0") == 6783618544537927710


def test_the_key_is_stable_and_a_signed_bigint() -> None:
    """pg_try_advisory_lock takes a signed bigint."""
    key = lease_key("curate-d", "worker-cpu-3")
    assert key == lease_key("curate-d", "worker-cpu-3")
    assert -(2**63) <= key < 2**63


@pytest.mark.parametrize(
    "other",
    [("curate-d", "worker-io-1"), ("curate-d", "worker-io-10"), ("other-app", "worker-io-1")],
)
def test_the_key_is_distinct_per_application_and_identity(other: tuple[str, str]) -> None:
    """Differ in either the application name or the executor identity."""
    assert lease_key("curate-d", "worker-io-0") != lease_key(*other)


def test_the_components_cannot_run_together() -> None:
    """A separator keeps ("curate-d", "x") and ("curate-", "dx") apart."""
    assert lease_key("curate-d", "x") != lease_key("curate-", "dx")


def test_a_second_holder_is_refused_until_the_first_releases() -> None:
    """At most one live process per identity."""
    server = FakeLockServer()
    first, second = server.lease("worker-io-0"), server.lease("worker-io-0")
    assert first.try_acquire()
    assert not second.try_acquire()
    first.release()
    assert second.try_acquire()
    second.release()


def test_different_identities_do_not_contend() -> None:
    """Grant leases on distinct identities independently."""
    server = FakeLockServer()
    first, second = server.lease("worker-io-0"), server.lease("worker-io-1")
    assert first.try_acquire() and second.try_acquire()


def test_acquisition_retries_while_contended_then_succeeds() -> None:
    """A replacement waits out a predecessor whose session has not been reaped yet."""
    server = FakeLockServer()
    server.refuse_locks = 3
    lease = server.lease("worker-io-0")
    lease.acquire(initial_backoff=0.001, max_backoff=0.002)
    assert lease.held
    assert server.refuse_locks == 0


def test_acquisition_retries_through_an_unreachable_database() -> None:
    """A database that is still starting is waited for, not fatal."""
    server = FakeLockServer()
    server.refuse_connections = 2
    lease = server.lease("worker-io-0")
    lease.acquire(initial_backoff=0.001)
    assert lease.held
    assert len(server.sessions) == 1


def test_a_broken_session_is_replaced_on_the_next_attempt() -> None:
    """A failed attempt discards its session rather than retrying on a dead one."""
    server = FakeLockServer()
    server.refuse_locks = 1
    lease = server.lease("worker-io-0")
    assert not lease.try_acquire()
    server.sessions[0].closed = True
    with pytest.raises(psycopg.OperationalError):
        lease.try_acquire()
    assert lease.try_acquire()
    assert len(server.sessions) == 2


def test_backoff_doubles_up_to_its_ceiling(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bounded: a long wait polls at the ceiling rather than ever more rarely."""
    waits: list[float] = []
    monkeypatch.setattr(time, "sleep", waits.append)
    server = FakeLockServer()
    server.refuse_locks = 5
    server.lease("worker-io-0").acquire(initial_backoff=1.0, max_backoff=4.0)
    assert waits == [1.0, 2.0, 4.0, 4.0, 4.0]


def test_acquisition_gives_up_at_the_deadline_without_leaking_a_session() -> None:
    """A worker never starts as an identity it cannot lease."""
    server = FakeLockServer()
    holder = server.lease("worker-io-0")
    assert holder.try_acquire()
    waiting = server.lease("worker-io-0")
    with pytest.raises(ExecutorLeaseUnavailableError, match="held by pid 42"):
        waiting.acquire(timeout=0.02, initial_backoff=0.005)
    assert not waiting.held
    assert server.sessions[-1].closed


def test_acquisition_stops_waiting_on_shutdown() -> None:
    """A signalled process does not sit out the whole deadline."""
    server = FakeLockServer()
    assert server.lease("worker-io-0").try_acquire()
    stop = threading.Event()
    stop.set()
    with pytest.raises(ExecutorLeaseUnavailableError, match="shutdown"):
        server.lease("worker-io-0").acquire(stop=stop)


def test_release_unlocks_closes_and_is_idempotent() -> None:
    """Release explicitly rather than waiting for the server to notice the socket close."""
    server = FakeLockServer()
    lease = server.lease("worker-io-0")
    lease.acquire()
    lease.release()
    lease.release()
    assert server.events == ["lease-acquired", "lease-released"]
    assert server.sessions[0].closed
    assert not lease.held


def test_releasing_a_lease_never_taken_does_nothing() -> None:
    """Release a lease that was never acquired without opening a session."""
    server = FakeLockServer()
    server.lease("worker-io-0").release()
    assert server.sessions == []


def test_a_broken_lease_connection_is_reported_as_lost() -> None:
    """The worker must stop: its identity may already be leased elsewhere."""
    server = FakeLockServer()
    lease = server.lease("worker-io-0")
    lease.acquire()
    lost = threading.Event()
    lease.monitor(lost.set)
    server.fail_probes = True
    assert lost.wait(2), "losing the lease connection was not reported"
    assert not lease.held
    assert server.sessions[0].closed
    lease.release()


def test_a_lock_gone_from_a_live_session_is_reported_as_lost() -> None:
    """A lock released behind the lease's back is as lost as a dropped connection."""
    server = FakeLockServer()
    lease = server.lease("worker-io-0")
    lease.acquire()
    lost = threading.Event()
    lease.monitor(lost.set)
    server.holders.clear()
    assert lost.wait(2)
    lease.release()


def test_a_released_lease_is_not_reported_as_lost() -> None:
    """Shutdown releases the lease; the monitor must not mistake that for a loss."""
    server = FakeLockServer()
    lease = server.lease("worker-io-0")
    lease.acquire()
    lost = threading.Event()
    lease.monitor(lost.set)
    lease.release()
    server.fail_probes = True
    assert not lost.wait(0.05)


def test_only_a_held_lease_can_be_monitored() -> None:
    """Refuse to monitor a lease that is not held, or monitor one twice."""
    server = FakeLockServer()
    lease = server.lease("worker-io-0")
    with pytest.raises(RuntimeError, match="held"):
        lease.monitor(lambda: None)
    lease.acquire()
    lease.monitor(lambda: None)
    with pytest.raises(RuntimeError, match="already"):
        lease.monitor(lambda: None)
    lease.release()


def test_the_lease_connection_detects_a_dead_server(monkeypatch: pytest.MonkeyPatch) -> None:
    """Client keepalives and a user timeout bound how long a lost server goes unnoticed."""
    captured: dict[str, object] = {}

    def connect(conninfo: str, **options: object) -> object:
        captured.update(options, conninfo=conninfo)
        return object()

    monkeypatch.setattr(psycopg, "connect", connect)
    connect_session("postgresql://db/curate_d", application_name="x" * 80)
    assert captured["autocommit"] is True
    assert captured["keepalives"] == 1
    assert captured["keepalives_idle"] == lease_module.KEEPALIVES_IDLE_SECONDS
    assert captured["keepalives_interval"] == lease_module.KEEPALIVES_INTERVAL_SECONDS
    assert captured["keepalives_count"] == lease_module.KEEPALIVES_COUNT
    assert captured["tcp_user_timeout"] == lease_module.TCP_USER_TIMEOUT_MILLISECONDS
    assert captured["connect_timeout"] == lease_module.CONNECT_TIMEOUT_SECONDS
    assert captured["application_name"] == "x" * 63


def test_the_lease_targets_the_ledger_database_as_libpq_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Advisory locks are per database: the lease shares the ledger database with DBOS's rows."""
    monkeypatch.setenv(
        "CURATE_D_LEDGER_DATABASE_URL",
        "postgresql+psycopg://dbos:secret@system:5432/dbos?sslmode=require",
    )
    assert lease_conninfo(Settings()) == "postgresql://dbos:secret@system:5432/dbos?sslmode=require"


def test_a_lease_for_settings_names_its_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    """pg_stat_activity tells a worker's lease from a sweep's claim."""
    names: list[str] = []
    server = FakeLockServer()

    def connect(conninfo: str, *, application_name: str) -> FakeSession:
        names.append(application_name)
        return server.connect()

    monkeypatch.setattr(lease_module, "connect_session", connect)
    lease = ExecutorLease.for_settings(Settings(), "worker-io-0", purpose="recovery sweep")
    assert lease.try_acquire()
    assert names == ["curate-d recovery sweep worker-io-0"]
    assert lease.key == lease_key("curate-d", "worker-io-0")
