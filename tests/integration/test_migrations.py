"""Alembic against an empty database.

The rest of the suite runs on a database that is already migrated, so it cannot notice a
migration that only works when something else ran first. These tests create a database of
their own and drive the Alembic command line against it, as CI and a deployment do.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from dbos import DBOS
from dbos import error as dbos_error
from pydantic import PostgresDsn
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from curate_d.config import Settings
from curate_d.domain import derive_destination_id
from curate_d.runtime import dbos_app
from curate_d.runtime.lease import connect_session, libpq_conninfo, migration_lock_key

pytestmark = pytest.mark.integration

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
VIEWS = {"workflow_status_by_queue", "stalled_workflows", "failed_workflows"}


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


def _alembic_environment(database_url: str, **overrides: str) -> dict[str, str]:
    """Return this process's environment pointed at one database, with `overrides` set."""
    environment = dict(os.environ)
    environment["CURATE_D_LEDGER_DATABASE_URL"] = database_url
    environment.update(overrides)
    return environment


def _run_alembic(
    database_url: str,
    *arguments: str,
    environment: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the Alembic command line against one database and return how it ended.

    Raises:
        subprocess.TimeoutExpired: The run outlasted `timeout` seconds; it was killed.
    """
    return subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=REPOSITORY_ROOT,
        env=dict(environment or _alembic_environment(database_url)),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _alembic(database_url: str, *arguments: str) -> None:
    """Run the Alembic command line against one database, with the system schema beside it."""
    completed = _run_alembic(database_url, *arguments)
    assert completed.returncode == 0, completed.stderr


async def _views_and_head(database_url: str) -> tuple[set[str], str | None]:
    """The workflow views present and the recorded Alembic revision."""
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


async def test_upgrade_succeeds_on_an_empty_database(empty_database: str) -> None:
    """The DBOS system schema is created before the views that read it."""
    _alembic(empty_database, "upgrade", "head")

    views, head = await _views_and_head(empty_database)
    assert views >= VIEWS
    assert head is not None


async def test_a_second_upgrade_is_a_no_op(empty_database: str) -> None:
    """Re-running against a database already at head changes nothing."""
    _alembic(empty_database, "upgrade", "head")
    first = await _views_and_head(empty_database)

    _alembic(empty_database, "upgrade", "head")

    assert await _views_and_head(empty_database) == first


async def test_a_full_downgrade_round_trips(empty_database: str) -> None:
    """Every downgrade applies, and the schema comes back from base."""
    _alembic(empty_database, "upgrade", "head")
    _alembic(empty_database, "downgrade", "base")

    views, head = await _views_and_head(empty_database)
    assert not views & VIEWS
    assert head is None

    _alembic(empty_database, "upgrade", "head")
    assert (await _views_and_head(empty_database))[0] >= VIEWS


def _script_directory() -> ScriptDirectory:
    """Return this repository's Alembic migration chain."""
    config = Config(str(REPOSITORY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPOSITORY_ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def test_the_chain_has_a_single_head() -> None:
    """Every revision descends from the one before: no two migrations share a parent."""
    assert len(_script_directory().get_heads()) == 1


# 0022 adds what a dry run evaluated with and the unselected outcome.
BEFORE_DRY_RUN_DRAFTS = "0021_staging_reservations"
DRY_RUN_DRAFTS = "0022_dry_run_drafts"
_DRY_RUN_COLUMNS = {
    "policy_source",
    "policy_version",
    "policy_rules",
    "templates_draft",
    "policy_document",
    "templates_document",
}


async def _columns(database_url: str, table: str) -> set[str]:
    """Return the names of a public table's columns."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND table_name = :table"
                ),
                {"table": table},
            )
            return {row[0] for row in rows}
    finally:
        await engine.dispose()


async def _execute(database_url: str, statement: str, **parameters: object) -> None:
    """Run one statement in its own transaction."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement), parameters)
    finally:
        await engine.dispose()


_INSERT_RUN = (
    "INSERT INTO dry_runs (id, started_at, template_version, taxonomy_version) "
    "VALUES (:id, now(), 'seed-1', 1)"
)
_INSERT_UNSELECTED = (
    "INSERT INTO dry_run_items (run_id, content_object_id, destination_name, outcome, paths, "
    "rules) VALUES (:run_id, :content_id, '', 'unselected', '/a.jpg', '[]'::jsonb)"
)


async def test_dry_run_drafts_round_trip(empty_database: str) -> None:
    """The columns and the unselected outcome come and go; downgrade waits on unselected items."""
    _alembic(empty_database, "upgrade", BEFORE_DRY_RUN_DRAFTS)
    assert not await _columns(empty_database, "dry_runs") & _DRY_RUN_COLUMNS
    assert "rules" not in await _columns(empty_database, "dry_run_items")

    _alembic(empty_database, "upgrade", DRY_RUN_DRAFTS)
    assert await _columns(empty_database, "dry_runs") >= _DRY_RUN_COLUMNS
    assert "rules" in await _columns(empty_database, "dry_run_items")
    run_id = uuid4()
    await _execute(empty_database, _INSERT_RUN, id=run_id)
    await _execute(empty_database, _INSERT_UNSELECTED, run_id=run_id, content_id=uuid4())

    refused = _run_alembic(empty_database, "downgrade", BEFORE_DRY_RUN_DRAFTS)
    assert refused.returncode != 0
    assert "DELETE FROM dry_run_items WHERE outcome = 'unselected'" in refused.stderr
    assert (await _views_and_head(empty_database))[1] == DRY_RUN_DRAFTS

    await _execute(empty_database, "DELETE FROM dry_run_items WHERE outcome = 'unselected'")
    _alembic(empty_database, "downgrade", BEFORE_DRY_RUN_DRAFTS)
    assert not await _columns(empty_database, "dry_runs") & _DRY_RUN_COLUMNS
    assert "rules" not in await _columns(empty_database, "dry_run_items")
    assert (await _views_and_head(empty_database))[1] == BEFORE_DRY_RUN_DRAFTS


# 0008 moves the partitioned queues' limit from partition_concurrency to its per-worker
# form. Each test seeds dbos.queues in its own throwaway database at the revision before.
BEFORE_PARTITION_WORKER = "0007_failed_workflows_output"
PARTITION_WORKER = "0008_partition_worker_limits"

QueueLimits = tuple[int | None, int | None, bool]


async def _seed_queues(database_url: str, rows: dict[str, tuple[int | None, int | None]]) -> None:
    """Insert dbos.queues rows as `name: (partition_concurrency, partition_worker_concurrency)`."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            for name, (global_limit, worker_limit) in rows.items():
                await connection.execute(
                    text(
                        "INSERT INTO dbos.queues (name, partition_concurrency, "
                        "partition_worker_concurrency, partition_queue) "
                        "VALUES (:name, :global_limit, :worker_limit, true)"
                    ),
                    {"name": name, "global_limit": global_limit, "worker_limit": worker_limit},
                )
    finally:
        await engine.dispose()


async def _queue_limits(database_url: str) -> dict[str, QueueLimits]:
    """Return each dbos.queues row as `name: (global, per-worker, partition_queue)`."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT name, partition_concurrency, partition_worker_concurrency, "
                    "partition_queue FROM dbos.queues"
                )
            )
            return {row[0]: (row[1], row[2], row[3]) for row in rows}
    finally:
        await engine.dispose()


LEGACY_QUEUES: dict[str, tuple[int | None, int | None]] = {
    # Registered by the old code with its default.
    "scan": (4, None),
    # Raised at runtime by the control plane; the adjusted value must survive.
    "transfer-small": (9, None),
    # Already per worker, as the new code registers it.
    "transfer-large": (None, 2),
    # Not a provider-partitioned queue of this application.
    "fingerprint": (3, None),
}


async def test_partition_worker_upgrade_moves_global_limits_to_per_worker(
    empty_database: str,
) -> None:
    """Legacy rows move across keeping their value; converted and unrelated rows stay."""
    _alembic(empty_database, "upgrade", BEFORE_PARTITION_WORKER)
    await _seed_queues(empty_database, LEGACY_QUEUES)

    _alembic(empty_database, "upgrade", PARTITION_WORKER)

    assert await _queue_limits(empty_database) == {
        "scan": (None, 4, True),
        "transfer-small": (None, 9, True),
        "transfer-large": (None, 2, True),
        "fingerprint": (3, None, True),
    }


async def test_partition_worker_upgrade_leaves_a_row_with_both_limits(
    empty_database: str,
) -> None:
    """A row with both limits set was configured deliberately and is not rewritten."""
    _alembic(empty_database, "upgrade", BEFORE_PARTITION_WORKER)
    await _seed_queues(empty_database, {"scan": (6, 2)})

    _alembic(empty_database, "upgrade", PARTITION_WORKER)

    assert await _queue_limits(empty_database) == {"scan": (6, 2, True)}


async def test_partition_worker_upgrade_is_a_no_op_without_queue_rows(
    empty_database: str,
) -> None:
    """A database no worker has registered queues in upgrades with nothing to move."""
    _alembic(empty_database, "upgrade", BEFORE_PARTITION_WORKER)

    _alembic(empty_database, "upgrade", PARTITION_WORKER)

    assert await _queue_limits(empty_database) == {}


async def test_partition_worker_downgrade_restores_global_limits(empty_database: str) -> None:
    """Downgrade moves per-worker limits on the partitioned queues back to global."""
    _alembic(empty_database, "upgrade", BEFORE_PARTITION_WORKER)
    await _seed_queues(empty_database, LEGACY_QUEUES)
    _alembic(empty_database, "upgrade", PARTITION_WORKER)

    _alembic(empty_database, "downgrade", BEFORE_PARTITION_WORKER)

    assert await _queue_limits(empty_database) == {
        "scan": (4, None, True),
        "transfer-small": (9, None, True),
        "transfer-large": (2, None, True),
        "fingerprint": (3, None, True),
    }


BEFORE_TRANSFER_CAPS = PARTITION_WORKER
TRANSFER_CAPS = "0009_transfer_worker_caps"

TransferLimits = tuple[int | None, int | None]


async def _seed_transfer_queues(database_url: str, rows: dict[str, TransferLimits]) -> None:
    """Insert dbos.queues rows as `name: (partition_worker_concurrency, worker_concurrency)`."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            for name, (partition_limit, worker_limit) in rows.items():
                await connection.execute(
                    text(
                        "INSERT INTO dbos.queues (name, partition_worker_concurrency, "
                        "worker_concurrency, partition_queue) "
                        "VALUES (:name, :partition_limit, :worker_limit, true)"
                    ),
                    {
                        "name": name,
                        "partition_limit": partition_limit,
                        "worker_limit": worker_limit,
                    },
                )
    finally:
        await engine.dispose()


async def _transfer_limits(database_url: str) -> dict[str, TransferLimits]:
    """Return each dbos.queues row as `name: (partition_worker, worker_concurrency)`."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT name, partition_worker_concurrency, worker_concurrency FROM dbos.queues"
                )
            )
            return {row[0]: (row[1], row[2]) for row in rows}
    finally:
        await engine.dispose()


async def test_transfer_caps_upgrade_sets_the_minimum_workable_limits(
    empty_database: str,
) -> None:
    """Default transfer rows drop to one per partition and one per worker."""
    _alembic(empty_database, "upgrade", BEFORE_TRANSFER_CAPS)
    await _seed_transfer_queues(
        empty_database,
        {"transfer-small": (4, None), "transfer-large": (1, None), "fingerprint": (None, 2)},
    )

    _alembic(empty_database, "upgrade", TRANSFER_CAPS)

    assert await _transfer_limits(empty_database) == {
        "transfer-small": (1, 1),
        "transfer-large": (1, 1),
        "fingerprint": (None, 2),
    }


async def test_transfer_caps_upgrade_keeps_runtime_adjustments(empty_database: str) -> None:
    """An adjusted partition limit survives and the new cap is raised to meet it."""
    _alembic(empty_database, "upgrade", BEFORE_TRANSFER_CAPS)
    await _seed_transfer_queues(
        empty_database, {"transfer-small": (9, None), "transfer-large": (2, 5)}
    )

    _alembic(empty_database, "upgrade", TRANSFER_CAPS)

    assert await _transfer_limits(empty_database) == {
        "transfer-small": (9, 9),
        "transfer-large": (2, 5),
    }


async def test_transfer_caps_upgrade_is_a_no_op_without_queue_rows(
    empty_database: str,
) -> None:
    """A database no worker has registered queues in upgrades with nothing to change."""
    _alembic(empty_database, "upgrade", BEFORE_TRANSFER_CAPS)

    _alembic(empty_database, "upgrade", TRANSFER_CAPS)

    assert await _transfer_limits(empty_database) == {}


async def test_transfer_caps_downgrade_restores_the_uncapped_limits(
    empty_database: str,
) -> None:
    """Downgrade clears the worker caps and returns transfer-small to four per partition."""
    _alembic(empty_database, "upgrade", BEFORE_TRANSFER_CAPS)
    await _seed_transfer_queues(
        empty_database, {"transfer-small": (4, None), "transfer-large": (1, None)}
    )
    _alembic(empty_database, "upgrade", TRANSFER_CAPS)

    _alembic(empty_database, "downgrade", BEFORE_TRANSFER_CAPS)

    assert await _transfer_limits(empty_database) == {
        "transfer-small": (4, None),
        "transfer-large": (1, None),
    }


# 0023 keys placements on their destination identity, backfilled from recorded evidence.
BEFORE_DESTINATION_IDENTITY = "0022_dry_run_drafts"
DESTINATION_IDENTITY = "0023_destination_identity"
_T0 = datetime(2026, 10, 1, tzinfo=UTC)
_ACCOUNT_1 = derive_destination_id("pcloud", "acct-1", "/Library")
_ACCOUNT_2 = derive_destination_id("pcloud", "acct-2", "/Library")


def _alembic_fails(database_url: str, *arguments: str) -> str:
    """Run the Alembic command line expecting it to fail; return what it reported."""
    completed = _run_alembic(database_url, *arguments)
    assert completed.returncode != 0, completed.stdout
    return completed.stderr


def _document(*destinations: tuple[str, str, str]) -> str:
    """Return a policy document with these (name, account, root) destinations, as JSON."""
    return json.dumps(
        {
            "destinations": [
                {
                    "name": name,
                    "provider": "pcloud",
                    "account": account,
                    "root_path": root,
                    "profile": "pcloud",
                }
                for name, account, root in destinations
            ],
            "rules": [],
            "retention": [],
        }
    )


async def _seed_policies(database_url: str, *documents: str) -> None:
    """Store these policy documents as versions 1, 2, ... a day apart, the last one active."""
    for version, document in enumerate(documents, start=1):
        await _execute(
            database_url,
            "INSERT INTO configurations (kind, version, document, active, created_at, "
            "created_by) VALUES ('policy', :version, CAST(:document AS jsonb), :active, :at, "
            "'test')",
            version=version,
            document=document,
            active=version == len(documents),
            at=_T0 + timedelta(days=version - 1),
        )


async def _seed_placement(
    database_url: str,
    *,
    name: str,
    locator: str,
    created: datetime,
    state: str = "present",
    content_id: UUID | None = None,
    started: tuple[datetime, dict[str, object]] | None = None,
) -> UUID:
    """Seed a name-keyed placement and, with `started`, its placement_started entry.

    Returns:
        The placement's identifier.
    """
    placement_id = uuid4()
    if content_id is None:
        content_id = uuid4()
        await _execute(
            database_url,
            "INSERT INTO content_objects (id, fingerprint_algorithm, fingerprint_digest, "
            "size_bytes, media_type) VALUES (:id, 'sha256', :digest, 1, 'image/jpeg')",
            id=content_id,
            digest=content_id.hex * 2,
        )
    await _execute(
        database_url,
        "INSERT INTO placements (id, content_object_id, destination_name, locator, "
        "template_version, state, verified_at, created_at) VALUES (:id, :content, :name, "
        ":locator, 'seed-1', :state, :created, :created)",
        id=placement_id,
        content=content_id,
        name=name,
        locator=locator,
        state=state,
        created=created,
    )
    if started is not None:
        at, details = started
        await _execute(
            database_url,
            "INSERT INTO audit_entries (occurred_at, entry_type, content_object_id, "
            "placement_id, decided_by, details) VALUES (:at, 'placement_started', :content, "
            ":placement, 'placement', CAST(:details AS jsonb))",
            at=at,
            content=content_id,
            placement=placement_id,
            details=json.dumps(details),
        )
    return placement_id


async def _rows(database_url: str, statement: str) -> list[tuple[object, ...]]:
    """Return the rows one query reads."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return [tuple(row) for row in await connection.execute(text(statement))]
    finally:
        await engine.dispose()


async def _seed_redefinition(database_url: str) -> dict[str, UUID]:
    """Seed `lib` at account 1 in policy v1, at account 2 in v2, and placements under it.

    Returns:
        Placement ids by case. `recorded` and `named` started after v2 but recorded
        account 1, by location and by policy version; `between` was created under v1
        with no start recorded; `after` was created under v2 with none.
    """
    await _seed_policies(
        database_url,
        _document(("lib", "acct-1", "/Library/")),
        _document(("lib", "acct-2", "/Library")),
    )
    late = _T0 + timedelta(days=2)
    location = {
        "destination_provider": "pcloud",
        "destination_account": "acct-1",
        "destination_root": "/Library",
        "policy_version": 2,
    }
    return {
        "recorded": await _seed_placement(
            database_url,
            name="lib",
            locator="/Library/r.jpg",
            created=late,
            started=(late, location),
        ),
        "named": await _seed_placement(
            database_url,
            name="lib",
            locator="/Library/n.jpg",
            created=late,
            started=(late, {"policy_version": 1}),
        ),
        "between": await _seed_placement(
            database_url, name="lib", locator="/Library/b.jpg", created=_T0 + timedelta(hours=1)
        ),
        "after": await _seed_placement(
            database_url, name="lib", locator="/Library/a.jpg", created=late
        ),
    }


async def test_destination_identity_backfills_from_the_evidence_and_round_trips(
    empty_database: str,
) -> None:
    """Recorded location, then named version, then version in force decide; ids are kept."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    ids = await _seed_redefinition(empty_database)

    _alembic(empty_database, "upgrade", DESTINATION_IDENTITY)

    rows = await _rows(empty_database, "SELECT id, destination_id FROM placements")
    assert {row[0]: row[1] for row in rows} == {
        ids["recorded"]: _ACCOUNT_1,
        ids["named"]: _ACCOUNT_1,
        ids["between"]: _ACCOUNT_1,
        ids["after"]: _ACCOUNT_2,
    }
    assert await _rows(
        empty_database, "SELECT id, provider, account, root_path FROM destinations ORDER BY account"
    ) == [
        (_ACCOUNT_1, "pcloud", "acct-1", "/Library"),
        (_ACCOUNT_2, "pcloud", "acct-2", "/Library"),
    ]
    constraints = {
        row[0]
        for row in await _rows(
            empty_database,
            "SELECT conname FROM pg_constraint WHERE conrelid = 'placements'::regclass",
        )
    }
    assert constraints >= {
        "uq_placements_content_object_id_destination_id",
        "uq_placements_destination_id_locator",
        "fk_placements_destination_id_destinations",
    }
    assert not constraints & {
        "uq_placements_content_object_id_destination_name",
        "uq_placements_destination_name_locator",
    }

    _alembic(empty_database, "downgrade", BEFORE_DESTINATION_IDENTITY)

    assert "destination_id" not in await _columns(empty_database, "placements")
    assert len(await _rows(empty_database, "SELECT id FROM placements")) == len(ids)
    assert (await _views_and_head(empty_database))[1] == BEFORE_DESTINATION_IDENTITY


async def test_a_placement_under_a_name_in_no_version_stops_the_upgrade(
    empty_database: str,
) -> None:
    """Nothing is guessed: the upgrade fails naming the placement, and nothing changes."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    await _seed_redefinition(empty_database)
    ghost = await _seed_placement(
        empty_database, name="ghost", locator="/Library/g.jpg", created=_T0 + timedelta(days=3)
    )

    reported = _alembic_fails(empty_database, "upgrade", DESTINATION_IDENTITY)

    assert "UnresolvedPlacementsError" in reported
    assert str(ghost) in reported
    assert (await _views_and_head(empty_database))[1] == BEFORE_DESTINATION_IDENTITY
    assert "destination_id" not in await _columns(empty_database, "placements")


async def test_a_root_that_does_not_normalize_is_not_resolved_from_an_earlier_version(
    empty_database: str,
) -> None:
    """A copy sent to an unnormalizable root is reported, not given version 1's account."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    await _seed_policies(
        empty_database,
        _document(("lib", "acct-1", "/Library")),
        _document(("lib", "acct-2", "/Old/../New")),
        _document(("lib", "acct-2", "/New")),
    )
    under_v2 = _T0 + timedelta(days=1, hours=1)
    in_force = await _seed_placement(
        empty_database, name="lib", locator="/Library/v.jpg", created=under_v2
    )
    recorded = await _seed_placement(
        empty_database,
        name="lib",
        locator="/Library/r.jpg",
        created=_T0 + timedelta(days=3),
        started=(
            _T0 + timedelta(days=3),
            {
                "destination_provider": "pcloud",
                "destination_account": "acct-2",
                "destination_root": "/Old/../New",
            },
        ),
    )

    reported = _alembic_fails(empty_database, "upgrade", DESTINATION_IDENTITY)

    assert "UnresolvedPlacementsError" in reported
    assert str(in_force) in reported
    assert str(recorded) in reported
    assert (await _views_and_head(empty_database))[1] == BEFORE_DESTINATION_IDENTITY


async def test_deleting_an_unresolved_placement_lets_the_upgrade_through(
    empty_database: str,
) -> None:
    """Failing an unresolved placement still refuses; deleting it lets the upgrade through."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    ids = await _seed_redefinition(empty_database)
    late = _T0 + timedelta(days=3)
    ghost = await _seed_placement(
        empty_database, name="ghost", locator="/Library/g.jpg", created=late, started=(late, {})
    )
    await _execute(
        empty_database,
        "UPDATE placements SET state = 'failed', failure_reason = 'unresolved' WHERE id = :id",
        id=ghost,
    )
    assert str(ghost) in _alembic_fails(empty_database, "upgrade", DESTINATION_IDENTITY)

    await _execute(empty_database, "DELETE FROM placements WHERE id = :id", id=ghost)
    _alembic(empty_database, "upgrade", DESTINATION_IDENTITY)

    assert {row[0] for row in await _rows(empty_database, "SELECT id FROM placements")} == set(
        ids.values()
    )
    assert await _rows(
        empty_database,
        f"SELECT count(*) FROM audit_entries WHERE placement_id = '{ghost}'",
    ) == [(1,)]


async def test_two_placements_resolving_to_one_identity_and_locator_stop_the_upgrade(
    empty_database: str,
) -> None:
    """Two names a past policy gave one place clash on its locator; the upgrade names both."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    await _seed_policies(
        empty_database,
        _document(("a", "acct-1", "/Library"), ("b", "acct-1", "/Library/")),
        _document(("a", "acct-1", "/Library")),
    )
    created = _T0 + timedelta(hours=1)
    first = await _seed_placement(
        empty_database, name="a", locator="/Library/x.jpg", created=created
    )
    second = await _seed_placement(
        empty_database, name="b", locator="/Library/x.jpg", created=created
    )

    reported = _alembic_fails(empty_database, "upgrade", DESTINATION_IDENTITY)

    assert "SharedIdentityError" in reported
    assert str(first) in reported
    assert str(second) in reported
    assert (await _views_and_head(empty_database))[1] == BEFORE_DESTINATION_IDENTITY


async def test_a_placement_still_copying_stops_the_upgrade(empty_database: str) -> None:
    """Its workflow is keyed by the name: the deploy drains first, and nothing changes."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    await _seed_policies(empty_database, _document(("lib", "acct-1", "/Library")))
    copying = await _seed_placement(
        empty_database,
        name="lib",
        locator="/Library/c.jpg",
        created=_T0 + timedelta(hours=1),
        state="copying",
    )

    reported = _alembic_fails(empty_database, "upgrade", DESTINATION_IDENTITY)

    assert "InFlightPlacementsError" in reported
    assert str(copying) in reported
    assert (await _views_and_head(empty_database))[1] == BEFORE_DESTINATION_IDENTITY


@pytest.mark.parametrize(
    "destinations",
    [
        (("a", "acct-1", "/Library"), ("b", "acct-1", "/Library/Photos")),
        (("a", "acct-1", "/Library"), ("b", "acct-1", "/Library/")),
        (("a", "acct-1", "/Library//Photos"),),
    ],
    ids=["nested-roots", "two-names-one-place", "unnormalized-root"],
)
async def test_an_active_policy_the_application_would_refuse_stops_the_upgrade(
    empty_database: str, destinations: tuple[tuple[str, str, str], ...]
) -> None:
    """The upgrade refuses before changing anything rather than leave an unloadable policy."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    await _seed_policies(empty_database, _document(*destinations))

    reported = _alembic_fails(empty_database, "upgrade", DESTINATION_IDENTITY)

    assert "UnloadablePolicyError" in reported
    assert "active policy version 1" in reported
    assert (await _views_and_head(empty_database))[1] == BEFORE_DESTINATION_IDENTITY


async def test_the_downgrade_refuses_two_placements_under_one_name(empty_database: str) -> None:
    """One content object at two identities under one name cannot be keyed by the name."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    ids = await _seed_redefinition(empty_database)
    _alembic(empty_database, "upgrade", DESTINATION_IDENTITY)
    await _execute(
        empty_database,
        "INSERT INTO placements (id, content_object_id, destination_id, destination_name, "
        "locator, template_version, state) SELECT :id, content_object_id, :elsewhere, 'lib', "
        "locator, 'seed-1', 'absent' FROM placements WHERE id = :between",
        id=uuid4(),
        elsewhere=_ACCOUNT_2,
        between=ids["between"],
    )

    reported = _alembic_fails(empty_database, "downgrade", BEFORE_DESTINATION_IDENTITY)

    assert "cannot be restored" in reported
    assert (await _views_and_head(empty_database))[1] == DESTINATION_IDENTITY


# The migration lock (migrations/env.py): concurrent runs serialize, a waiter gives up after
# the timeout, workers only verify, and an upgrade on a schema ahead of its build is a no-op.
MIGRATION_LOCK_KEY = migration_lock_key(dbos_app.APPLICATION_NAME)
RUNNING_UPGRADE = "Running upgrade"
CONCURRENT_RUNS = 3
# Far below the 1800 s lock-timeout default, so a lock run that hangs (as when a waiter's
# snapshot blocked DBOS's CREATE INDEX CONCURRENTLY) fails the test instead of CI's job.
RUN_TIMEOUT_SECONDS = 300
_FUTURE_REVISION = "ffff_future"


def _upgrade_concurrently(database_url: str, logs: Path) -> list[tuple[int, str]]:
    """Start `alembic upgrade head` CONCURRENT_RUNS times at once and wait for every run.

    Output goes to files, not pipes, so a run never blocks on a pipe no one is reading
    while another run waits for the lock it holds.

    Returns:
        Each run's return code and stderr.

    Raises:
        pytest.fail.Exception: The runs outlasted RUN_TIMEOUT_SECONDS; all were killed.
    """
    environment = _alembic_environment(database_url)
    runs: list[tuple[subprocess.Popen[bytes], Path]] = []
    for index in range(CONCURRENT_RUNS):
        stderr_path = logs / f"upgrade-{index}.stderr"
        with stderr_path.open("wb") as stderr:
            process = subprocess.Popen(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=REPOSITORY_ROOT,
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
            )
        runs.append((process, stderr_path))
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    try:
        codes = [process.wait(timeout=max(0.0, deadline - time.monotonic())) for process, _ in runs]
    except subprocess.TimeoutExpired:
        pytest.fail(f"concurrent upgrades still running after {RUN_TIMEOUT_SECONDS}s")
    finally:
        for process, _ in runs:
            if process.poll() is None:
                process.kill()
                process.wait()
    return [
        (code, stderr_path.read_text()) for code, (_, stderr_path) in zip(codes, runs, strict=True)
    ]


def _running_upgrade_lines(stderr: str) -> int:
    """Return how many revisions Alembic reports applying in one run's stderr."""
    return sum(RUNNING_UPGRADE in line for line in stderr.splitlines())


async def _table_names(database_url: str) -> set[tuple[str, str]]:
    """Return every table and view in the `public` and `dbos` schemas as (schema, name)."""
    return {
        (str(schema), str(name))
        for schema, name in await _rows(
            database_url,
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema IN ('public', 'dbos')",
        )
    }


async def _has_dbos_schema(database_url: str) -> bool:
    """Return whether the database has a `dbos` schema."""
    return bool(
        await _rows(
            database_url,
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'dbos'",
        )
    )


async def _assert_at_head_with_system_tables(database_url: str) -> None:
    """Assert the ledger is at the chain's head beside the DBOS and tracking tables."""
    assert (await _views_and_head(database_url))[1] == _script_directory().get_current_head()
    assert {("dbos", "workflow_status"), ("dbos", "datasource_outputs")} <= await _table_names(
        database_url
    )


async def test_concurrent_upgrades_on_an_empty_database_apply_each_revision_once(
    empty_database: str, tmp_path: Path
) -> None:
    """Upgrades started at once all exit 0, and every revision runs once across them."""
    runs = _upgrade_concurrently(empty_database, tmp_path)

    assert [code for code, _ in runs] == [0] * CONCURRENT_RUNS, [err for _, err in runs]
    await _assert_at_head_with_system_tables(empty_database)
    assert sum(_running_upgrade_lines(stderr) for _, stderr in runs) == len(
        list(_script_directory().walk_revisions())
    )


async def test_concurrent_upgrades_one_revision_behind_apply_it_once(
    empty_database: str, tmp_path: Path
) -> None:
    """Upgrades started at once one revision behind all exit 0; one of them applies it."""
    script = _script_directory()
    head_id = script.get_current_head()
    assert head_id is not None
    head = script.get_revision(head_id)
    assert head is not None and isinstance(head.down_revision, str)
    _alembic(empty_database, "upgrade", head.down_revision)

    runs = _upgrade_concurrently(empty_database, tmp_path)

    assert [code for code, _ in runs] == [0] * CONCURRENT_RUNS, [err for _, err in runs]
    await _assert_at_head_with_system_tables(empty_database)
    assert sum(_running_upgrade_lines(stderr) for _, stderr in runs) == 1


async def test_a_lock_timeout_exits_3_and_changes_nothing(empty_database: str) -> None:
    """A run that cannot take the held lock in time names the setting, exits 3, migrates nothing."""
    holder = connect_session(
        libpq_conninfo(empty_database), application_name="migration-test holder"
    )
    try:
        assert holder.try_lock(MIGRATION_LOCK_KEY)

        completed = _run_alembic(
            empty_database,
            "upgrade",
            "head",
            environment=_alembic_environment(
                empty_database, CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS="1"
            ),
            timeout=RUN_TIMEOUT_SECONDS,
        )

        assert completed.returncode == 3, completed.stderr
        assert "migration.lock_waiting" in completed.stderr
        assert "migration.lock_timeout" in completed.stderr
        assert "CURATE_D_MIGRATION_LOCK_TIMEOUT_SECONDS" in completed.stderr
        assert ("public", "alembic_version") not in await _table_names(empty_database)
        assert not await _has_dbos_schema(empty_database)
    finally:
        holder.close()


async def test_verify_only_launch_fails_on_an_unmigrated_database(
    settings: Settings, empty_database: str
) -> None:
    """A worker's DBOS launch refuses a database with no system schema and creates none."""
    unmigrated = settings.model_copy(update={"ledger_database_url": PostgresDsn(empty_database)})
    DBOS.destroy()
    try:
        dbos_app.configure(unmigrated, executor_id="migration-verify-test")
        with pytest.raises(dbos_error.DBOSInitializationError):
            DBOS.launch()
    finally:
        DBOS.destroy()

    assert not await _has_dbos_schema(empty_database)


async def test_an_upgrade_on_a_schema_ahead_changes_nothing(empty_database: str) -> None:
    """An upgrade from an unknown revision warns and exits 0; a downgrade from it fails."""
    _alembic(empty_database, "upgrade", "head")
    await _execute(
        empty_database,
        "UPDATE alembic_version SET version_num = :revision",
        revision=_FUTURE_REVISION,
    )
    tables = await _table_names(empty_database)

    upgraded = _run_alembic(empty_database, "upgrade", "head")

    assert upgraded.returncode == 0, upgraded.stderr
    assert "migration.schema_ahead" in upgraded.stderr
    assert _FUTURE_REVISION in upgraded.stderr
    assert RUNNING_UPGRADE not in upgraded.stderr
    assert (await _views_and_head(empty_database))[1] == _FUTURE_REVISION
    assert await _table_names(empty_database) == tables

    downgraded = _run_alembic(empty_database, "downgrade", "base")

    assert downgraded.returncode != 0, downgraded.stderr
    assert (await _views_and_head(empty_database))[1] == _FUTURE_REVISION
    assert await _table_names(empty_database) == tables


async def _wait_until(condition: Callable[[], Awaitable[bool]], what: str) -> None:
    """Poll `condition` every 50 ms until it holds; fail after RUN_TIMEOUT_SECONDS."""
    deadline = time.monotonic() + RUN_TIMEOUT_SECONDS
    while not await condition():
        if time.monotonic() > deadline:
            pytest.fail(f"{what} did not happen within {RUN_TIMEOUT_SECONDS}s")
        await asyncio.sleep(0.05)


def _lock_holder_pid(database_url: str) -> int | None:
    """Return the pid of the session holding the migration lock, if one is visible."""
    observer = connect_session(
        libpq_conninfo(database_url), application_name="migration-test observer"
    )
    try:
        description = observer.holder(MIGRATION_LOCK_KEY)
    finally:
        observer.close()
    if description is None:
        return None
    match = re.match(r"pid (\d+)", description)
    assert match is not None, description
    return int(match.group(1))


async def test_a_lost_lock_session_stops_the_run_before_the_ledger_step(
    empty_database: str, tmp_path: Path
) -> None:
    """A run whose lock session is ended logs `migration.lock_lost`, exits 4, migrates nothing."""
    stderr_path = tmp_path / "upgrade.stderr"
    with stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=REPOSITORY_ROOT,
            env=_alembic_environment(empty_database),
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )
    try:
        # The `dbos` schema appears only once the DBOS step runs, after the lock and the
        # schema-ahead read, so the kill lands on a session idle while DBOS writes on its
        # own connections, or inside Alembic's own step. Both must end as a lost lock.
        await _wait_until(lambda: _has_dbos_schema(empty_database), "the DBOS step")
        pid = _lock_holder_pid(empty_database)
        assert pid is not None
        await _execute(empty_database, "SELECT pg_terminate_backend(:pid)", pid=pid)
        code = process.wait(timeout=RUN_TIMEOUT_SECONDS)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
    log = stderr_path.read_text()

    assert code == 4, log
    assert "migration.lock_lost" in log
    assert "migration.lock_released" in log
    assert ("public", "alembic_version") not in await _table_names(empty_database)
