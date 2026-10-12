"""Alembic against an empty database.

The rest of the suite runs on a database that is already migrated, so it cannot notice a
migration that only works when something else ran first. These tests create a database of
their own and drive the Alembic command line against it, as CI and a deployment do.
"""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from curate_d.config import Settings

pytestmark = pytest.mark.integration

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VIEWS = {"workflow_status_by_queue", "stalled_workflows", "failed_workflows"}
BASELINE = "0001_baseline"
# What each view over DBOS's tables answers with, in order.
VIEW_COLUMNS = {
    "workflow_status_by_queue": (
        "queue_name",
        "partition_key",
        "status",
        "application_version",
        "workflow_count",
        "oldest_created_at",
        "newest_updated_at",
    ),
    "stalled_workflows": (
        "workflow_id",
        "workflow_name",
        "queue_name",
        "partition_key",
        "status",
        "application_version",
        "executor_id",
        "recovery_attempts",
        "created_at",
        "updated_at",
    ),
    "failed_workflows": (
        "workflow_id",
        "workflow_name",
        "queue_name",
        "application_version",
        "recovery_attempts",
        "failed_at",
        "error_payload",
    ),
}


@pytest.fixture
async def empty_database(settings: Settings) -> AsyncIterator[str]:
    """Create a throwaway database on the ledger's server and drop it afterwards.

    Yields:
        Its URL, in the same form as the configured ledger URL.
    """
    ledger_url = make_url(str(settings.ledger_database_url))
    name = f"curate_d_migrations_{uuid4().hex[:12]}"
    admin = create_async_engine(ledger_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
        yield ledger_url.set(database=name).render_as_string(hide_password=False)
    finally:
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


def _alembic(database_url: str, *arguments: str) -> None:
    """Run the Alembic command line against one database, with the system schema beside it."""
    environment = dict(os.environ)
    environment["CURATE_D_LEDGER_DATABASE_URL"] = database_url
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


async def _views_and_head(database_url: str) -> tuple[set[str], str | None]:
    """Return the workflow views present and the recorded Alembic revision."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            views = {
                row[0]
                for row in await connection.execute(
                    text("SELECT viewname FROM pg_views WHERE schemaname = 'public'")
                )
            }
            # Alembic keeps the table at base and deletes its row, so no row means base.
            head = await connection.scalar(text("SELECT version_num FROM alembic_version"))
    finally:
        await engine.dispose()
    return views, head


async def _ledger_objects(database_url: str) -> set[str]:
    """Return the tables and functions in `public` that the migrations own."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                    "UNION ALL SELECT proname FROM pg_proc "
                    "WHERE pronamespace = 'public'::regnamespace AND proname LIKE 'curate_d_%'"
                )
            )
            # The version table outlives a full downgrade; PostGIS's table is the extension's.
            return {row[0] for row in rows} - {"alembic_version", "spatial_ref_sys"}
    finally:
        await engine.dispose()


async def test_upgrade_succeeds_on_an_empty_database(empty_database: str) -> None:
    """The DBOS system schema is created before the baseline's views that read it."""
    _alembic(empty_database, "upgrade", "head")

    views, head = await _views_and_head(empty_database)
    assert views >= VIEWS
    assert head == BASELINE


async def test_every_workflow_view_answers_with_its_columns(empty_database: str) -> None:
    """Each view over DBOS's tables answers a query on a fresh database, with its columns."""
    _alembic(empty_database, "upgrade", "head")

    engine = create_async_engine(empty_database)
    columns: dict[str, tuple[str, ...]] = {}
    try:
        async with engine.connect() as connection:
            for view in VIEW_COLUMNS:
                result = await connection.execute(text(f"SELECT * FROM {view} LIMIT 0"))
                columns[view] = tuple(result.keys())
    finally:
        await engine.dispose()
    assert columns == VIEW_COLUMNS


async def test_a_second_upgrade_is_a_no_op(empty_database: str) -> None:
    """Re-running against a database already at head changes nothing."""
    _alembic(empty_database, "upgrade", "head")
    first = await _views_and_head(empty_database)

    _alembic(empty_database, "upgrade", "head")

    assert await _views_and_head(empty_database) == first


async def test_a_full_downgrade_round_trips(empty_database: str) -> None:
    """The downgrade removes every ledger object, and the schema comes back from base."""
    _alembic(empty_database, "upgrade", "head")
    built = await _ledger_objects(empty_database)
    _alembic(empty_database, "downgrade", "base")

    views, head = await _views_and_head(empty_database)
    assert not views & VIEWS
    assert head is None
    assert await _ledger_objects(empty_database) == set()

    _alembic(empty_database, "upgrade", "head")
    assert (await _views_and_head(empty_database))[0] >= VIEWS
    assert await _ledger_objects(empty_database) == built
