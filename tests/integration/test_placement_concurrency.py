"""Starts and conclusions racing on one placement, in concurrent transactions.

Most open on a placement verifying attempt 1. One transaction writes and is held open
before it commits; the other must block on the placement row, then check against what
the first committed. Under read committed, without the row lock, the second would check
the row as it was before the first and overwrite the first's write.

Two first starts open on no placement row, which no row lock can hold. They render to
different locators, so they lock different paths; the placement's own lock is what makes
the second wait and then find the first's row.
"""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from curate_d.domain import (
    AuditEntryType,
    ContentObject,
    HashAlgorithm,
    Placement,
    PlacementState,
    Provider,
    SourceObject,
    StrongFingerprint,
    VerificationLevel,
)
from curate_d.ledger import ContentObjectRepository, PlacementRepository, SourceObjectRepository
from curate_d.ledger.tables import audit_entries, content_objects, placements, source_objects
from curate_d.placement.pipeline import PlacementStore, conclude, start
from curate_d.placement.verification import Assessment, SideEvidence, TransferEvidence, Verdict

pytestmark = pytest.mark.integration

_LOCATOR: Final = "/Photos/2026/IMG_0001.JPG"
_OTHER_LOCATOR: Final = "/Photos/2026-07/IMG_0001.JPG"
_TEMPLATE: Final = "seed-1"
_VERIFIED: Final = Assessment(
    verdict=Verdict.VERIFIED,
    level=VerificationLevel.STRONG_COMMON_HASH,
    compared="sha256",
    source_value="a" * 64,
    destination_value="a" * 64,
)
# How long the second transaction is given to show it is waiting, and to finish once
# the first commits; a missing lock fails on the assertions, never by hanging.
_BLOCKED_FOR: Final = 0.5
_FINISH_WITHIN: Final = 10


@dataclass(frozen=True)
class _Verifying:
    """A committed placement verifying attempt 1, with its content and source."""

    content: ContentObject
    source: SourceObject
    placement: Placement


@dataclass(frozen=True)
class _Unplaced:
    """Committed content and its source, with no placement at the destination yet."""

    content: ContentObject
    source: SourceObject
    destination: str


async def _seed(connection: AsyncConnection) -> tuple[ContentObject, SourceObject]:
    """Insert content and the source it was fingerprinted from."""
    content = await ContentObjectRepository(connection).upsert(
        ContentObject.build(
            fingerprint=StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest=uuid4().hex * 2),
            size_bytes=10,
            media_type="image/jpeg",
        )
    )
    source = await SourceObjectRepository(connection).upsert(
        SourceObject.build(
            provider=Provider.PCLOUD,
            account="acct-concurrency",
            native_id=uuid4().hex,
            path="/Photos/IMG_0001.JPG",
            size_bytes=10,
            modified_at=datetime(2026, 7, 1, 8, 0, tzinfo=UTC),
            version_marker="rev-1",
        )
    )
    return content, source


async def _remove(
    engine: AsyncEngine, content: ContentObject, source: SourceObject, destination: str
) -> None:
    """Delete the content's placement at `destination`, the source and the content."""
    async with engine.begin() as connection:
        await connection.execute(
            delete(placements).where(
                placements.c.content_object_id == content.id,
                placements.c.destination_name == destination,
            )
        )
        await connection.execute(delete(source_objects).where(source_objects.c.id == source.id))
        await connection.execute(delete(content_objects).where(content_objects.c.id == content.id))


@pytest.fixture
async def verifying(ledger_engine: AsyncEngine) -> AsyncIterator[_Verifying]:
    """Commit a placement verifying attempt 1; remove its rows afterwards."""
    destination = f"dest-{uuid4().hex[:8]}"
    async with ledger_engine.begin() as connection:
        content, source = await _seed(connection)
        placement = await PlacementRepository(connection).upsert(
            Placement.build(
                content_object_id=content.id,
                destination_name=destination,
                locator=_LOCATOR,
                template_version=_TEMPLATE,
            ).model_copy(
                update={
                    "state": PlacementState.VERIFYING,
                    "attempts": 1,
                    "source_object_id": source.id,
                }
            )
        )
    yield _Verifying(content=content, source=source, placement=placement)
    await _remove(ledger_engine, content, source, destination)


@pytest.fixture
async def unplaced(ledger_engine: AsyncEngine) -> AsyncIterator[_Unplaced]:
    """Commit content and its source with no placement; remove their rows afterwards."""
    destination = f"dest-{uuid4().hex[:8]}"
    async with ledger_engine.begin() as connection:
        content, source = await _seed(connection)
    yield _Unplaced(content=content, source=source, destination=destination)
    await _remove(ledger_engine, content, source, destination)


async def _start(
    connection: AsyncConnection, verifying: _Verifying, *, attempt: int
) -> tuple[Placement, bool]:
    """Start a copy of the fixture's content as `attempt` at its locator."""
    return await start(
        connection,
        content_object_id=verifying.content.id,
        fingerprint=verifying.content.fingerprint,
        destination_name=verifying.placement.destination_name,
        locator=_LOCATOR,
        template_version=_TEMPLATE,
        source=verifying.source,
        transformations={},
        attempt=attempt,
        now=datetime.now(UTC),
    )


async def _first_start(
    connection: AsyncConnection, unplaced: _Unplaced, *, locator: str
) -> tuple[Placement, bool]:
    """Start the first copy of the fixture's content, rendered to `locator`."""
    return await start(
        connection,
        content_object_id=unplaced.content.id,
        fingerprint=unplaced.content.fingerprint,
        destination_name=unplaced.destination,
        locator=locator,
        template_version=_TEMPLATE,
        source=unplaced.source,
        transformations={},
        now=datetime.now(UTC),
    )


async def _conclude(
    connection: AsyncConnection, verifying: _Verifying
) -> tuple[Placement, SourceObject | None] | None:
    """Conclude attempt 1 of the fixture's placement as verified."""
    return await conclude(
        connection, verifying.placement.id, _VERIFIED, attempt=1, now=datetime.now(UTC)
    )


async def _recorded(engine: AsyncEngine, verifying: _Verifying) -> Placement | None:
    """Return the fixture's placement as committed."""
    async with engine.connect() as connection:
        return await PlacementRepository(connection).get(verifying.placement.id)


async def _recorded_at(engine: AsyncEngine, placement: Placement) -> Placement | None:
    """Return `placement` as committed."""
    async with engine.connect() as connection:
        return await PlacementRepository(connection).get(placement.id)


async def _entries(engine: AsyncEngine, verifying: _Verifying) -> list[str]:
    """Return the entry types audited for the fixture's placement, oldest first."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(audit_entries.c.entry_type)
            .where(audit_entries.c.placement_id == verifying.placement.id)
            .order_by(audit_entries.c.id)
        )
        return list(rows.scalars())


async def test_a_conclusion_waits_for_a_newer_start_and_then_concludes_nothing(
    ledger_engine: AsyncEngine, verifying: _Verifying
) -> None:
    """A late verification blocks on a start in flight, then sees attempt 2 and writes nothing."""
    async with ledger_engine.connect() as starting, ledger_engine.connect() as concluding:
        await starting.begin()
        started, copy = await _start(starting, verifying, attempt=2)
        assert copy
        assert (started.state, started.attempts) == (PlacementState.COPYING, 2)

        await concluding.begin()
        waiting = asyncio.create_task(_conclude(concluding, verifying))
        await asyncio.sleep(_BLOCKED_FOR)
        assert not waiting.done()

        await starting.commit()
        concluded = await asyncio.wait_for(waiting, _FINISH_WITHIN)
        await concluding.commit()

    assert concluded is None
    recorded = await _recorded(ledger_engine, verifying)
    assert recorded is not None
    assert (recorded.state, recorded.attempts) == (PlacementState.COPYING, 2)
    assert await _entries(ledger_engine, verifying) == [AuditEntryType.PLACEMENT_STARTED.value]


async def test_a_start_waits_for_a_conclusion_and_then_sees_the_registration(
    ledger_engine: AsyncEngine, verifying: _Verifying
) -> None:
    """A stale start blocks on a conclusion in flight, then finds the copy registered."""
    async with ledger_engine.connect() as concluding, ledger_engine.connect() as starting:
        await concluding.begin()
        concluded = await _conclude(concluding, verifying)
        assert concluded is not None
        registered, _ = concluded
        assert registered.state is PlacementState.PRESENT

        await starting.begin()
        waiting = asyncio.create_task(_start(starting, verifying, attempt=1))
        await asyncio.sleep(_BLOCKED_FOR)
        assert not waiting.done()

        await concluding.commit()
        placement, copy = await asyncio.wait_for(waiting, _FINISH_WITHIN)
        await starting.commit()

    assert not copy
    assert (placement.state, placement.attempts) == (PlacementState.PRESENT, 1)
    recorded = await _recorded(ledger_engine, verifying)
    assert recorded is not None
    assert (recorded.state, recorded.attempts) == (PlacementState.PRESENT, 1)
    assert await _entries(ledger_engine, verifying) == [
        AuditEntryType.VERIFICATION_SUCCEEDED.value,
        AuditEntryType.PLACEMENT_REGISTERED.value,
    ]


async def test_two_first_starts_at_different_locators_serialize_on_the_placement(
    ledger_engine: AsyncEngine, unplaced: _Unplaced
) -> None:
    """A first start waits for another of the placement in flight, then records the next attempt."""
    async with ledger_engine.connect() as first, ledger_engine.connect() as second:
        await first.begin()
        started, copy = await _first_start(first, unplaced, locator=_LOCATOR)
        assert copy
        assert (started.locator, started.attempts) == (_LOCATOR, 0)

        await second.begin()
        waiting = asyncio.create_task(_first_start(second, unplaced, locator=_OTHER_LOCATOR))
        await asyncio.sleep(_BLOCKED_FOR)
        assert not waiting.done()

        await first.commit()
        restarted, copy = await asyncio.wait_for(waiting, _FINISH_WITHIN)
        await second.commit()

    assert copy
    assert (restarted.id, restarted.state) == (started.id, PlacementState.COPYING)
    assert (restarted.locator, restarted.attempts) == (_OTHER_LOCATOR, 1)
    evidence = TransferEvidence(source=SideEvidence(size_bytes=10), destination=SideEvidence())
    async with ledger_engine.begin() as connection:
        assert not await PlacementStore(connection).set_evidence(started.id, evidence, attempt=0)
    recorded = await _recorded_at(ledger_engine, started)
    assert recorded is not None
    assert (recorded.state, recorded.locator, recorded.attempts) == (
        PlacementState.COPYING,
        _OTHER_LOCATOR,
        1,
    )
