"""Executor lease: at most one live process per executor identity.

DBOS recovers a process's PENDING workflows by executor identity, and two live
processes sharing one identity is unsupported (dbos-transact-py #804): each may recover
or strand the other's work. A StatefulSet already avoids that in the normal case; the
lease makes it hold when the controller is bypassed, such as a forced pod deletion that
starts a replacement while the old container is still running on a partitioned node.

The lease is a session-level ``pg_try_advisory_lock`` in the database holding the DBOS
system tables, keyed on a stable 64-bit hash of (application name, executor identity).
Advisory locks are scoped to one database, so the lease and the rows it protects must
live in the same one. A worker takes it before ``DBOS.launch()`` (which runs startup
recovery), holds it on a dedicated connection outside any SQLAlchemy pool for the life
of the process, and exits if it is lost. The recovery sweep uses the same lock as its
liveness test: an executor is dead only if the sweep can take its lease.

The single-``bigint`` form occupies a different lock space (``objsubid = 1``) from the
two-``int4`` locks staging uses (``objsubid = 2``), so the two can never collide.

Session locks die with their session, so how quickly a vanished holder is noticed
depends on TCP. A crashed process's kernel closes the socket and the server releases
the lock at once. A vanished host sends nothing, so the server must be configured with
``tcp_keepalives_*`` to reap the session (compose.yaml does this locally). The client
side sets libpq keepalives and ``tcp_user_timeout`` so a worker notices a dead server
promptly and stops, rather than working on under a lock it no longer holds.

The lease connection must reach PostgreSQL directly or through a session-mode pooler.
Under PgBouncer transaction pooling a session lock is taken on whichever server
connection served that one statement and stays there after the client moves on: the
next probe runs elsewhere and reports the lease lost, the unlock misses it, and the
orphaned lock blocks the identity until PgBouncer recycles that server connection.
In session mode the server connection returns to the pool when the client goes, so the
pooler must detect a vanished client (its own ``tcp_keep*`` settings) and must reset the
connection with ``DISCARD ALL`` (the default ``server_reset_query``), which is what
drops the lock; an emptied reset query hands the lock to the next client.
"""

import threading
import time
from collections.abc import Callable
from hashlib import sha256
from typing import Final, LiteralString, Protocol, Self

import psycopg
from psycopg.rows import TupleRow
from sqlalchemy.engine import make_url

from curate_d.config import Settings
from curate_d.observability.logging import get_logger
from curate_d.runtime.dbos_app import APPLICATION_NAME, system_database_url

logger = get_logger(__name__)

# Versioned so a change of derivation can never alias an old key held by a process
# still running the previous build.
_KEY_DOMAIN: Final = b"curate-d/executor-lease/v1"

# libpq settings on the lease connection. Idle probes start after 10s and give up after
# three unanswered 5s intervals; tcp_user_timeout bounds a probe whose bytes the server
# never acknowledges (Linux; libpq ignores it where unsupported). A dead server is
# therefore noticed within about 30 seconds.
CONNECT_TIMEOUT_SECONDS: Final = 10
KEEPALIVES_IDLE_SECONDS: Final = 10
KEEPALIVES_INTERVAL_SECONDS: Final = 5
KEEPALIVES_COUNT: Final = 3
TCP_USER_TIMEOUT_MILLISECONDS: Final = 30_000

# How often a held lease is re-checked.
PROBE_INTERVAL_SECONDS: Final = 5.0

# Startup waits this long for a previous holder to go away before giving up, which is
# ample for the server to reap a vanished session under the keepalives compose.yaml sets.
ACQUIRE_TIMEOUT_SECONDS: Final = 600.0
INITIAL_BACKOFF_SECONDS: Final = 1.0
MAX_BACKOFF_SECONDS: Final = 15.0

# PostgreSQL truncates application_name here; truncating first keeps the identity visible.
_APPLICATION_NAME_LIMIT: Final = 63


class ExecutorLeaseUnavailableError(RuntimeError):
    """The lease could not be taken before the deadline or before shutdown was requested."""


class ExecutorLeaseLostError(RuntimeError):
    """A held lease was lost; the process may no longer act as its executor identity."""


def lease_key(application_name: str, executor_id: str) -> int:
    """Return the signed 64-bit advisory-lock key for one executor identity.

    Stable across processes and releases of the same derivation, and distinct per
    (application name, executor identity) pair up to a 64-bit hash collision.
    """
    material = b"\0".join((_KEY_DOMAIN, application_name.encode(), executor_id.encode()))
    return int.from_bytes(sha256(material).digest()[:8], "big", signed=True)


class LeaseSession(Protocol):
    """One database session able to hold advisory locks."""

    def try_lock(self, key: int) -> bool:
        """Take the session lock on `key` without waiting; return whether it was taken."""
        ...

    def holds(self, key: int) -> bool:
        """Return whether this session still holds the lock on `key`."""
        ...

    def unlock(self, key: int) -> None:
        """Release this session's lock on `key`."""
        ...

    def holder(self, key: int) -> str | None:
        """Describe the session currently holding `key`, if it is visible."""
        ...

    def close(self) -> None:
        """Close the session, releasing every lock it holds."""
        ...


type SessionFactory = Callable[[], LeaseSession]

_TRY_LOCK: Final = "SELECT pg_try_advisory_lock(%(key)s)"
_UNLOCK: Final = "SELECT pg_advisory_unlock(%(key)s)"
# pg_locks reports a bigint advisory key as classid = high 32 bits, objid = low 32 bits.
_HOLDS: Final = """
    SELECT EXISTS (
        SELECT 1 FROM pg_locks
        WHERE locktype = 'advisory' AND granted AND objsubid = 1
          AND database = (SELECT oid FROM pg_database WHERE datname = current_database())
          AND classid = %(high)s::bigint::oid AND objid = %(low)s::bigint::oid
          AND pid = pg_backend_pid()
    )
"""
_HOLDER: Final = """
    SELECT l.pid, a.application_name, host(a.client_addr), a.backend_start
    FROM pg_locks AS l LEFT JOIN pg_stat_activity AS a ON a.pid = l.pid
    WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1
      AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND l.classid = %(high)s::bigint::oid AND l.objid = %(low)s::bigint::oid
"""


def _key_halves(key: int) -> dict[str, int]:
    """Split a signed 64-bit key into the unsigned halves pg_locks reports."""
    unsigned = key & 0xFFFF_FFFF_FFFF_FFFF
    return {"high": unsigned >> 32, "low": unsigned & 0xFFFF_FFFF}


class PostgresLeaseSession:
    """A `LeaseSession` on one dedicated autocommit psycopg connection."""

    def __init__(self, connection: psycopg.Connection[TupleRow]) -> None:
        self._connection = connection

    def _scalar(self, query: LiteralString, params: dict[str, int]) -> object:
        """Run `query` and return the first column of its single row."""
        row = self._connection.execute(query, params).fetchone()
        return None if row is None else row[0]

    def try_lock(self, key: int) -> bool:
        """Take the session lock on `key` without waiting; return whether it was taken."""
        return self._scalar(_TRY_LOCK, {"key": key}) is True

    def holds(self, key: int) -> bool:
        """Return whether this session still holds the lock on `key`, per pg_locks."""
        return self._scalar(_HOLDS, _key_halves(key)) is True

    def unlock(self, key: int) -> None:
        """Release this session's lock on `key`."""
        self._scalar(_UNLOCK, {"key": key})

    def holder(self, key: int) -> str | None:
        """Describe the session holding `key` as pid, application, address and start."""
        row = self._connection.execute(_HOLDER, _key_halves(key)).fetchone()
        if row is None:
            return None
        pid, application, address, started = row
        return f"pid {pid} ({application or 'unknown'} from {address or 'local'}, since {started})"

    def close(self) -> None:
        """Close the connection, which ends the session and releases its locks."""
        self._connection.close()


def lease_conninfo(settings: Settings) -> str:
    """Return a libpq URL for the database holding the DBOS system tables."""
    url = make_url(system_database_url(settings)).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


def connect_session(conninfo: str, *, application_name: str) -> PostgresLeaseSession:
    """Open a dedicated lease connection with client-side keepalives.

    Autocommit, because a session lock must not ride inside a transaction that could be
    left idle, and outside any pool, because a pooled connection is handed to other code
    and returned with whatever session state that code left on it.
    """
    connection = psycopg.connect(
        conninfo,
        autocommit=True,
        application_name=application_name[:_APPLICATION_NAME_LIMIT],
        connect_timeout=CONNECT_TIMEOUT_SECONDS,
        keepalives=1,
        keepalives_idle=KEEPALIVES_IDLE_SECONDS,
        keepalives_interval=KEEPALIVES_INTERVAL_SECONDS,
        keepalives_count=KEEPALIVES_COUNT,
        tcp_user_timeout=TCP_USER_TIMEOUT_MILLISECONDS,
    )
    return PostgresLeaseSession(connection)


def _close_quietly(session: LeaseSession) -> None:
    """Close `session`, ignoring failure: a broken session has released its locks."""
    try:
        session.close()
    except Exception as error:  # Nothing left to release on a session that cannot close.
        logger.debug("executor_lease.close_failed", error_type=type(error).__name__)


class ExecutorLease:
    """The session-level advisory lock on one executor identity.

    Thread-safe: the monitor thread and the owner share the session under one lock.
    """

    def __init__(
        self,
        executor_id: str,
        *,
        connect: SessionFactory,
        application_name: str = APPLICATION_NAME,
        probe_interval: float = PROBE_INTERVAL_SECONDS,
    ) -> None:
        """Bind a lease to an identity without connecting.

        Args:
            executor_id: The DBOS executor identity the lease stands for.
            connect: Opens a new dedicated session; called again after a broken one.
            application_name: The DBOS application name, part of the lock key.
            probe_interval: Seconds between checks of a monitored lease.
        """
        self.executor_id = executor_id
        self._probe_interval = probe_interval
        self.key = lease_key(application_name, executor_id)
        self._connect = connect
        self._session: LeaseSession | None = None
        self._held = False
        self._guard = threading.Lock()
        self._monitor: threading.Thread | None = None
        self._stop_monitoring = threading.Event()

    @classmethod
    def for_settings(
        cls, settings: Settings, executor_id: str, *, purpose: str = "executor lease"
    ) -> Self:
        """Return a lease on the configured DBOS system database.

        Args:
            settings: Resolved configuration naming the system database.
            executor_id: The identity to lease.
            purpose: Shown in pg_stat_activity, so an operator can tell a worker's lease
                from a sweep's momentary claim.
        """
        conninfo = lease_conninfo(settings)
        name = f"{APPLICATION_NAME} {purpose} {executor_id}"
        return cls(executor_id, connect=lambda: connect_session(conninfo, application_name=name))

    @property
    def held(self) -> bool:
        """Return whether this object currently holds the lease."""
        return self._held

    def try_acquire(self) -> bool:
        """Make one attempt to take the lease, connecting first if needed.

        A refused attempt keeps the session open so the holder can be described and the
        next attempt need not reconnect; `release` closes it.

        Raises:
            psycopg.Error: The session could not be opened or the attempt failed. The
                session is discarded, so the next attempt reconnects.
        """
        with self._guard:
            if self._held:
                return True
            if self._session is None:
                self._session = self._connect()
            try:
                self._held = self._session.try_lock(self.key)
            except BaseException:
                self._discard_session()
                raise
            return self._held

    def acquire(
        self,
        *,
        stop: threading.Event | None = None,
        timeout: float = ACQUIRE_TIMEOUT_SECONDS,
        initial_backoff: float = INITIAL_BACKOFF_SECONDS,
        max_backoff: float = MAX_BACKOFF_SECONDS,
    ) -> None:
        """Take the lease, retrying with capped exponential backoff while it is held elsewhere.

        Connection failures are retried the same way: the database being down at startup
        is not a reason to give up sooner than a live holder is.

        Args:
            stop: Abandon the wait once set. The first attempt is always made.
            timeout: Give up after this many seconds.
            initial_backoff: The first retry delay, doubled after each refusal.
            max_backoff: The ceiling on the retry delay.

        Raises:
            ExecutorLeaseUnavailableError: The deadline passed or `stop` was set first.
        """
        deadline = time.monotonic() + timeout
        delay = initial_backoff
        attempt = 0
        while True:
            attempt += 1
            try:
                if self.try_acquire():
                    logger.info(
                        "executor_lease.acquired", executor_id=self.executor_id, attempts=attempt
                    )
                    return
                holder = self._describe_holder()
                obstacle = f"held by {holder}" if holder else "held by another session"
            except psycopg.Error as error:
                obstacle = f"database unreachable ({type(error).__name__})"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.release()
                raise ExecutorLeaseUnavailableError(
                    f"executor identity {self.executor_id!r} is still {obstacle} after "
                    f"{timeout:.0f}s; another live process may be using it. A worker never "
                    f"starts as an identity it cannot lease"
                )
            wait = min(delay, remaining)
            logger.warning(
                "executor_lease.waiting",
                executor_id=self.executor_id,
                obstacle=obstacle,
                attempt=attempt,
                retry_in_seconds=round(wait, 3),
            )
            if stop is not None and stop.wait(wait):
                self.release()
                raise ExecutorLeaseUnavailableError(
                    f"shutdown requested while waiting for the lease on {self.executor_id!r}"
                )
            if stop is None:
                time.sleep(wait)
            delay = min(delay * 2, max_backoff)

    def monitor(self, on_lost: Callable[[], None]) -> None:
        """Re-check the held lease every probe interval on a daemon thread.

        `on_lost` is called once, from that thread, when the check fails or the session
        breaks; the lease is no longer held by then.

        Raises:
            RuntimeError: The lease is not held, or is already being monitored.
        """
        if not self._held:
            raise RuntimeError("only a held lease can be monitored")
        if self._monitor is not None:
            raise RuntimeError("the lease is already being monitored")
        self._stop_monitoring.clear()
        thread = threading.Thread(
            target=self._watch, args=(on_lost,), name="executor-lease", daemon=True
        )
        self._monitor = thread
        thread.start()

    def release(self) -> None:
        """Stop monitoring, unlock and close the session. Idempotent."""
        self._stop_monitoring.set()
        with self._guard:
            session, held = self._session, self._held
            self._session, self._held = None, False
        if session is not None:
            if held:
                try:
                    session.unlock(self.key)
                except Exception as error:  # Closing the session releases it regardless.
                    logger.warning(
                        "executor_lease.unlock_failed",
                        executor_id=self.executor_id,
                        error_type=type(error).__name__,
                    )
            _close_quietly(session)
            if held:
                logger.info("executor_lease.released", executor_id=self.executor_id)
        monitor, self._monitor = self._monitor, None
        if monitor is not None and monitor is not threading.current_thread():
            monitor.join(timeout=self._probe_interval)

    def _describe_holder(self) -> str | None:
        """Describe the current holder for the waiting log line, best effort."""
        with self._guard:
            session = self._session
            if session is None:
                return None
            try:
                return session.holder(self.key)
            except psycopg.Error:
                self._discard_session()
                return None

    def _watch(self, on_lost: Callable[[], None]) -> None:
        """Probe until released, reporting the first failure to `on_lost`."""
        while not self._stop_monitoring.wait(self._probe_interval):
            if not self._probe():
                on_lost()
                return

    def _probe(self) -> bool:
        """Return False, discarding the session, if the lease is no longer demonstrably held."""
        with self._guard:
            if self._stop_monitoring.is_set():
                return True
            session = self._session
            failure: str | None = None
            try:
                if session is None or not session.holds(self.key):
                    failure = "the session no longer holds the lock"
            except Exception as error:  # Any failure to vouch for the lease is a loss.
                failure = f"the lease connection failed ({type(error).__name__})"
            if failure is None:
                return True
            self._discard_session()
            self._held = False
        logger.error("executor_lease.lost", executor_id=self.executor_id, reason=failure)
        return False

    def _discard_session(self) -> None:
        """Close and forget the session. Call with the guard held."""
        session, self._session = self._session, None
        self._held = False
        if session is not None:
            _close_quietly(session)
