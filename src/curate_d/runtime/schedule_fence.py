"""The fence that lets only the latest application version write persisted schedules.

Schedules are written by the deployment command through DBOS's public `DBOSClient`
schedule API. The client opens a transaction of its own for each call, so the fence goes
where the client cannot avoid it: a SQLAlchemy `begin` listener on the engine the client
is built with (`DBOSClient(system_database_engine=...)`). While the fence is armed, every
transaction the client opens first

1. bounds its lock waits (`lock_timeout`, local to the transaction),
2. takes the schedule authority, a transaction-scoped advisory lock
   (`pg_advisory_xact_lock`) on `schedule_authority_key`, and
3. reads DBOS's latest application version from `dbos.application_versions`, and raises
   `StaleScheduleDeploymentError` if it is not the command's own.

The check and the client's writes then commit together, under the lock, or not at all.
A newer deployment takes ownership when its workers register its version at launch: from
then on an older command, stalled or cancelled and resumed, fails its next transaction,
and one already past the check holds the lock, so the newer command's writes land after
it. Losing the backend loses the lock and the pending writes together.

The key is the one the worker-side schedule authority took before deployment-time
reconciliation replaced it, so a rollout from such a build serializes with its workers.

This reads a DBOS table through SQL, the schema coupling migrations 0005, 0007, 0008 and
0009 already have, rather than private DBOS Python API.
`tests/integration/test_schedule_deploy.py` pins the columns it reads; re-check them on
every DBOS upgrade.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from typing import Final

import sqlalchemy as sa
from dbos import DBOSClient
from psycopg.errors import LockNotAvailable
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from curate_d.config import Settings
from curate_d.runtime.dbos_app import (
    APPLICATION_NAME,
    SYSTEM_SCHEMA,
    system_database_url,
)

# How long a deployment command waits for another to finish a write. Each holds the lock
# for one short transaction; a longer wait means a holder is stuck, and the command fails
# visibly rather than queueing behind it. It bounds every lock wait in the transaction.
SCHEDULE_AUTHORITY_TIMEOUT_SECONDS: Final = 120.0

# A connection the command leaves idle inside a transaction is ended by the server, which
# releases the lock with it. DBOSClient applies its own timeout only to engines it builds.
_IDLE_IN_TRANSACTION_TIMEOUT_MS: Final = 60_000

# Versioned, and distinct from the executor lease's domain: both are single-bigint
# advisory keys in one lock space, so only the domain keeps them from aliasing.
_DOMAIN: Final = b"curate-d/schedule-authority/v1"

_LOCK_TIMEOUT: Final = sa.text("SELECT set_config('lock_timeout', :timeout, true)")
_LOCK: Final = sa.text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))")
# DBOS's own definition of an application's latest version: its rows and unclaimed ones,
# newest timestamp first (`DBOSClient.get_latest_application_version`).
_LATEST_VERSION: Final = sa.text(
    f"""
    SELECT version_name FROM {SYSTEM_SCHEMA}.application_versions
    WHERE application_name = :application OR application_name IS NULL
    ORDER BY version_timestamp DESC
    LIMIT 1
    """
)
# pg_locks reports a bigint advisory key as classid = high 32 bits, objid = low 32 bits.
_HOLDER: Final = sa.text(
    """
    SELECT l.pid, a.application_name, host(a.client_addr), a.xact_start
    FROM pg_locks AS l LEFT JOIN pg_stat_activity AS a ON a.pid = l.pid
    WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1
      AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND l.classid = CAST(:high AS bigint)::oid AND l.objid = CAST(:low AS bigint)::oid
    """
)

# The name the command's connections report in pg_stat_activity, and so in a holder
# description.
CONNECTION_APPLICATION_NAME: Final = "curate-d-schedules"


class ScheduleAuthorityUnavailableError(RuntimeError):
    """Another writer held the schedule authority past the timeout."""


class StaleScheduleDeploymentError(RuntimeError):
    """The command's version is not DBOS's latest, so a newer deployment owns the schedules."""


def schedule_authority_key(application: str = APPLICATION_NAME) -> int:
    """Return the signed 64-bit advisory-lock key serializing one application's schedules."""
    material = b"\0".join((_DOMAIN, application.encode()))
    return int.from_bytes(sha256(material).digest()[:8], "big", signed=True)


class ScheduleFence:
    """The check every armed transaction makes before the client writes in it.

    Arming is per thread, so reads made while waiting for the version to register, and
    other threads sharing the engine, pass unfenced.
    """

    def __init__(
        self,
        *,
        application: str,
        version: str,
        timeout_seconds: float = SCHEDULE_AUTHORITY_TIMEOUT_SECONDS,
    ) -> None:
        self.application = application
        self.version = version
        self.timeout_seconds = timeout_seconds
        self._key = schedule_authority_key()
        self._state = threading.local()

    @property
    def is_armed(self) -> bool:
        """Return whether transactions this thread opens are fenced."""
        return bool(getattr(self._state, "armed", False))

    @contextmanager
    def armed(self) -> Iterator[None]:
        """Fence every transaction this thread opens in the block."""
        previous = self.is_armed
        self._state.armed = True
        try:
            yield
        finally:
            self._state.armed = previous

    def install(self, engine: sa.Engine) -> None:
        """Run the check at the start of every transaction on `engine`."""
        event.listen(engine, "begin", self.check)

    def check(self, connection: sa.Connection) -> None:
        """Take the authority and confirm this version is the latest, if armed.

        Runs as the transaction begins, so the client's own statements follow it in the
        same transaction.

        Raises:
            ScheduleAuthorityUnavailableError: The lock was not free within the timeout.
            StaleScheduleDeploymentError: Another version is the latest, or none is.
        """
        if not self.is_armed:
            return
        timeout_ms = max(1, round(self.timeout_seconds * 1000))
        connection.execute(_LOCK_TIMEOUT, {"timeout": f"{timeout_ms}ms"})
        try:
            connection.execute(_LOCK, {"key": self._key})
        except DBAPIError as error:
            if not isinstance(error.orig, LockNotAvailable):
                raise
            raise ScheduleAuthorityUnavailableError(
                f"another writer held the schedule authority for over "
                f"{self.timeout_seconds:g}s: "
                f"{self._describe_holder(connection.engine) or 'holder not visible'}"
            ) from None
        latest = connection.execute(_LATEST_VERSION, {"application": self.application}).scalar()
        if latest != self.version:
            raise StaleScheduleDeploymentError(
                f"version {self.version!r} is not the latest of {self.application!r} "
                f"({latest!r} is): the schedules belong to that deployment"
            )

    def _describe_holder(self, engine: sa.Engine) -> str | None:
        """Describe the backend holding the key by pid, application, address and start."""
        unsigned = self._key & 0xFFFF_FFFF_FFFF_FFFF
        previous = self.is_armed
        self._state.armed = False
        try:
            with engine.connect() as connection:
                row = connection.execute(
                    _HOLDER, {"high": unsigned >> 32, "low": unsigned & 0xFFFF_FFFF}
                ).first()
        except SQLAlchemyError:
            return None
        finally:
            self._state.armed = previous
        if row is None:
            return None
        pid, holder, address, started = row
        return f"pid {pid} ({holder or 'unknown'} from {address or 'local'}, since {started})"


def fenced_engine(settings: Settings, fence: ScheduleFence) -> sa.Engine:
    """Build an engine on the DBOS system database whose transactions `fence` checks."""
    engine = sa.create_engine(
        system_database_url(settings),
        pool_pre_ping=True,
        connect_args={
            "application_name": CONNECTION_APPLICATION_NAME,
            "options": f"-c idle_in_transaction_session_timeout={_IDLE_IN_TRANSACTION_TIMEOUT_MS}",
        },
    )
    fence.install(engine)
    return engine


@contextmanager
def fenced_client(settings: Settings, fence: ScheduleFence) -> Iterator[DBOSClient]:
    """Open a DBOS client acting for the fence's application, behind `fence`.

    Connection errors raise rather than block: the deploy job retries the whole command,
    which converges.
    """
    engine = fenced_engine(settings, fence)
    try:
        client = DBOSClient(
            system_database_engine=engine,
            dbos_system_schema=SYSTEM_SCHEMA,
            application_name=fence.application,
            retry_connection_errors=False,
        )
        try:
            yield client
        finally:
            client.destroy()
    finally:
        engine.dispose()
