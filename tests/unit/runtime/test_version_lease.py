"""The version registry, exclusive version holds, the standalone version lease and the fence.

The SQL runs against PostgreSQL in tests/integration/test_version_lease.py; what is checked
here is what each piece does with its connection and its locks, on in-memory doubles.
"""

import asyncio
import threading
from datetime import UTC, datetime

import psycopg
import pytest

from curate_d.config import Settings
from curate_d.runtime import version_lease
from curate_d.runtime.lease import (
    ExecutorLeaseLostError,
    ExecutorLeaseUnavailableError,
    lease_key,
)
from curate_d.runtime.version_lease import (
    LockMode,
    PromotionFenceTimeoutError,
    StandaloneVersionLease,
    VersionRegistryError,
    hold_exclusive,
    hold_fence_exclusive,
    hold_fence_shared,
    promotion_fence_key,
    register_version,
    registrations,
    version_lease_key,
)
from tests.unit.runtime.fake_lease import FakeLockServer

KEY = version_lease_key("curate-d", "v1")
FENCE = promotion_fence_key("curate-d")


class _Cursor:
    """The rows a statement returned, as psycopg's cursor exposes them."""

    def __init__(self, rows: list[tuple[object, ...]] | None) -> None:
        self._rows = rows
        self.description = None if rows is None else ("version", "first_registered")

    def fetchall(self) -> list[tuple[object, ...]]:
        return list(self._rows or [])


class _Connection:
    """A dedicated connection that records statements and is closed exactly once."""

    def __init__(
        self, rows: list[tuple[object, ...]] | None = None, error: Exception | None = None
    ) -> None:
        self.rows = rows
        self.error = error
        self.statements: list[tuple[str, dict[str, str]]] = []
        self.closed = 0

    def execute(self, query: str, params: dict[str, str]) -> _Cursor:
        self.statements.append((query, params))
        if self.error is not None:
            raise self.error
        return _Cursor(self.rows)

    def close(self) -> None:
        self.closed += 1


def _connect_to(
    monkeypatch: pytest.MonkeyPatch, connection: _Connection, names: list[str] | None = None
) -> None:
    """Make the registry open `connection`, recording the application names it uses."""

    def connect(conninfo: str, *, application_name: str) -> _Connection:
        if names is not None:
            names.append(application_name)
        return connection

    monkeypatch.setattr(version_lease, "connect_dedicated", connect)


def test_registration_keeps_the_first_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The insert does nothing on conflict, so a restart never retimes a version."""
    connection = _Connection()
    names: list[str] = []
    _connect_to(monkeypatch, connection, names)

    register_version(Settings(), "v1")

    [(query, params)] = connection.statements
    assert "ON CONFLICT (version) DO NOTHING" in query
    assert "first_registered" not in query, "the database clock must set the time"
    assert params == {"version": "v1"}
    assert connection.closed == 1
    assert names == ["curate-d version registration"]


def test_a_failed_registration_names_the_version(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker must not launch, and the error says what failed for which version."""
    connection = _Connection(error=psycopg.OperationalError("connection refused"))
    _connect_to(monkeypatch, connection)

    with pytest.raises(VersionRegistryError, match="registering version 'v1'") as raised:
        register_version(Settings(), "v1")
    assert isinstance(raised.value.__cause__, psycopg.OperationalError)
    assert connection.closed == 1


def test_an_unreachable_registry_fails_registration(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection that cannot open is a registration failure, not a crash elsewhere."""

    def refuse(conninfo: str, *, application_name: str) -> _Connection:
        raise psycopg.OperationalError("no route to host")

    monkeypatch.setattr(version_lease, "connect_dedicated", refuse)
    with pytest.raises(VersionRegistryError, match="no route to host"):
        register_version(Settings(), "v1")


async def test_registrations_map_each_version_to_its_first_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep reads deploy order from the registry."""
    first = datetime(2026, 10, 1, tzinfo=UTC)
    second = datetime(2026, 10, 2, tzinfo=UTC)
    connection = _Connection(rows=[("v1", first), ("v2", second)])
    _connect_to(monkeypatch, connection)

    assert await registrations(Settings()) == {"v1": first, "v2": second}
    assert connection.closed == 1


async def test_an_unreadable_registry_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """The sweep must fork nothing when it cannot tell which versions registered."""
    _connect_to(monkeypatch, _Connection(error=psycopg.OperationalError("timeout")))
    with pytest.raises(VersionRegistryError, match="reading lease_versions failed"):
        await registrations(Settings())


@pytest.mark.parametrize("row", [("v1", None), (42, datetime(2026, 10, 1, tzinfo=UTC))])
async def test_a_malformed_registry_row_raises(
    monkeypatch: pytest.MonkeyPatch, row: tuple[object, ...]
) -> None:
    """A row the registry should never hold is not silently skipped."""
    _connect_to(monkeypatch, _Connection(rows=[row]))
    with pytest.raises(VersionRegistryError, match="unexpected lease_versions row"):
        await registrations(Settings())


async def test_an_exclusive_hold_is_released_after_its_block() -> None:
    """The lock lives exactly as long as the block, and its session is closed."""
    server = FakeLockServer()
    async with hold_exclusive(Settings(), "v1", connect=server.connect) as hold:
        assert hold.held
        assert server.mode(KEY) == "exclusive"
    assert server.mode(KEY) is None
    assert all(session.closed for session in server.sessions)


async def test_an_exclusive_hold_is_released_when_its_block_raises() -> None:
    """A failed fork still releases the version's lock."""
    server = FakeLockServer()
    with pytest.raises(RuntimeError, match="fork failed"):
        async with hold_exclusive(Settings(), "v1", connect=server.connect) as hold:
            assert hold.held
            raise RuntimeError("fork failed")
    assert server.mode(KEY) is None
    assert all(session.closed for session in server.sessions)


@pytest.mark.parametrize(
    ("take", "mode"),
    [("shared", LockMode.SHARED), ("exclusive", LockMode.EXCLUSIVE)],
)
async def test_a_refused_hold_reports_the_mode_held(take: str, mode: LockMode) -> None:
    """Live workers hold the version shared; another sweep holds it exclusively."""
    server = FakeLockServer()
    other = server.connect()
    assert other.try_lock_shared(KEY) if take == "shared" else other.try_lock(KEY)

    async with hold_exclusive(Settings(), "v1", connect=server.connect) as hold:
        assert not hold.held
        assert hold.holder_mode is mode
    assert server.sessions[-1].closed
    assert server.mode(KEY) == take


async def test_a_lock_error_raises_and_holds_nothing() -> None:
    """An error taking the lock surfaces before any block runs."""
    server = FakeLockServer()
    server.refuse_connections = 1
    with pytest.raises(psycopg.OperationalError):
        async with hold_exclusive(Settings(), "v1", connect=server.connect):
            pytest.fail("the block must not run")
    assert server.mode(KEY) is None


def test_a_standalone_lease_holds_the_version_shared_beside_workers() -> None:
    """The operator sweep coexists with the version's live workers."""
    server = FakeLockServer()
    worker = server.lease("worker-io-0", version="v1")
    assert worker.try_acquire()
    standalone = StandaloneVersionLease("v1", connect=server.connect)
    assert standalone.try_acquire()
    assert standalone.version_key == KEY
    assert server.mode(KEY) == "shared"
    standalone.release()
    assert worker.held
    assert server.mode(KEY) == "shared"


def test_a_standalone_lease_waits_out_a_sweep_then_gives_up() -> None:
    """An exclusive holder keeps it waiting, within its budget."""
    server = FakeLockServer()
    assert server.connect().try_lock(KEY)
    standalone = StandaloneVersionLease("v1", connect=server.connect)
    with pytest.raises(ExecutorLeaseUnavailableError, match="version 'v1'"):
        standalone.acquire(timeout=0.02, initial_backoff=0.005, max_backoff=0.005)


def test_a_standalone_lease_never_takes_an_executor_key() -> None:
    """Only the shared version lock is taken and given back."""
    server = FakeLockServer()
    standalone = StandaloneVersionLease("v1", connect=server.connect)
    assert standalone.try_acquire()
    standalone.release()
    assert server.events == ["version-shared-acquired", "version-shared-released"]
    assert server.holders == {}


def test_a_lost_standalone_lease_stops_the_next_write() -> None:
    """Once the monitor finds the lock lost, `ensure_held` refuses every further write."""
    server = FakeLockServer()
    standalone = StandaloneVersionLease("v1", connect=server.connect, probe_interval=0.01)
    with pytest.raises(ExecutorLeaseLostError):
        standalone.ensure_held()
    standalone.acquire(timeout=1)
    standalone.ensure_held()

    lost = threading.Event()
    standalone.monitor(lost.set)
    server.fail_probes = True
    assert lost.wait(1)
    with pytest.raises(ExecutorLeaseLostError, match="stopping before the next write"):
        standalone.ensure_held()
    standalone.release()


async def test_a_refused_lock_with_no_visible_holder_stays_refused() -> None:
    """A disappearing holder is not proof that this session acquired the version lock."""
    server = FakeLockServer()
    server.refuse_locks = 1
    async with hold_exclusive(Settings(), "v1", connect=server.connect) as hold:
        assert not hold.held
        assert hold.holder_mode is None
    assert server.events == []
    assert server.sessions[0].closed


@pytest.mark.parametrize("operation", ["try_lock", "holder_mode"])
async def test_a_lock_query_failure_closes_the_opened_session(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """Query errors release the dedicated connection before propagating."""
    server = FakeLockServer()
    session = server.connect()
    server.refuse_locks = 1
    error = psycopg.OperationalError("lock query failed")

    def fail(key: int) -> None:
        """Raise the database failure at the selected query boundary."""
        raise error

    monkeypatch.setattr(session, operation, fail)
    with pytest.raises(psycopg.OperationalError) as raised:
        async with hold_exclusive(Settings(), "v1", connect=lambda: session):
            pytest.fail("a query failure must not enter the recovery block")
    assert raised.value is error
    assert session.closed
    assert server.mode(KEY) is None


async def test_an_unlock_failure_closes_the_session_and_preserves_the_body_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing releases the lock even when unlock fails during failed recovery."""
    server = FakeLockServer()
    session = server.connect()

    def fail_unlock(key: int) -> None:
        """Simulate a database failure before the lock can be explicitly released."""
        raise psycopg.OperationalError("unlock failed")

    monkeypatch.setattr(session, "unlock", fail_unlock)
    error = RuntimeError("recovery failed")
    with pytest.raises(RuntimeError) as raised:
        async with hold_exclusive(Settings(), "v1", connect=lambda: session):
            assert server.mode(KEY) == "exclusive"
            raise error
    assert raised.value is error
    assert session.closed
    assert server.mode(KEY) is None


async def test_cancelling_recovery_releases_the_exclusive_version_hold() -> None:
    """Task cancellation propagates after releasing the source version's lock."""
    server = FakeLockServer()
    with pytest.raises(asyncio.CancelledError):
        async with hold_exclusive(Settings(), "v1", connect=server.connect):
            raise asyncio.CancelledError
    assert server.mode(KEY) is None
    assert all(session.closed for session in server.sessions)


@pytest.mark.parametrize("body_fails", [False, True])
async def test_a_close_failure_does_not_replace_the_exclusive_hold_result(
    monkeypatch: pytest.MonkeyPatch, body_fails: bool
) -> None:
    """Cleanup failure preserves a successful block or the original recovery exception."""
    server = FakeLockServer()
    session = server.connect()
    close = session.close
    error = RuntimeError("recovery failed")

    def fail_close() -> None:
        """Close the session before reporting the connection cleanup error."""
        close()
        raise psycopg.OperationalError("close failed")

    monkeypatch.setattr(session, "close", fail_close)

    async def recover() -> None:
        """Enter the hold and optionally fail the recovery operation."""
        async with hold_exclusive(Settings(), "v1", connect=lambda: session) as hold:
            assert hold.held
            if body_fails:
                raise error

    if body_fails:
        with pytest.raises(RuntimeError) as raised:
            await recover()
        assert raised.value is error
    else:
        await recover()
    assert session.closed
    assert server.mode(KEY) is None


async def test_a_held_version_lock_vouches_for_itself_through_its_own_session() -> None:
    """Held while the session holds it; lost once the lock goes or the session breaks."""
    server = FakeLockServer()
    async with hold_exclusive(Settings(), "v1", connect=server.connect) as hold:
        assert await hold.still_held()
        server.sessions[-1].unlock(KEY)
        assert not await hold.still_held()
        assert server.sessions[-1].try_lock(KEY)
        server.fail_probes = True
        assert not await hold.still_held()


async def test_a_refused_version_hold_is_never_still_held() -> None:
    """A hold that was not taken has nothing to vouch for."""
    server = FakeLockServer()
    assert server.connect().try_lock_shared(KEY)
    async with hold_exclusive(Settings(), "v1", connect=server.connect) as hold:
        assert not hold.held
        assert not await hold.still_held()


async def test_a_held_fence_vouches_for_itself_through_its_own_session() -> None:
    """The fence hold asks its session; a closed session is a lost fence."""
    server = FakeLockServer()
    async with hold_fence_shared(Settings(), connect=server.connect) as fenced:
        assert await fenced.still_held()
        server.sessions[-1].close()
        assert not await fenced.still_held()


# The promotion fence.


def test_the_fence_key_is_its_own() -> None:
    """The fence never aliases an executor or version key spelled like its name."""
    assert FENCE not in {
        lease_key("curate-d", "latest-version"),
        version_lease_key("curate-d", "latest-version"),
    }
    assert promotion_fence_key("curate-d") == FENCE
    assert promotion_fence_key("other") != FENCE


async def test_sweeps_share_the_fence_and_release_it_after_their_block() -> None:
    """Two sweeps hold it together; each session is closed once its block exits."""
    server = FakeLockServer()
    async with (
        hold_fence_shared(Settings(), connect=server.connect) as first,
        hold_fence_shared(Settings(), connect=server.connect) as second,
    ):
        assert first.held and second.held
        assert await first.still_held() and await second.still_held()
        assert len(server.shared[FENCE]) == 2
    assert server.mode(FENCE) is None
    assert all(session.closed for session in server.sessions)


async def test_a_promotion_holding_the_fence_refuses_a_sweep() -> None:
    """The sweep's one attempt is refused without waiting."""
    server = FakeLockServer()
    assert server.connect().try_lock(FENCE)
    async with hold_fence_shared(Settings(), connect=server.connect) as fenced:
        assert not fenced.held
        assert not await fenced.still_held()
    assert server.sessions[-1].closed
    assert server.mode(FENCE) == "exclusive"


async def test_the_shared_fence_is_released_when_its_block_raises() -> None:
    """A sweep that fails mid-batch still lets a promotion through."""
    server = FakeLockServer()
    with pytest.raises(RuntimeError, match="fork failed"):
        async with hold_fence_shared(Settings(), connect=server.connect) as fenced:
            assert fenced.held
            raise RuntimeError("fork failed")
    assert server.mode(FENCE) is None


async def test_a_shared_fence_error_raises_and_holds_nothing() -> None:
    """An unreachable database surfaces before any block runs."""
    server = FakeLockServer()
    server.refuse_connections = 1
    with pytest.raises(psycopg.OperationalError):
        async with hold_fence_shared(Settings(), connect=server.connect):
            pytest.fail("the block must not run")
    assert server.mode(FENCE) is None


def test_a_promotion_holds_the_fence_exclusively_for_its_block() -> None:
    """The fence is held across the block, waited for with the given timeout, then freed."""
    server = FakeLockServer()
    with hold_fence_exclusive(Settings(), timeout=7.5, connect=server.connect):
        assert server.mode(FENCE) == "exclusive"
    assert server.waits == [7.5]
    assert server.mode(FENCE) is None
    assert all(session.closed for session in server.sessions)


def test_a_sweep_holding_the_fence_times_out_a_promotion() -> None:
    """The block never runs, the session is closed, and the sweep keeps its hold."""
    server = FakeLockServer()
    sweep = server.connect()
    assert sweep.try_lock_shared(FENCE)
    with (
        pytest.raises(PromotionFenceTimeoutError, match="after 2s"),
        hold_fence_exclusive(Settings(), timeout=2, connect=server.connect),
    ):
        pytest.fail("the block must not run")
    assert server.sessions[-1].closed
    assert server.mode(FENCE) == "shared"


def test_the_exclusive_fence_is_released_when_its_block_raises() -> None:
    """A failed promotion still lets the next sweep take the fence."""
    server = FakeLockServer()
    with (
        pytest.raises(RuntimeError, match="promotion failed"),
        hold_fence_exclusive(Settings(), timeout=1, connect=server.connect),
    ):
        raise RuntimeError("promotion failed")
    assert server.mode(FENCE) is None
