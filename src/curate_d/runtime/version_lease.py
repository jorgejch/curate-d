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

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from curate_d.config import Settings
from curate_d.runtime.lease import version_lease_key

__all__ = [
    "LockMode",
    "StandaloneVersionLease",
    "VersionHold",
    "hold_exclusive",
    "register_version",
    "registrations",
    "version_lease_key",
]

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


def register_version(settings: Settings, version: str) -> None:
    """Record `version` in `lease_versions`, keeping its first registration time.

    Raises:
        psycopg.Error: The database refused or could not be reached; the caller must not
            launch DBOS.
    """
    raise NotImplementedError


async def registrations(settings: Settings) -> Mapping[str, datetime]:
    """Return every registered version with its first registration time.

    Raises:
        psycopg.Error: The registry could not be read.
    """
    raise NotImplementedError


def hold_exclusive(settings: Settings, version: str) -> AbstractAsyncContextManager[VersionHold]:
    """Try once to hold `version`'s lock exclusively, on a dedicated connection.

    Yields a held `VersionHold` until the block exits, or a refused one naming the mode
    another session holds. The lock is released, and the connection closed, on every exit.

    Raises:
        psycopg.Error: Connecting or taking the lock failed.
    """
    raise NotImplementedError


class StandaloneVersionLease:
    """A shared hold on one version's lock for a process that runs no workflows.

    It uses a dedicated connection with the executor lease's keepalives, acquire loop,
    600 s budget and monitor, and never registers the version.
    """

    def __init__(self, settings: Settings, version: str) -> None:
        """Bind the lease to `version` without connecting."""
        self.version = version
        self._settings = settings

    @property
    def held(self) -> bool:
        """Return whether the lock is currently held."""
        raise NotImplementedError

    def acquire(self, *, timeout: float | None = None) -> None:
        """Take the shared lock, retrying within the acquire budget.

        Raises:
            ExecutorLeaseUnavailableError: The budget ran out first.
        """
        raise NotImplementedError

    def monitor(self, on_lost: Callable[[], None]) -> None:
        """Probe the held lock on a daemon thread, calling `on_lost` once if it is lost."""
        raise NotImplementedError

    def ensure_held(self) -> None:
        """Return only if the lock has not been found lost; call before every write.

        Raises:
            ExecutorLeaseLostError: The monitor found the lock lost.
        """
        raise NotImplementedError

    def release(self) -> None:
        """Stop monitoring, unlock and close the connection. Idempotent."""
        raise NotImplementedError
