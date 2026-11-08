"""Alembic environment.

The URL comes from application settings rather than alembic.ini, so migrations cannot be
pointed at a database the application itself is not configured to use.

Online runs, upgrades and downgrades alike, serialize under the migration lock: a session
advisory lock (`migration_lock_key`) taken on Alembic's own connection before anything
changes and released after Alembic's transaction commits or rolls back. A run that finds
the lock held polls for it up to `CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS`, then logs
`migration.lock_timeout` and exits 3 having changed nothing. A run that takes the lock
after another one finishes finds the schema current and exits 0.

A waiting run commits after every probe, so it holds no snapshot while it sleeps. A run
blocked inside `pg_advisory_lock` would hold one, and the holder's `DBOS.migrate` runs
`CREATE INDEX CONCURRENTLY`, which waits for every older snapshot: the two would deadlock
through the holder's client, out of sight of PostgreSQL's deadlock detector.

Under the lock, this module is the only migrator of the ledger database: it brings the
DBOS system schema and the ledger datasource's tracking table up to date through DBOS's
public API, then applies this project's revisions. Workers launch with migrations off and
only verify. Migrations 0005 and 0007 define views over DBOS's tables and 0008 and 0009
update its queue rows, so the DBOS step must come first. The migrations assume the system
schema lives in the ledger database: a separated system database is migrated here, but the
ledger database then has no `dbos` tables and 0005 fails.

The two DBOS steps run on connections of their own, so a lock session that dies while one
runs does not stop it. After each of them the run asks its own session whether it still
holds the lock; a lost lock, there or during Alembic's own step, logs `migration.lock_lost`
and exits 4 without applying a ledger revision. Both DBOS steps are idempotent, so the
repair is to run the migration again.

An upgrade on a schema ahead of its build (a revision this build does not know, written
by a newer build) logs `migration.schema_ahead`, changes nothing and exits 0, so an older
image can start on a newer, backward-compatible schema. Only `alembic upgrade` takes that
path: a downgrade from such a revision fails with Alembic's error, and `stamp`, `current`
and the other commands reach Alembic unchanged.

Offline mode (`--sql`) takes no lock and emits no DBOS SQL.
"""

import asyncio
import logging
import sys
import time
from collections.abc import Mapping
from logging.config import fileConfig
from typing import Final, Literal, LiteralString

from alembic import context
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from dbos import DBOS, AsyncSQLAlchemyDatasource
from sqlalchemy import Connection, pool, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, async_engine_from_config

from curate_d.config import Settings, get_settings
from curate_d.ledger.tables import metadata
from curate_d.observability import configure_logging, get_logger
from curate_d.runtime.datasource import TRACKING_SCHEMA
from curate_d.runtime.dbos_app import APPLICATION_NAME, SYSTEM_SCHEMA, system_database_url
from curate_d.runtime.lease import describe_holder, holds_lock, migration_lock_key

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata

logger = get_logger(__name__)

EXIT_LOCK_TIMEOUT: Final = 3
"""Another run held the migration lock past the timeout; nothing was changed."""

EXIT_LOCK_LOST: Final = 4
"""The lock's session ended mid-run; no ledger revision was applied. Run it again."""

LOCK_TIMEOUT_SETTING: Final = "CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS"

LOCK_POLL_INTERVAL_SECONDS: Final = 0.5
"""How long a waiting run sleeps between two probes of the migration lock."""

_TRY_LOCK: Final = text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))")
_UNLOCK: Final = text("SELECT pg_advisory_unlock(CAST(:key AS bigint))")

type Outcome = Literal["committed", "skipped"]


class MigrationLockTimeoutError(RuntimeError):
    """The migration lock stayed held past the timeout; nothing was changed."""


class MigrationLockLostError(RuntimeError):
    """The session holding the migration lock ended before the run finished."""


def _database_url() -> str:
    """Return the ledger database URL from application settings."""
    return str(get_settings().ledger_database_url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting, for review before an operator applies it.

    Offline mode takes no migration lock and emits no DBOS SQL: the DBOS system schema and
    the datasource tracking table are migrated separately, so run `dbos migrate` against
    the target, or an online `alembic upgrade`, before applying the emitted SQL.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    """Configure Alembic against an open connection and run pending migrations."""
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _holder(connection: AsyncConnection, key: int) -> str:
    """Describe the session holding `key`, or say that it is not visible."""

    def describe(sync_connection: Connection) -> str | None:
        """Run lease.py's holder query on the driver connection."""

        def execute(query: LiteralString, params: Mapping[str, int]) -> tuple[object, ...] | None:
            """Run one psycopg-style query and return its first row."""
            row = sync_connection.exec_driver_sql(query, dict(params)).fetchone()
            return None if row is None else tuple(row)

        return describe_holder(execute, key)

    return await connection.run_sync(describe) or "holder not visible"


async def _acquire_lock(connection: AsyncConnection, key: int, timeout_seconds: float) -> None:
    """Take the migration lock on `connection`, polling for up to `timeout_seconds`.

    Each probe commits at once, and so does each holder read, so no snapshot outlives a
    statement: a holder's `CREATE INDEX CONCURRENTLY` never waits on a waiter. The
    connection is left with no open transaction; the session lock survives the commit.

    Raises:
        MigrationLockTimeoutError: The lock stayed held past the timeout.
    """
    started = time.monotonic()
    waiting = False
    while True:
        taken = (await connection.execute(_TRY_LOCK, {"key": key})).scalar_one()
        await connection.commit()
        if taken:
            break
        if not waiting:
            waiting = True
            holder = await _holder(connection, key)
            await connection.commit()
            logger.warning("migration.lock_waiting", holder=holder, timeout_seconds=timeout_seconds)
        if time.monotonic() - started >= timeout_seconds:
            holder = await _holder(connection, key)
            await connection.commit()
            logger.error(
                "migration.lock_timeout",
                setting=LOCK_TIMEOUT_SETTING,
                timeout_seconds=timeout_seconds,
                holder=holder,
            )
            raise MigrationLockTimeoutError(
                f"the migration lock stayed held past {timeout_seconds}s by {holder}; "
                f"raise {LOCK_TIMEOUT_SETTING} or stop the holder"
            )
        await asyncio.sleep(LOCK_POLL_INTERVAL_SECONDS)
    logger.info("migration.lock_acquired", wait_seconds=round(time.monotonic() - started, 3))


async def _release_lock(
    connection: AsyncConnection, key: int, outcome: str, *, failing: bool
) -> None:
    """Release the migration lock on `connection` and log the run's `outcome`.

    A failed unlock is logged, not a leaked lock: the session lock dies with the
    connection, which the caller closes next.

    Args:
        failing: The migration raised and its exception is propagating. A failed unlock
            then must not replace it, so the unlock error is only logged.

    Raises:
        Exception: The unlock failed after a migration that did not raise.
    """
    if not connection.invalidated:
        try:
            # A no-op when nothing is open; ends a transaction a failure left aborted.
            await connection.rollback()
            await connection.execute(_UNLOCK, {"key": key})
            await connection.commit()
        except Exception as error:
            logger.warning("migration.lock_released", outcome=outcome, unlock_error=repr(error))
            if failing:
                return
            raise
    # An invalidated connection lost its session, and the server released the lock with it.
    logger.info("migration.lock_released", outcome=outcome)


def _lock_lost(step: str, cause: BaseException | None) -> MigrationLockLostError:
    """Log `migration.lock_lost` for `step` and return the error to raise.

    Args:
        step: The step that found the lock gone: the one just finished, whose writes on
            DBOS's connections may have run unprotected, or the ledger step itself.
        cause: The driver error that revealed it, or None when pg_locks said so.
    """
    logger.error("migration.lock_lost", step=step, cause=None if cause is None else repr(cause))
    return MigrationLockLostError(
        f"the migration lock's session ended during the {step} step; "
        "no ledger revision was applied, run the migration again"
    )


async def _verify_lock_held(connection: AsyncConnection, key: int, step: str) -> None:
    """Ask the lock's own session whether it still holds `key`, after `step` ran elsewhere.

    The DBOS steps write on connections of their own, so the lock session was idle
    meanwhile and may have been ended (an idle-session timeout, an operator, a reset). A
    dead session answers with a disconnect; a live one that lost the lock cannot happen for
    a session lock, but pg_locks is asked rather than assumed.

    Raises:
        MigrationLockLostError: The session is gone or no longer holds the lock.
    """

    def holds(sync_connection: Connection) -> bool:
        """Run lease.py's holder check on the driver connection."""

        def execute(query: LiteralString, params: Mapping[str, int]) -> tuple[object, ...] | None:
            """Run one psycopg-style query and return its first row."""
            row = sync_connection.exec_driver_sql(query, dict(params)).fetchone()
            return None if row is None else tuple(row)

        return holds_lock(execute, key)

    try:
        held = await connection.run_sync(holds)
        await connection.commit()
    except DBAPIError as error:
        if not error.connection_invalidated:
            raise
        raise _lock_lost(step, error) from error
    if not held:
        raise _lock_lost(step, None)


def _alembic_command() -> str:
    """Return the name of the Alembic command running this environment.

    A run without command-line options, as from a programmatic call, counts as `upgrade`.
    """
    command = getattr(config.cmd_opts, "cmd", None)
    if isinstance(command, tuple) and command:
        name = getattr(command[0], "__name__", None)
        if isinstance(name, str) and name:
            return name
    return "upgrade"


def _unknown_heads(sync_connection: Connection) -> tuple[list[str], list[str]]:
    """Return the database revisions this build does not know, and the build's heads."""
    database_revisions = MigrationContext.configure(sync_connection).get_current_heads()
    script = ScriptDirectory.from_config(config)
    unknown: list[str] = []
    for revision in database_revisions:
        try:
            known = script.get_revision(revision) is not None
        except CommandError:
            known = False
        if not known:
            unknown.append(revision)
    return unknown, list(script.get_heads())


async def _schema_ahead(connection: AsyncConnection) -> bool:
    """Return whether an upgrade finds the schema ahead of this build, logging it if so.

    Only `upgrade` takes the schema-ahead path. Every other command reaches Alembic: a
    downgrade from an unknown revision fails there, and `stamp --purge` is the operator's
    repair of `alembic_version`. The connection is left with no open transaction, so
    Alembic later begins its own.
    """
    unknown, build_heads = await connection.run_sync(_unknown_heads)
    await connection.rollback()
    command = _alembic_command()
    if not unknown or command != "upgrade":
        return False
    logger.warning(
        "migration.schema_ahead",
        database_revisions=unknown,
        build_heads=build_heads,
        command=command,
    )
    return True


async def _migrate_under_lock(connection: AsyncConnection, settings: Settings, key: int) -> Outcome:
    """Migrate the DBOS system schema, the tracking table and the ledger, in that order.

    `connection` holds the lock on `key`. The two DBOS steps run on connections of their
    own, so the lock is verified after each before the next step writes.

    Returns:
        `skipped` when the schema is ahead of this build and nothing ran, else `committed`.

    Raises:
        MigrationLockLostError: The lock's session ended during the run.
    """
    if await _schema_ahead(connection):
        return "skipped"
    DBOS.migrate(system_database_url(settings), schema=SYSTEM_SCHEMA)
    await _verify_lock_held(connection, key, "DBOS system schema")
    await AsyncSQLAlchemyDatasource.migrate(
        str(settings.ledger_database_url), schema=TRACKING_SCHEMA
    )
    await _verify_lock_held(connection, key, "datasource tracking table")
    try:
        await connection.run_sync(_run)
    except DBAPIError as error:
        if not error.connection_invalidated:
            raise
        # Alembic's transaction died with the session, so nothing of it was committed.
        raise _lock_lost("ledger revisions", error) from error
    return "committed"


async def run_migrations_online() -> None:
    """Take the migration lock, then migrate the DBOS schemas and this project's revisions.

    Raises:
        MigrationLockTimeoutError: Another run held the lock past the timeout.
        MigrationLockLostError: The lock's session ended during the run.
    """
    configure_logging(level=logging.INFO)
    settings = get_settings()
    key = migration_lock_key(APPLICATION_NAME)
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = str(settings.ledger_database_url)
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await _acquire_lock(connection, key, settings.migration_lock_timeout_seconds)
            outcome = "rolled back"
            failing = True
            try:
                outcome = await _migrate_under_lock(connection, settings, key)
                failing = False
            finally:
                await _release_lock(connection, key, outcome, failing=failing)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    try:
        asyncio.run(run_migrations_online())
    except MigrationLockTimeoutError:
        sys.exit(EXIT_LOCK_TIMEOUT)
    except MigrationLockLostError:
        sys.exit(EXIT_LOCK_LOST)
