"""A start and a conclusion racing on one placement, in concurrent transactions.

Each opens on a placement verifying attempt 1. One transaction writes and is held open
before it commits; the other must block on the placement row, then check against what
the first committed. Under read committed, without the row lock, the second would check
the row as it was before the first and overwrite the first's write.
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
from curate_d.placement.pipeline import conclude, start
from curate_d.placement.verification import Assessment, Verdict

pytestmark = pytest.mark.integration

_LOCATOR: Final = "/Photos/2026/IMG_0001.JPG"
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


@pytest.fixture
async def verifying(ledger_engine: AsyncEngine) -> AsyncIterator[_Verifying]:
    """Commit a placement verifying attempt 1; remove its rows afterwards."""
    destination = f"dest-{uuid4().hex[:8]}"
    async with ledger_engine.begin() as connection:
        content = await ContentObjectRepository(connection).upsert(
            ContentObject.build(
                fingerprint=StrongFingerprint(
                    algorithm=HashAlgorithm.SHA256, digest=uuid4().hex * 2
                ),
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
    async with ledger_engine.begin() as connection:
        await connection.execute(delete(placements).where(placements.c.id == placement.id))
        await connection.execute(delete(source_objects).where(source_objects.c.id == source.id))
        await connection.execute(delete(content_objects).where(content_objects.c.id == content.id))


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
