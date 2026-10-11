"""Read and write persisted DBOS schedules inside the transaction that holds their authority.

Schedule rows are shared by every worker and every version of the application, so one
worker at a time reconciles them. The authority is a transaction-scoped advisory lock
(`pg_advisory_xact_lock`) on a DBOS system-database connection, and every read and write
of a reconciliation runs in that same transaction: the latest application version, the
upserts, the listing and the deletions. Authority and writes therefore end together. The
commit publishes the writes as it releases the lock; a backend that ends first — an
idle-in-transaction timeout on a stalled worker, a terminated session, a dropped network —
releases the lock and rolls every pending write back, and the worker's next statement or
its commit fails. A session lock on a connection of its own cannot give this: PostgreSQL
drops it with that session while writes on other connections carry on.

DBOS 3.2 has no public way to write schedules on a caller's connection, so this module
uses system-database internals: `_sys_db.engine`, `upsert_schedule`, `list_schedules`
and `delete_schedule` with `conn`, `get_queue`, `serializer`, `SystemDatabase._name_filter`,
`SystemSchema.application_versions`, and the registration lookups `DBOS.apply_schedules`
validates with. `tests/unit/runtime/test_schedule_authority.py` pins them; re-check them
on every DBOS upgrade.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from typing import Final
from zoneinfo import ZoneInfo

import sqlalchemy as sa
from dbos import ScheduleInput
from dbos._croniter import croniter  # type: ignore[attr-defined]
from dbos._dbos import _get_dbos_instance
from dbos._error import DBOSException
from dbos._registrations import DBOSFuncType, get_dbos_func_name, get_func_info
from dbos._schemas.system_database import SystemSchema
from dbos._sys_db import SystemDatabase, WorkflowSchedule
from dbos._utils import GlobalParams, generate_uuid
from psycopg.errors import LockNotAvailable
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from curate_d.runtime.dbos_app import APPLICATION_NAME

# How long a starting worker waits for another to finish reconciling. That takes a few
# statements; a longer wait means a holder is stuck, and startup fails visibly rather
# than serving without its schedules. It bounds every lock wait in the transaction.
SCHEDULE_AUTHORITY_TIMEOUT_SECONDS: Final = 120.0

# Versioned, and distinct from the executor lease's domain: both are single-bigint
# advisory keys in one lock space, so only the domain keeps them from aliasing. The key
# is the one the earlier session-lock build took, and a session lock and a transaction
# lock on one key exclude each other, so the two builds serialize during a rollout.
_DOMAIN: Final = b"curate-d/schedule-authority/v1"

_LOCK_TIMEOUT: Final = sa.text("SELECT set_config('lock_timeout', :timeout, true)")
_LOCK: Final = sa.text("SELECT pg_advisory_xact_lock(CAST(:key AS bigint))")
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


class ScheduleAuthorityUnavailableError(RuntimeError):
    """Another worker held the schedule authority past the timeout."""


def schedule_authority_key(application_name: str) -> int:
    """Return the signed 64-bit advisory-lock key serializing one application's schedules."""
    material = b"\0".join((_DOMAIN, application_name.encode()))
    return int.from_bytes(sha256(material).digest()[:8], "big", signed=True)


@dataclass(frozen=True, slots=True)
class StoredSchedule:
    """A persisted schedule's name and the workflow it starts."""

    name: str
    workflow: str


class ScheduleAuthority:
    """The application's schedule rows, read and written in the transaction holding authority."""

    def __init__(self, connection: sa.Connection) -> None:
        self._connection = connection
        self._sys_db: SystemDatabase = _get_dbos_instance()._sys_db

    @property
    def connection(self) -> sa.Connection:
        """Return the transaction's connection, whose backend holds the lock."""
        return self._connection

    def latest_version(self) -> str:
        """Return the latest application version, as `DBOS.get_latest_application_version` does.

        Raises:
            DBOSException: No version of this application is registered.
        """
        versions = SystemSchema.application_versions
        row = self._connection.execute(
            sa.select(versions.c.version_name)
            .where(SystemDatabase._name_filter(versions.c.application_name, self._sys_db.app_name))
            .order_by(versions.c.version_timestamp.desc())
            .limit(1)
        ).first()
        if row is None:
            raise DBOSException("No application versions found")
        return str(row[0])

    def apply(self, schedules: Sequence[ScheduleInput]) -> None:
        """Validate `schedules` and create or replace each, as `DBOS.apply_schedules` does.

        Raises:
            DBOSException: A declaration is incomplete, its cron or timezone is invalid,
                its workflow is not registered, or its queue is not declared.
        """
        rows = [stored_form(index, entry) for index, entry in enumerate(schedules)]
        for row in rows:
            self._sys_db.upsert_schedule(row, conn=self._connection)

    def stored(self) -> list[StoredSchedule]:
        """Return this application's stored schedules plus unclaimed ones."""
        return [
            StoredSchedule(row["schedule_name"], row["workflow_name"])
            for row in self._sys_db.list_schedules(conn=self._connection)
        ]

    def delete(self, name: str) -> None:
        """Delete the schedule named `name`; a missing one is not an error."""
        self._sys_db.delete_schedule(name, conn=self._connection)


def stored_form(index: int, entry: ScheduleInput) -> WorkflowSchedule:
    """Validate one declaration and build its row exactly as `DBOS.apply_schedules` (3.2) does.

    `DBOS.apply_schedules` writes in a transaction of its own, so it cannot run under the
    authority. Pinned against it in `tests/unit/runtime/test_schedule_authority.py`.

    Raises:
        DBOSException: As `DBOS.apply_schedules` raises for the same entry.
    """
    for field in ("schedule_name", "workflow_fn", "schedule"):
        if field not in entry:
            raise DBOSException(f"Schedule entry {index} is missing required field '{field}'")
    cron = entry["schedule"]
    if not croniter.is_valid(cron, second_at_beginning=True):
        raise DBOSException(f"Invalid cron schedule: '{cron}'")
    cron_timezone = entry.get("cron_timezone")
    if cron_timezone is not None:
        try:
            ZoneInfo(cron_timezone)
        except Exception:
            raise DBOSException(f"Invalid timezone: '{cron_timezone}'") from None
    dbos = _get_dbos_instance()
    workflow_fn = entry["workflow_fn"]
    workflow_name = get_dbos_func_name(workflow_fn)
    if workflow_name not in dbos._registry.workflow_info_map:
        raise DBOSException(f"Workflow function '{workflow_name}' is not registered")
    info = get_func_info(workflow_fn)
    if info is not None and info.func_type == DBOSFuncType.Instance:
        raise DBOSException("Configured instance methods cannot be used as scheduled workflows")
    workflow_class_name = (
        info.class_info.registered_name
        if info is not None and info.class_info is not None and info.func_type == DBOSFuncType.Class
        else None
    )
    queue_name = entry.get("queue_name")
    if (
        queue_name is not None
        and queue_name not in dbos._registry.internal_queue_map
        and dbos._sys_db.get_queue(queue_name) is None
    ):
        raise DBOSException(
            f"Queue '{queue_name}' is not declared. Please create the queue before using it "
            f"in a schedule."
        )
    return WorkflowSchedule(
        schedule_id=generate_uuid(),
        schedule_name=entry["schedule_name"],
        workflow_name=workflow_name,
        workflow_class_name=workflow_class_name,
        schedule=cron,
        status="ACTIVE",
        context=dbos._sys_db.serializer.serialize(entry.get("context")),
        last_fired_at=None,
        automatic_backfill=entry.get("automatic_backfill", False),
        cron_timezone=cron_timezone,
        queue_name=queue_name,
        application_name=GlobalParams.app_name,
    )


@contextmanager
def schedule_authority(
    *, timeout_seconds: float = SCHEDULE_AUTHORITY_TIMEOUT_SECONDS
) -> Iterator[ScheduleAuthority]:
    """Open a system-database transaction holding the schedule authority, committing on exit.

    The lock waits at most `timeout_seconds` (PostgreSQL's `lock_timeout`, local to the
    transaction), as does every other lock wait in the block. An exception in the block
    rolls everything back. Losing the backend at any point loses the writes with the lock:
    the failure surfaces from the next statement or the commit.

    Raises:
        ScheduleAuthorityUnavailableError: The lock was not free within `timeout_seconds`.
    """
    key = schedule_authority_key(APPLICATION_NAME)
    engine = _get_dbos_instance()._sys_db.engine
    with engine.begin() as connection:
        connection.execute(_LOCK_TIMEOUT, {"timeout": f"{max(1, round(timeout_seconds * 1000))}ms"})
        try:
            connection.execute(_LOCK, {"key": key})
        except DBAPIError as error:
            if not isinstance(error.orig, LockNotAvailable):
                raise
            raise ScheduleAuthorityUnavailableError(
                f"another worker held the schedule authority for over {timeout_seconds:g}s: "
                f"{_describe_holder(engine, key) or 'holder not visible'}"
            ) from None
        yield ScheduleAuthority(connection)


def _describe_holder(engine: sa.Engine, key: int) -> str | None:
    """Describe the backend holding `key` by pid, application, address and transaction start."""
    unsigned = key & 0xFFFF_FFFF_FFFF_FFFF
    try:
        with engine.connect() as connection:
            row = connection.execute(
                _HOLDER, {"high": unsigned >> 32, "low": unsigned & 0xFFFF_FFFF}
            ).first()
    except SQLAlchemyError:
        return None
    if row is None:
        return None
    pid, application, address, started = row
    return f"pid {pid} ({application or 'unknown'} from {address or 'local'}, since {started})"
