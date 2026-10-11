"""Locator reservation against a real database, across concurrent transactions."""

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from curate_d.domain import (
    ContentObject,
    ContentObjectId,
    HashAlgorithm,
    Placement,
    StrongFingerprint,
)
from curate_d.ledger import ContentObjectRepository, PlacementRepository
from curate_d.ledger.tables import content_objects, placements
from curate_d.placement.pipeline import reserve
from curate_d.rendering.collision import disambiguate

pytestmark = pytest.mark.integration

_LOCATOR = "/Photos/2024/2024-05/IMG_0001.JPG"


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
        await connection.execute(delete(content_objects).where(content_objects.c.id.in_(ids)))


async def _claim(
    connection: AsyncConnection, content: ContentObject, destination: str
) -> tuple[str, ContentObjectId | None]:
    """Reserve the shared locator for `content` and record a placement there."""
    locator, displaced_by = await reserve(
        connection,
        content_object_id=content.id,
        fingerprint=content.fingerprint,
        destination_name=destination,
        locator=_LOCATOR,
    )
    await PlacementRepository(connection).upsert(
        Placement.build(
            content_object_id=content.id,
            destination_name=destination,
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
    destination = f"dest-{uuid4().hex[:8]}"
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
    destination = f"dest-{uuid4().hex[:8]}"
    async with ledger_engine.begin() as connection:
        await _claim(connection, first, destination)
    async with ledger_engine.begin() as connection:
        assert await _claim(connection, first, destination) == (_LOCATOR, None)
