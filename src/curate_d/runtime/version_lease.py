"""Version lease: which application versions some process still runs.

A worker registers its version in `lease_versions` before launch, then holds that
version's advisory lock shared, on its executor lease's session, for its whole life
(`ExecutorLease(version=...)`). Registration says a version follows this protocol; the
lock says it is live. A process that runs no workflows but acts for a version (the
operator sweep) holds the lock shared on its own connection (`StandaloneVersionLease`)
and never registers.

The recovery sweep forks or retires a version's rows only while it holds that version's
lock exclusively (`hold_exclusive`), which no live holder of either mode allows, and only
if the version registered before the sweep's target. A build that predates the lease is
unregistered, holds nothing, and so is never mistaken for a dead version.

The lock is keyed by `version_lease_key` in the database holding the DBOS system tables,
under the executor lease's connection settings and pooler constraint (lease.py).
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final, LiteralString, Protocol

import psycopg

from curate_d.config import Settings
from curate_d.observability.logging import get_logger
from curate_d.runtime.dbos_app import APPLICATION_NAME
from curate_d.runtime.lease import (
    PROBE_INTERVAL_SECONDS,
    ExecutorLease,
    ExecutorLeaseLostError,
    LeaseSession,
    PostgresLeaseSession,
    SessionFactory,
    connect_dedicated,
    connect_session,
    lease_conninfo,
    version_lease_key,
)

__all__ = [
    "LockMode",
    "StandaloneVersionLease",
    "VersionHold",
    "VersionRegistryError",
    "hold_exclusive",
    "register_version",
    "registrations",
    "standalone_version_lease",
    "version_lease_key",
]

logger = get_logger(__name__)

_REGISTER: Final = (
    "INSERT INTO lease_versions (version) VALUES (%(version)s) ON CONFLICT (version) DO NOTHING"
)
_REGISTRATIONS: Final = "SELECT version, first_registered FROM lease_versions"


class LockMode(StrEnum):
    """The mode another session holds a version's lock in."""

    SHARED = "shared"
    """Live workers, or an operator sweep, of that version."""

    EXCLUSIVE = "exclusive"
    """Another sweep recovering that version."""


@dataclass(frozen=True)
class VersionHold:
    """The outcome of one attempt to hold a version's lock exclusively."""

    version: str
    held: bool
    holder_mode: LockMode | None = None
    """The mode another session holds the lock in, when refused and visible."""


class VersionRegistryError(RuntimeError):
    """`lease_versions` could not be written or read; the cause is chained."""


class ExclusiveLockSession(Protocol):
    """A dedicated session able to take one lock exclusively and see who else holds it."""

    def try_lock(self, key: int) -> bool:
        """Take the lock on `key` without waiting; return whether it was taken."""
        ...

    def unlock(self, key: int) -> None:
        """Release this session's lock on `key`."""
        ...

    def holder_mode(self, key: int) -> str | None:
        """Return the mode another session holds `key` in: `exclusive`, `shared` or None."""
        ...

    def close(self) -> None:
        """Close the session, releasing every lock it holds."""
        ...


def _run(
    settings: Settings, purpose: str, query: LiteralString, params: Mapping[str, str]
) -> list[tuple[object, ...]]:
    """Run one statement on a short-lived dedicated connection and return its rows."""
    connection = connect_dedicated(
        lease_conninfo(settings), application_name=f"{APPLICATION_NAME} {purpose}"
    )
    try:
        cursor = connection.execute(query, params)
        return list(cursor.fetchall()) if cursor.description is not None else []
    finally:
        connection.close()


def register_version(settings: Settings, version: str) -> None:
    """Record `version` in `lease_versions`, keeping its first registration time.

    Runs on its own connection: the executor lease's session belongs to `ExecutorLease`.

    Raises:
        VersionRegistryError: The database refused or could not be reached; the caller
            must not launch DBOS.
    """
    try:
        _run(settings, "version registration", _REGISTER, {"version": version})
    except psycopg.Error as error:
        raise VersionRegistryError(
            f"registering version {version!r} in lease_versions failed: {error}"
        ) from error
    logger.info("version_lease.registered", version=version)


async def registrations(settings: Settings) -> Mapping[str, datetime]:
    """Return every registered version with its first registration time.

    Raises:
        VersionRegistryError: The registry could not be read, or a row contains a
            non-string version or a non-datetime registration time.
    """
    try:
        rows = await asyncio.to_thread(_run, settings, "version registry read", _REGISTRATIONS, {})
    except psycopg.Error as error:
        raise VersionRegistryError(f"reading lease_versions failed: {error}") from error
    entries: dict[str, datetime] = {}
    for version, first_registered in rows:
        if not isinstance(version, str) or not isinstance(first_registered, datetime):
            raise VersionRegistryError(
                f"unexpected lease_versions row ({version!r}, {first_registered!r})"
            )
        entries[version] = first_registered
    return entries


def _connect_exclusive(settings: Settings, version: str) -> PostgresLeaseSession:
    """Open the dedicated session a sweep holds one version's lock exclusively on."""
    return connect_session(
        lease_conninfo(settings), application_name=f"{APPLICATION_NAME} recovery sweep {version}"
    )


@asynccontextmanager
async def hold_exclusive(
    settings: Settings,
    version: str,
    *,
    connect: Callable[[], ExclusiveLockSession] | None = None,
    application_name: str = APPLICATION_NAME,
) -> AsyncIterator[VersionHold]:
    """Try once to hold `version`'s lock exclusively, on a dedicated connection.

    Yields a held `VersionHold` until the block exits, or a refused one naming the mode
    another session holds, if visible. On exit, attempt to release any acquired lock and
    close the session, suppressing cleanup errors. Exceptions from the caller's block
    propagate.

    Args:
        settings: Resolved configuration naming the system database.
        version: The version whose rows the caller will fork or retire.
        connect: Opens the session; a dedicated connection to the system database when
            omitted. Substituted by tests that must not connect.
        application_name: The DBOS application name, part of the lock key.

    Raises:
        psycopg.Error: Connecting, taking the lock or reading its holder's mode failed.
    """
    key = version_lease_key(application_name, version)
    session = await asyncio.to_thread(connect or (lambda: _connect_exclusive(settings, version)))
    try:
        if not await asyncio.to_thread(session.try_lock, key):
            mode = await asyncio.to_thread(session.holder_mode, key)
            yield VersionHold(
                version, held=False, holder_mode=None if mode is None else LockMode(mode)
            )
            return
        try:
            yield VersionHold(version, held=True)
        finally:
            try:
                await asyncio.to_thread(session.unlock, key)
            except Exception as error:  # Closing the session below releases it regardless.
                logger.warning(
                    "version_lease.unlock_failed", version=version, error_type=type(error).__name__
                )
    finally:
        try:
            await asyncio.to_thread(session.close)
        except Exception as error:  # A broken session has already released its lock.
            logger.debug("version_lease.close_failed", error_type=type(error).__name__)


class StandaloneVersionLease(ExecutorLease):
    """A shared hold on one version's lock for a process that runs no workflows.

    The operator sweep holds it for its run, on a dedicated connection with the executor
    lease's keepalives, acquire loop, 600 s budget and monitor. It holds no executor key
    and never registers the version. `ensure_held` raises once the monitor has found the
    lock lost; the monitor polls, so the loss can precede its detection by one interval.
    """

    def __init__(
        self,
        version: str,
        *,
        connect: SessionFactory,
        application_name: str = APPLICATION_NAME,
        probe_interval: float = PROBE_INTERVAL_SECONDS,
    ) -> None:
        """Bind the lease to `version` without connecting."""
        super().__init__(
            f"version-lease {version}",
            connect=connect,
            application_name=application_name,
            probe_interval=probe_interval,
            version=version,
        )

    def _take(self, session: LeaseSession) -> bool:
        """Take the version's lock shared."""
        key = self._version_key()
        self._refused_key = key
        taken = session.try_lock_shared(key)
        if taken:
            self._refused_key = None
        return taken

    def _holds_all(self, session: LeaseSession) -> bool:
        """Return whether `session` still holds the version's lock."""
        return session.holds(self._version_key())

    def _give_back(self, session: LeaseSession) -> None:
        """Release the version's shared lock."""
        session.unlock_shared(self._version_key())

    def _obstacle_subject(self) -> str:
        """Name the version this lease waits for."""
        return f"version {self.version!r}"

    def _version_key(self) -> int:
        """Return the version key, which a standalone lease always has."""
        if self.version_key is None:  # pragma: no cover - set by __init__
            raise RuntimeError("a standalone version lease has no version")
        return self.version_key

    def ensure_held(self) -> None:
        """Return only while the lock has not been found lost; call before every write.

        Raises:
            ExecutorLeaseLostError: The lease was never taken, or the monitor found it lost.
        """
        if not self.held:
            raise ExecutorLeaseLostError(
                f"the version lease on {self.version!r} is not held; stopping before the next write"
            )


def standalone_version_lease(settings: Settings, version: str) -> StandaloneVersionLease:
    """Return a standalone version lease on the configured DBOS system database."""
    conninfo = lease_conninfo(settings)
    name = f"{APPLICATION_NAME} operator version lease {version}"
    return StandaloneVersionLease(
        version, connect=lambda: connect_session(conninfo, application_name=name)
    )
