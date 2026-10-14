"""Read and write persisted DBOS schedules while holding the schedule authority.

Schedule rows are shared by every worker and every version of the application, so one
worker at a time reconciles them. The authority is a session-level advisory lock on a
dedicated connection of its own (`runtime/lease.py`), and the reads and writes go through
DBOS's public schedule API, which commits each on DBOS's own pool. Lock and writes
therefore do not end together: a session that is lost mid-reconciliation releases the
lock while writes already sent carry on. The caller detects that afterwards through
`ScheduleAuthority.still_held` and a re-read of the latest version, and a periodic
reconciliation repairs what a stale writer left (`worker/schedules.py`).
"""

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import Final, Protocol

from dbos import DBOS, ScheduleInput

from curate_d.config import Settings
from curate_d.observability import get_logger
from curate_d.runtime.dbos_app import APPLICATION_NAME
from curate_d.runtime.lease import connect_session, lease_conninfo

logger = get_logger(__name__)

# How long a starting worker waits for another to finish reconciling. That takes a few
# statements; a longer wait means a holder is stuck, and startup fails visibly rather
# than serving without its schedules.
SCHEDULE_AUTHORITY_TIMEOUT_SECONDS: Final = 120.0

# Versioned, and distinct from the executor lease's domain: both are single-bigint
# advisory keys in one lock space, so only the domain keeps them from aliasing. Every
# build has taken this key, first as a session lock, then as a transaction lock, now as a
# session lock again; the two kinds on one key exclude each other, so mixed builds
# serialize during a rollout.
_DOMAIN: Final = b"curate-d/schedule-authority/v1"


class ScheduleAuthorityUnavailableError(RuntimeError):
    """Another worker held the schedule authority past the timeout."""


def schedule_authority_key(application_name: str) -> int:
    """Return the signed 64-bit advisory-lock key serializing one application's schedules."""
    material = b"\0".join((_DOMAIN, application_name.encode()))
    return int.from_bytes(sha256(material).digest()[:8], "big", signed=True)


class AuthoritySession(Protocol):
    """One dedicated database session able to wait for and hold an advisory lock."""

    def lock_within(self, key: int, timeout: float) -> bool:
        """Wait up to `timeout` seconds for the session lock on `key`; return whether taken."""
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


type AuthorityConnect = Callable[[], AuthoritySession]


@dataclass(frozen=True, slots=True)
class StoredSchedule:
    """A persisted schedule's name and the workflow it starts."""

    name: str
    workflow: str


class ScheduleAuthority:
    """The application's schedule rows, written through DBOS while the authority is held."""

    def __init__(self, session: AuthoritySession, key: int) -> None:
        self._session = session
        self._key = key

    def latest_version(self) -> str:
        """Return DBOS's latest application version.

        Raises:
            dbos.error.DBOSException: No version of this application is registered.
        """
        return DBOS.get_latest_application_version()["version_name"]

    def apply(self, schedules: Sequence[ScheduleInput]) -> None:
        """Create or replace each of `schedules`, keeping a stored one's status and last tick.

        Raises:
            dbos.error.DBOSException: A declaration is incomplete, its cron or timezone is
                invalid, its workflow is not registered, or its queue is not declared.
        """
        DBOS.apply_schedules(list(schedules))

    def stored(self) -> list[StoredSchedule]:
        """Return this application's stored schedules."""
        return [
            StoredSchedule(row["schedule_name"], row["workflow_name"])
            for row in DBOS.list_schedules()
        ]

    def delete(self, name: str) -> None:
        """Delete the schedule named `name`; a missing one is not an error."""
        DBOS.delete_schedule(name)

    def still_held(self) -> bool:
        """Return whether the session still holds the authority; a failed probe is a loss."""
        try:
            return self._session.holds(self._key)
        except Exception as error:  # A session that cannot vouch for the lock has lost it.
            logger.warning("schedule_authority.probe_failed", error_type=type(error).__name__)
            return False


@contextmanager
def schedule_authority(
    settings: Settings,
    *,
    timeout_seconds: float = SCHEDULE_AUTHORITY_TIMEOUT_SECONDS,
    connect: AuthorityConnect | None = None,
) -> Iterator[ScheduleAuthority]:
    """Hold the schedule authority on a dedicated session for the block.

    The lock is session-level, so it outlives no statement of the block and guards none
    of DBOS's writes, which commit on DBOS's own pool: check `ScheduleAuthority.still_held`
    after writing. The session is unlocked and closed on exit, whatever the block raised.

    Args:
        settings: Resolved configuration; names the database holding the DBOS tables.
        timeout_seconds: How long to wait for another holder to let go.
        connect: Opens the session; a dedicated connection to that database when omitted.

    Raises:
        ScheduleAuthorityUnavailableError: The lock was not free within `timeout_seconds`.
        psycopg.Error: The session could not be opened or failed while waiting.
    """
    key = schedule_authority_key(APPLICATION_NAME)
    if connect is None:
        conninfo = lease_conninfo(settings)
        name = f"{APPLICATION_NAME} schedule authority {DBOS.executor_id}"

        def connect() -> AuthoritySession:
            """Open a dedicated session on the DBOS system database."""
            return connect_session(conninfo, application_name=name)

    session = connect()
    locked = False
    try:
        if not session.lock_within(key, timeout_seconds):
            raise ScheduleAuthorityUnavailableError(
                f"another worker held the schedule authority for over {timeout_seconds:g}s: "
                f"{_describe_holder(session, key) or 'holder not visible'}"
            )
        locked = True
        yield ScheduleAuthority(session, key)
    finally:
        _release(session, key, locked=locked)


def _describe_holder(session: AuthoritySession, key: int) -> str | None:
    """Describe the session holding `key`, or None if that cannot be read."""
    try:
        return session.holder(key)
    except Exception as error:  # Only a diagnostic; the timeout is the failure reported.
        logger.debug("schedule_authority.holder_unreadable", error_type=type(error).__name__)
        return None


def _release(session: AuthoritySession, key: int, *, locked: bool) -> None:
    """Unlock `key` if it was taken, then close `session`, ignoring a broken session."""
    try:
        if locked:
            session.unlock(key)
    except Exception as error:  # Closing the session releases the lock regardless.
        logger.debug("schedule_authority.unlock_failed", error_type=type(error).__name__)
    finally:
        try:
            session.close()
        except Exception as error:  # A session that cannot close holds nothing.
            logger.debug("schedule_authority.close_failed", error_type=type(error).__name__)
