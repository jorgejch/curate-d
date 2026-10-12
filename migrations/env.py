"""Alembic environment.

The URL comes from application settings rather than alembic.ini, so migrations cannot be
pointed at a database the application itself is not configured to use.

Online runs first bring the DBOS system schema up to date, the same step `dbos migrate`
performs. The baseline revision defines views over DBOS's tables, which otherwise exist
only once a DBOS process has launched, so `alembic upgrade` on an empty database would
fail. The migrations assume the system schema lives in the ledger database: a separated
system database is migrated here, but the ledger database then has no `dbos` tables and
the baseline fails.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from dbos.cli.migration import run_dbos_database_migrations
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from curate_d.config import get_settings
from curate_d.ledger.tables import metadata
from curate_d.runtime.dbos_app import SYSTEM_SCHEMA, system_database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def _database_url() -> str:
    """Return the ledger database URL from application settings."""
    return str(get_settings().ledger_database_url)


def _migrate_dbos_system_schema() -> None:
    """Create or upgrade the DBOS system schema; a no-op when it is already current."""
    run_dbos_database_migrations(system_database_url(get_settings()), schema=SYSTEM_SCHEMA)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting, for review before an operator applies it.

    The DBOS system schema is not part of the emitted SQL; run `dbos migrate` against the
    target before applying it.
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


async def run_migrations_online() -> None:
    """Bring the DBOS system schema up to date, then apply this project's migrations."""
    _migrate_dbos_system_schema()
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
