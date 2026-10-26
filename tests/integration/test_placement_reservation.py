"""Locator reservation against a real database, across concurrent transactions."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from curate_d.domain import (
    ContentObject,
    ContentObjectId,
    DestinationConfig,
    HashAlgorithm,
    Placement,
    Provider,
    SourceObject,
    StrongFingerprint,
)
from curate_d.ledger import (
    ContentObjectRepository,
    DestinationRepository,
    PlacementRepository,
    SourceObjectRepository,
)
from curate_d.ledger.tables import content_objects, placements, source_objects
from curate_d.placement.pipeline import reserve, start
from curate_d.rendering.collision import disambiguate

pytestmark = pytest.mark.integration

_LOCATOR = "/Photos/2024/2024-05/IMG_0001.JPG"
_WHEN = datetime(2026, 10, 25, tzinfo=UTC)


@pytest.fixture
async def contents(ledger_engine: AsyncEngine) -> AsyncIterator[list[ContentObject]]:
    """Two committed content objects, removed with their placements afterwards."""
    async with ledger_engine.begin() as connection:
        repository = ContentObjectRepository(connection)
        stored = [
            await repository.upsert(
                ContentObject.build(
                    fingerprint=StrongFingerprint(
                        algorithm=HashAlgorithm.SHA256, digest=uuid4().hex * 2
                    ),
                    size_bytes=10,
                    media_type="image/jpeg",
                )
            )
            for _ in range(2)
        ]
    yield stored
    ids = [c.id for c in stored]
    async with ledger_engine.begin() as connection:
        await connection.execute(delete(placements).where(placements.c.content_object_id.in_(ids)))
        await connection.execute(
            delete(source_objects).where(source_objects.c.native_id.in_([str(i) for i in ids]))
        )
        await connection.execute(delete(content_objects).where(content_objects.c.id.in_(ids)))


def _destination(name: str | None = None, account: str | None = None) -> DestinationConfig:
    """Return a destination of its own, unless a name or account is given to share."""
    return DestinationConfig(
        name=name or f"dest-{uuid4().hex[:8]}",
        provider=Provider.PCLOUD,
        account=account or f"acct-{uuid4().hex[:8]}",
        root_path="/Library",
    )


async def _claim(
    connection: AsyncConnection, content: ContentObject, destination: DestinationConfig
) -> tuple[str, ContentObjectId | None]:
    """Reserve the shared locator for `content` and record a placement there."""
    await DestinationRepository(connection).ensure(destination.identity)
    locator, displaced_by = await reserve(
        connection,
        content_object_id=content.id,
        fingerprint=content.fingerprint,
        destination_id=destination.id,
        locator=_LOCATOR,
    )
    await PlacementRepository(connection).upsert(
        Placement.build(
            content_object_id=content.id,
            destination_id=destination.id,
            destination_name=destination.name,
            locator=locator,
            template_version="v1",
        )
    )
    return locator, displaced_by


async def test_a_concurrent_start_waits_and_takes_the_suffixed_locator(
    ledger_engine: AsyncEngine, contents: list[ContentObject]
) -> None:
    """The second reservation blocks until the first commits, then sees its claim."""
    first, second = contents
    destination = _destination()
    async with ledger_engine.connect() as one, ledger_engine.connect() as two:
        await one.begin()
        assert await _claim(one, first, destination) == (_LOCATOR, None)

        await two.begin()
        waiting = asyncio.create_task(_claim(two, second, destination))
        await asyncio.sleep(0.5)
        assert not waiting.done()

        await one.commit()
        claimed = await asyncio.wait_for(waiting, 10)
        await two.commit()

    assert claimed == (disambiguate(_LOCATOR, second.fingerprint), first.id)


async def test_content_already_holding_its_locator_keeps_it(
    ledger_engine: AsyncEngine, contents: list[ContentObject]
) -> None:
    """A retry of the same content is not pushed aside by its own earlier claim."""
    first, _ = contents
    destination = _destination()
    async with ledger_engine.begin() as connection:
        await _claim(connection, first, destination)
    async with ledger_engine.begin() as connection:
        assert await _claim(connection, first, destination) == (_LOCATOR, None)


async def test_one_name_at_two_identities_claims_the_plain_locator_at_both(
    ledger_engine: AsyncEngine, contents: list[ContentObject]
) -> None:
    """Two accounts under one name neither wait for each other nor push each other aside."""
    first, second = contents
    name = f"dest-{uuid4().hex[:8]}"
    here, there = _destination(name), _destination(name)
    async with ledger_engine.connect() as one, ledger_engine.connect() as two:
        await one.begin()
        assert await _claim(one, first, here) == (_LOCATOR, None)

        await two.begin()
        # The first transaction still holds its lock; another identity's is a different key.
        claimed = await asyncio.wait_for(_claim(two, second, there), 5)
        await two.commit()
        await one.commit()

    assert claimed == (_LOCATOR, None)


async def _start(
    connection: AsyncConnection, content: ContentObject, destination: DestinationConfig
) -> tuple[Placement, bool]:
    """Start a first copy of `content` at the shared locator of `destination`."""
    source = await SourceObjectRepository(connection).upsert(
        SourceObject.build(
            provider=Provider.PCLOUD,
            account="acct-reservation",
            native_id=f"{content.id}",
            path="/Photos/IMG_0001.JPG",
            size_bytes=10,
            modified_at=_WHEN,
        )
    )
    return await start(
        connection,
        content_object_id=content.id,
        fingerprint=content.fingerprint,
        destination=destination,
        locator=_LOCATOR,
        template_version="v1",
        source=source,
        transformations={},
        now=_WHEN,
    )


async def _destination_rows(engine: AsyncEngine, destination: DestinationConfig) -> int:
    """Return how many destinations rows record `destination`'s identity."""
    async with engine.connect() as connection:
        return len(await DestinationRepository(connection).find_many([destination.id]))


async def test_one_content_under_one_name_at_two_accounts_is_two_placements(
    ledger_engine: AsyncEngine, contents: list[ContentObject]
) -> None:
    """After the name moved accounts, a start records a second placement at the plain locator."""
    first, _ = contents
    old = _destination()
    moved = _destination(old.name)
    async with ledger_engine.begin() as connection:
        before, _ = await _start(connection, first, old)
    async with ledger_engine.begin() as connection:
        after, copy = await _start(connection, first, moved)
        recorded = await PlacementRepository(connection).find_for_content_object(first.id)

    assert copy
    assert before.id != after.id
    assert {(p.destination_id, p.locator) for p in recorded} == {
        (old.id, _LOCATOR),
        (moved.id, _LOCATOR),
    }


async def test_a_start_after_a_rename_relabels_the_placement_and_keeps_its_id(
    ledger_engine: AsyncEngine, contents: list[ContentObject]
) -> None:
    """Same provider, account and root under a new name: one row, the new label."""
    first, _ = contents
    original = _destination()
    renamed = DestinationConfig(
        name=f"renamed-{uuid4().hex[:8]}",
        provider=Provider.PCLOUD,
        account=original.account,
        root_path="/Library/",
    )
    async with ledger_engine.begin() as connection:
        before, _ = await _start(connection, first, original)
    async with ledger_engine.begin() as connection:
        after, _ = await _start(connection, first, renamed)
        recorded = await PlacementRepository(connection).find_for_content_object(first.id)

    assert [(p.id, p.destination_name) for p in recorded] == [(before.id, renamed.name)]
    assert after.id == before.id


async def test_the_destination_is_recorded_in_the_starts_transaction(
    ledger_engine: AsyncEngine, contents: list[ContentObject]
) -> None:
    """A start rolled back leaves no destination row; one committed leaves exactly one."""
    first, _ = contents
    destination = _destination()
    async with ledger_engine.connect() as connection:
        await connection.begin()
        await _start(connection, first, destination)
        await connection.rollback()
    assert await _destination_rows(ledger_engine, destination) == 0

    async with ledger_engine.begin() as connection:
        await _start(connection, first, destination)
    assert await _destination_rows(ledger_engine, destination) == 1
