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

The promotion fence keeps DBOS's latest version from moving backwards under a sweep. The
sweep holds it shared, taken once without waiting, across every fork and retirement
(`hold_fence_shared`); promotion, the only way latest moves backwards, holds it exclusively
across the change, waiting a bounded time (`hold_fence_exclusive`).

DBOS commits a fork on its own connection, not on the lock's, so a lock lost mid-sweep
does not undo the writes made under it. Each hold can therefore ask its own session
whether it still holds the lock (`VersionHold.still_held`, `FenceHold.still_held`); the
sweep asks before and after every fork and stops once either answer is no.

The locks are keyed by `version_lease_key` and `promotion_fence_key` in the database
holding the DBOS system tables, under the executor lease's connection settings and pooler
constraint (lease.py).
"""

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
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
    promotion_fence_key,
    version_lease_key,
)

__all__ = [
    "FenceHold",
    "LockMode",
    "PromotionFenceTimeoutError",
    "StandaloneVersionLease",
    "VersionHold",
    "VersionRegistryError",
    "hold_exclusive",
    "hold_fence_exclusive",
    "hold_fence_shared",
    "promotion_fence_key",
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


type HoldProbe = Callable[[], bool]
"""Asks the lock's own session whether it still holds the lock; may raise if it broke."""


async def _still_held(held: bool, probe: HoldProbe | None) -> bool:
    """Return whether a taken lock is still held, per its session; a broken session is a loss."""
    if not held or probe is None:
        return False
    try:
        return await asyncio.to_thread(probe)
    except Exception as error:  # Any failure to vouch for the lock is a loss.
        logger.warning("version_lease.probe_failed", error_type=type(error).__name__)
        return False


@dataclass(frozen=True)
class VersionHold:
    """The outcome of one attempt to hold a version's lock exclusively."""

    version: str
    held: bool
    holder_mode: LockMode | None = None
    """The mode another session holds the lock in, when refused and visible."""
    probe: HoldProbe | None = field(default=None, repr=False, compare=False)
    """Asks the holding session whether it still holds the lock; None when not held."""

    async def still_held(self) -> bool:
        """Return whether the lock is still held, asking its session.

        False when it was never taken, carries no probe, or the session cannot answer.
        """
        return await _still_held(self.held, self.probe)


@dataclass(frozen=True)
class FenceHold:
    """The outcome of one attempt to hold the promotion fence shared."""

    held: bool
    probe: HoldProbe | None = field(default=None, repr=False, compare=False)
    """Asks the holding session whether it still holds the fence; None when not held."""

    async def still_held(self) -> bool:
        """Return whether the fence is still held, asking its session.

        False when it was never taken, carries no probe, or the session cannot answer.
        """
        return await _still_held(self.held, self.probe)


class VersionRegistryError(RuntimeError):
    """`lease_versions` could not be written or read; the cause is chained."""


class PromotionFenceTimeoutError(RuntimeError):
    """The promotion fence stayed held, by a sweep, past the wait; nothing was promoted."""


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

    def holds(self, key: int) -> bool:
        """Return whether this session still holds the lock on `key`."""
        ...

    def close(self) -> None:
        """Close the session, releasing every lock it holds."""
        ...


class SharedFenceSession(Protocol):
    """A dedicated session able to take one lock shared without waiting."""

    def try_lock_shared(self, key: int) -> bool:
        """Take the shared lock on `key` without waiting; return whether it was taken."""
        ...

    def holds(self, key: int) -> bool:
        """Return whether this session still holds the lock on `key`."""
        ...

    def unlock_shared(self, key: int) -> None:
        """Release this session's shared lock on `key`."""
        ...

    def close(self) -> None:
        """Close the session, releasing every lock it holds."""
        ...


class ExclusiveFenceSession(Protocol):
    """A dedicated session able to wait a bounded time for one lock, exclusively."""

    def lock_within(self, key: int, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the lock on `key`; return whether it was taken."""
        ...

    def unlock(self, key: int) -> None:
        """Release this session's lock on `key`."""
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
    another session holds, if visible. A held one's `still_held` asks this session. On
    exit, attempt to release any acquired lock and close the session, suppressing cleanup
    errors.

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
            yield VersionHold(version, held=True, probe=lambda: session.holds(key))
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


def _connect_fence(settings: Settings, purpose: str) -> PostgresLeaseSession:
    """Open the dedicated session one side holds the promotion fence on."""
    return connect_session(
        lease_conninfo(settings), application_name=f"{APPLICATION_NAME} promotion fence {purpose}"
    )


@asynccontextmanager
async def hold_fence_shared(
    settings: Settings,
    *,
    connect: Callable[[], SharedFenceSession] | None = None,
    application_name: str = APPLICATION_NAME,
) -> AsyncIterator[FenceHold]:
    """Try once to hold the promotion fence shared, on a dedicated connection.

    Yields a `FenceHold` saying whether it was taken, and holds it until the block exits;
    a held one's `still_held` asks this session. A promotion holding the fence, or waiting
    for it, refuses it. On exit, attempt to release it and close the session, suppressing
    cleanup errors.

    Args:
        settings: Resolved configuration naming the system database.
        connect: Opens the session; a dedicated connection to the system database when
            omitted. Substituted by tests that must not connect.
        application_name: The DBOS application name, part of the lock key.

    Raises:
        psycopg.Error: Connecting or taking the lock failed.
    """
    key = promotion_fence_key(application_name)
    session = await asyncio.to_thread(connect or (lambda: _connect_fence(settings, "sweep")))
    try:
        if not await asyncio.to_thread(session.try_lock_shared, key):
            yield FenceHold(held=False)
            return
        try:
            yield FenceHold(held=True, probe=lambda: session.holds(key))
        finally:
            try:
                await asyncio.to_thread(session.unlock_shared, key)
            except Exception as error:  # Closing the session below releases it regardless.
                logger.warning("promotion_fence.unlock_failed", error_type=type(error).__name__)
    finally:
        try:
            await asyncio.to_thread(session.close)
        except Exception as error:  # A broken session has already released its lock.
            logger.debug("promotion_fence.close_failed", error_type=type(error).__name__)


@contextmanager
def hold_fence_exclusive(
    settings: Settings,
    *,
    timeout: float,
    connect: Callable[[], ExclusiveFenceSession] | None = None,
    application_name: str = APPLICATION_NAME,
) -> Iterator[None]:
    """Hold the promotion fence exclusively for the block, waiting up to `timeout` seconds.

    While it waits, a sweep's shared attempt is refused, so sweeps cannot starve it. On
    exit, attempt to release it and close the session, suppressing cleanup errors.

    Args:
        settings: Resolved configuration naming the system database.
        timeout: Seconds to wait for sweeps holding the fence to finish.
        connect: Opens the session; a dedicated connection to the system database when
            omitted. Substituted by tests that must not connect.
        application_name: The DBOS application name, part of the lock key.

    Raises:
        PromotionFenceTimeoutError: A sweep still held the fence after `timeout` seconds.
        psycopg.Error: Connecting or waiting for the lock failed.
    """
    key = promotion_fence_key(application_name)
    session = (connect or (lambda: _connect_fence(settings, "promote")))()
    try:
        if not session.lock_within(key, timeout):
            raise PromotionFenceTimeoutError(
                f"a recovery sweep still held the promotion fence after {timeout:g}s; "
                f"nothing was promoted. Retry once it finishes, or with a longer --fence-timeout"
            )
        try:
            yield
        finally:
            try:
                session.unlock(key)
            except Exception as error:  # Closing the session below releases it regardless.
                logger.warning("promotion_fence.unlock_failed", error_type=type(error).__name__)
    finally:
        try:
            session.close()
        except Exception as error:  # A broken session has already released its lock.
            logger.debug("promotion_fence.close_failed", error_type=type(error).__name__)


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
