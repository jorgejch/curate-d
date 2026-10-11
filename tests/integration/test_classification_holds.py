"""Classification holds against a real database, inside a rolled-back transaction."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from curate_d.classification.remote import RemoteRequest
from curate_d.classification.store import Hold, HoldRepository
from curate_d.domain import ContentObject, Facts, HashAlgorithm, MediaKind, StrongFingerprint
from curate_d.ledger import ContentObjectRepository

pytestmark = pytest.mark.integration

_WHEN = datetime(2026, 8, 31, 23, 0, tzinfo=UTC)


@pytest.fixture
async def connection(ledger_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A transaction rolled back at the end."""
    async with ledger_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()


async def _held(connection: AsyncConnection, period: str) -> Hold:
    """Hold a fresh content object in `period`."""
    content = await ContentObjectRepository(connection).upsert(
        ContentObject.build(
            fingerprint=StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="d" * 64),
            size_bytes=10,
            media_type="application/pdf",
        )
    )
    hold = Hold(
        content_object_id=content.id,
        period=period,
        held_at=_WHEN,
        request=RemoteRequest(
            content_object_id=content.id,
            media_type="application/pdf",
            paths=("/docs/scan.pdf",),
            hints={},
        ),
        base=Facts(media_kind=MediaKind.DOCUMENT),
    )
    await HoldRepository(connection).hold(hold)
    return hold


async def test_releasing_a_hold_drops_it(connection: AsyncConnection) -> None:
    """A released hold is no longer due."""
    holds = HoldRepository(connection)
    hold = await _held(connection, "2026-08")

    await holds.release(hold.content_object_id, "2026-08")

    assert await holds.due("2026-10") == []


async def test_releasing_keeps_a_hold_the_retry_renewed(connection: AsyncConnection) -> None:
    """The retry ran first and was held again by the next cap; releasing the old one keeps it."""
    holds = HoldRepository(connection)
    original = await _held(connection, "2026-08")
    await holds.hold(original.model_copy(update={"period": "2026-09"}))

    await holds.release(original.content_object_id, "2026-08")

    assert [(h.content_object_id, h.period) for h in await holds.due("2026-10")] == [
        (original.content_object_id, "2026-09")
    ]
