"""Alembic against an empty database.

The rest of the suite runs on a database that is already migrated, so it cannot notice a
migration that only works when something else ran first. These tests create a database of
their own and drive the Alembic command line against it, as CI and a deployment do.
"""

import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import create_async_engine

from curate_d.config import Settings
from curate_d.domain import derive_destination_id

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


# 0020 records each placement's destination identity from the policy in force when it was
# last started. Each test seeds placements in its own throwaway database at the revision
# before.
BEFORE_DESTINATION_IDENTITY = "0019_command_completed_audit"
DESTINATION_IDENTITY = "0020_destination_identity"
_T0 = datetime(2026, 10, 1, tzinfo=UTC)
_ACCOUNT_A = derive_destination_id("pcloud", "acct-a", "/Library")
_ACCOUNT_B = derive_destination_id("pcloud", "acct-b", "/Library")


def _alembic_fails(database_url: str, *arguments: str) -> str:
    """Run the Alembic command line expecting it to fail; return what it reported."""
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
    assert completed.returncode != 0, completed.stdout
    return completed.stderr


def _policy(account: str) -> str:
    """Return a policy document naming `lib` at `/Library/` of `account`, as JSON."""
    return json.dumps(
        {
            "destinations": [
                {
                    "name": "lib",
                    "provider": "pcloud",
                    "account": account,
                    "root_path": "/Library/",
                    "profile": "pcloud",
                }
            ],
            "rules": [],
            "retention": [],
        }
    )


async def _seed_redefinition(database_url: str, *, ghost: bool = False) -> dict[str, UUID]:
    """Seed `lib` at account A in policy v1, at account B in v2, and placements under it.

    Returns:
        Placement ids by case: `before` started under v1; `restarted` created under v1
        and started again under v2; `unstarted` created under v2 with no start recorded;
        and with `ghost`, `ghost` under a name no version defines.
    """
    placements = {
        "before": ("lib", _T0 + timedelta(hours=1), [_T0 + timedelta(hours=1)]),
        "restarted": (
            "lib",
            _T0 + timedelta(hours=2),
            [_T0 + timedelta(hours=2), _T0 + timedelta(days=2)],
        ),
        "unstarted": ("lib", _T0 + timedelta(days=3), []),
    }
    if ghost:
        placements["ghost"] = ("ghost", _T0 + timedelta(days=3), [])
    ids: dict[str, UUID] = {}
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            for version, (account, at) in enumerate(
                (("acct-a", _T0), ("acct-b", _T0 + timedelta(days=1))), start=1
            ):
                await connection.execute(
                    text(
                        "INSERT INTO configurations (kind, version, document, active, "
                        "created_at, created_by) VALUES ('policy', :version, "
                        "CAST(:document AS jsonb), :active, :at, 'test')"
                    ),
                    {
                        "version": version,
                        "document": _policy(account),
                        "active": version == 2,
                        "at": at,
                    },
                )
            for index, (case, (name, created, starts)) in enumerate(placements.items()):
                content_id, placement_id = uuid4(), uuid4()
                ids[case] = placement_id
                await connection.execute(
                    text(
                        "INSERT INTO content_objects (id, fingerprint_algorithm, "
                        "fingerprint_digest, size_bytes, media_type) VALUES (:id, 'sha256', "
                        ":digest, 1, 'image/jpeg')"
                    ),
                    {"id": content_id, "digest": f"{index:x}" * 64},
                )
                await connection.execute(
                    text(
                        "INSERT INTO placements (id, content_object_id, destination_name, "
                        "locator, template_version, created_at) VALUES (:id, :content, :name, "
                        ":locator, 'seed-1', :created)"
                    ),
                    {
                        "id": placement_id,
                        "content": content_id,
                        "name": name,
                        "locator": f"/Library/{case}.jpg",
                        "created": created,
                    },
                )
                for started in starts:
                    await connection.execute(
                        text(
                            "INSERT INTO audit_entries (occurred_at, entry_type, "
                            "content_object_id, placement_id, decided_by, details) VALUES "
                            "(:at, 'placement_started', :content, :placement, 'placement', "
                            "'{}'::jsonb)"
                        ),
                        {"at": started, "content": content_id, "placement": placement_id},
                    )
    finally:
        await engine.dispose()
    return ids


async def _destination_ids(database_url: str) -> dict[UUID, UUID]:
    """Return each placement's destination identity."""
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(text("SELECT id, destination_id FROM placements"))
            return {row[0]: row[1] for row in rows}
    finally:
        await engine.dispose()


async def test_destination_identity_comes_from_the_policy_in_force_at_the_last_start(
    empty_database: str,
) -> None:
    """A start before the redefinition is at account A; one after it, or a placement only
    created after it, is at account B. Placement identifiers are kept."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    ids = await _seed_redefinition(empty_database)

    _alembic(empty_database, "upgrade", DESTINATION_IDENTITY)

    assert await _destination_ids(empty_database) == {
        ids["before"]: _ACCOUNT_A,
        ids["restarted"]: _ACCOUNT_B,
        ids["unstarted"]: _ACCOUNT_B,
    }
    engine = create_async_engine(empty_database)
    try:
        async with engine.connect() as connection:
            rows = await connection.execute(
                text("SELECT id, provider, account, root_path FROM destinations ORDER BY account")
            )
            assert [tuple(row) for row in rows] == [
                (_ACCOUNT_A, "pcloud", "acct-a", "/Library"),
                (_ACCOUNT_B, "pcloud", "acct-b", "/Library"),
            ]
    finally:
        await engine.dispose()


async def test_a_placement_no_policy_version_explains_stops_the_upgrade(
    empty_database: str,
) -> None:
    """Nothing is guessed: the upgrade fails naming the placement, and nothing changes."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    ids = await _seed_redefinition(empty_database, ghost=True)

    reported = _alembic_fails(empty_database, "upgrade", DESTINATION_IDENTITY)

    assert str(ids["ghost"]) in reported
    _, head = await _views_and_head(empty_database)
    assert head == BEFORE_DESTINATION_IDENTITY


async def test_destination_identity_downgrades_and_upgrades_again(empty_database: str) -> None:
    """The name-keyed schema comes back with the placements, and the backfill repeats."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    ids = await _seed_redefinition(empty_database)
    _alembic(empty_database, "upgrade", DESTINATION_IDENTITY)

    _alembic(empty_database, "downgrade", BEFORE_DESTINATION_IDENTITY)

    engine = create_async_engine(empty_database)
    try:
        async with engine.connect() as connection:
            count = await connection.scalar(text("SELECT count(*) FROM placements"))
            column = await connection.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'placements' AND column_name = 'destination_id'"
                )
            )
        assert (count, column) == (3, 0)
    finally:
        await engine.dispose()
    _alembic(empty_database, "upgrade", DESTINATION_IDENTITY)
    assert (await _destination_ids(empty_database))[ids["before"]] == _ACCOUNT_A


async def test_the_downgrade_refuses_two_placements_under_one_name(empty_database: str) -> None:
    """One content object at two identities under one name cannot be keyed by the name."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    ids = await _seed_redefinition(empty_database)
    _alembic(empty_database, "upgrade", "head")
    engine = create_async_engine(empty_database)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO placements (id, content_object_id, destination_id, "
                    "destination_name, locator, template_version) SELECT :id, "
                    "content_object_id, :elsewhere, 'lib', locator, 'seed-1' FROM placements "
                    "WHERE id = :before"
                ),
                {"id": uuid4(), "elsewhere": _ACCOUNT_B, "before": ids["before"]},
            )
    finally:
        await engine.dispose()

    reported = _alembic_fails(empty_database, "downgrade", BEFORE_DESTINATION_IDENTITY)

    assert "cannot be restored" in reported


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
    """Store these policy documents as versions 1, 2, ..., the last one active."""
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            for version, document in enumerate(documents, start=1):
                await connection.execute(
                    text(
                        "INSERT INTO configurations (kind, version, document, active, "
                        "created_at, created_by) VALUES ('policy', :version, "
                        "CAST(:document AS jsonb), :active, :at, 'test')"
                    ),
                    {
                        "version": version,
                        "document": document,
                        "active": version == len(documents),
                        "at": _T0 + timedelta(days=version - 1),
                    },
                )
    finally:
        await engine.dispose()


async def test_two_names_for_one_place_stop_the_upgrade(empty_database: str) -> None:
    """Placements a past policy made under two spellings of one root clash on one identity:
    the upgrade names them instead of failing on a constraint, and nothing changes."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    await _seed_policies(
        empty_database,
        _document(("a", "acct-a", "/Library"), ("b", "acct-a", "/Library/")),
        _document(("a", "acct-a", "/Library")),
    )
    content_id, first, second = uuid4(), uuid4(), uuid4()
    engine = create_async_engine(empty_database)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO content_objects (id, fingerprint_algorithm, fingerprint_digest, "
                    "size_bytes, media_type) VALUES (:id, 'sha256', :digest, 1, 'image/jpeg')"
                ),
                {"id": content_id, "digest": "f" * 64},
            )
            for placement_id, name in ((first, "a"), (second, "b")):
                await connection.execute(
                    text(
                        "INSERT INTO placements (id, content_object_id, destination_name, "
                        "locator, template_version, created_at) VALUES (:id, :content, :name, "
                        "'/Library/x.jpg', 'seed-1', :created)"
                    ),
                    {
                        "id": placement_id,
                        "content": content_id,
                        "name": name,
                        "created": _T0 + timedelta(hours=1),
                    },
                )
    finally:
        await engine.dispose()

    reported = _alembic_fails(empty_database, "upgrade", DESTINATION_IDENTITY)

    assert "SharedIdentityError" in reported
    assert str(first) in reported and str(second) in reported
    _, head = await _views_and_head(empty_database)
    assert head == BEFORE_DESTINATION_IDENTITY


@pytest.mark.parametrize(
    "destinations",
    [
        (("a", "acct-a", "/Library"), ("b", "acct-a", "/Library/")),
        (("a", "acct-a", "/Library"), ("b", "acct-a", "/Library/Photos")),
        (("a", "acct-a", "/Library//Photos"),),
    ],
    ids=["two-names-one-place", "nested-roots", "unnormalized-root"],
)
async def test_an_active_policy_the_application_would_refuse_stops_the_upgrade(
    empty_database: str, destinations: tuple[tuple[str, str, str], ...]
) -> None:
    """The upgrade fails before changing anything rather than leave a policy that no longer
    loads in force; an earlier version with the same flaw is no obstacle."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    await _seed_policies(empty_database, _document(*destinations))

    reported = _alembic_fails(empty_database, "upgrade", DESTINATION_IDENTITY)

    assert "UnloadablePolicyError" in reported
    assert "active policy version 1" in reported
    _, head = await _views_and_head(empty_database)
    assert head == BEFORE_DESTINATION_IDENTITY


async def test_a_flawed_policy_no_longer_in_force_is_no_obstacle(empty_database: str) -> None:
    """Only the active policy must load; an earlier one with nested roots upgrades fine."""
    _alembic(empty_database, "upgrade", BEFORE_DESTINATION_IDENTITY)
    await _seed_policies(
        empty_database,
        _document(("a", "acct-a", "/Library"), ("b", "acct-a", "/Library/Photos")),
        _document(("a", "acct-a", "/Library")),
    )

    _alembic(empty_database, "upgrade", DESTINATION_IDENTITY)

    _, head = await _views_and_head(empty_database)
    assert head == DESTINATION_IDENTITY
