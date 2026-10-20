"""Repository behaviour against a real database.

Covers upsert and lookup for source objects, content objects, placements and audit
entries, and the change-detection comparison reading its prior state through the
repository.
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    ContentObject,
    DestinationConfig,
    HashAlgorithm,
    PerceptualFingerprint,
    Placement,
    PlacementState,
    Provider,
    SourceObject,
    SourceObjectState,
    StrongFingerprint,
    VerificationLevel,
)
from curate_d.ledger import (
    AuditRepository,
    ChangeVerdict,
    ContentObjectRepository,
    DestinationRepository,
    ListedFile,
    PlacementRepository,
    SourceObjectRepository,
    assess,
)
from curate_d.ledger.mapping import to_audit_entry
from curate_d.ledger.tables import audit_entries

pytestmark = pytest.mark.integration

_WHEN = datetime(2026, 7, 1, 8, 0, tzinfo=UTC)
_ARCHIVE = DestinationConfig(
    name="archive", provider=Provider.PCLOUD, account="acct-1", root_path="/Archive"
)


@pytest.fixture
async def connection(ledger_engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A transaction rolled back at the end, so tests cannot see each other's rows."""
    async with ledger_engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()


def _fingerprint(char: str) -> StrongFingerprint:
    """Build a SHA-256 fingerprint by repeating `char` to a valid digest length."""
    return StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest=char * 64)


def _source(*, native_id: str, path: str = "/a/x.jpg", size_bytes: int = 10) -> SourceObject:
    """Build a test source object with sensible defaults for the fields left unspecified."""
    return SourceObject.build(
        provider=Provider.PCLOUD,
        account="acct-1",
        native_id=native_id,
        path=path,
        size_bytes=size_bytes,
        modified_at=_WHEN,
        version_marker="rev-1",
    )


async def test_a_source_object_round_trips_through_the_repository(
    connection: AsyncConnection,
) -> None:
    """Upserting then getting a source object returns the same value."""
    repository = SourceObjectRepository(connection)
    stored = await repository.upsert(_source(native_id="s-1"))
    assert await repository.get(stored.id) == stored


async def test_a_source_object_is_found_by_its_provider_native_identity(
    connection: AsyncConnection,
) -> None:
    """A stored source object is found by provider, account, and native id."""
    repository = SourceObjectRepository(connection)
    stored = await repository.upsert(_source(native_id="s-2"))
    found = await repository.find_by_native_identity(
        provider=Provider.PCLOUD, account="acct-1", native_id="s-2"
    )
    assert found == stored


async def test_an_unknown_native_identity_returns_nothing(connection: AsyncConnection) -> None:
    """Looking up a native identity with no recorded row returns None."""
    repository = SourceObjectRepository(connection)
    assert (
        await repository.find_by_native_identity(
            provider=Provider.PCLOUD, account="acct-1", native_id="absent"
        )
        is None
    )


async def test_a_rename_updates_the_existing_row_rather_than_inserting_a_second(
    connection: AsyncConnection,
) -> None:
    """Existing placements stay valid because the source object keeps its identity."""
    repository = SourceObjectRepository(connection)
    before = await repository.upsert(_source(native_id="s-3", path="/before.jpg"))
    after = await repository.upsert(_source(native_id="s-3", path="/after.jpg"))

    assert after.id == before.id
    assert after.path == "/after.jpg"
    count = await connection.scalar(
        text("SELECT count(*) FROM source_objects WHERE native_id = 's-3'")
    )
    assert count == 1


async def test_change_detection_reads_its_prior_state_through_the_repository(
    connection: AsyncConnection,
) -> None:
    """`assess` distinguishes unchanged from changed using a row read from the repository."""
    repository = SourceObjectRepository(connection)
    await repository.upsert(_source(native_id="s-4", size_bytes=100))

    recorded = await repository.find_by_native_identity(
        provider=Provider.PCLOUD, account="acct-1", native_id="s-4"
    )
    unchanged = assess(
        ListedFile(
            native_id="s-4",
            path="/a/x.jpg",
            size_bytes=100,
            modified_at=_WHEN,
            version_marker="rev-1",
        ),
        recorded,
    )
    changed = assess(
        ListedFile(
            native_id="s-4",
            path="/a/x.jpg",
            size_bytes=100,
            modified_at=_WHEN,
            version_marker="rev-2",
        ),
        recorded,
    )
    assert unchanged.verdict is ChangeVerdict.UNCHANGED
    assert changed.verdict is ChangeVerdict.CHANGED


async def test_two_source_objects_with_one_fingerprint_attach_to_one_content_object(
    connection: AsyncConnection,
) -> None:
    """Two sources with equal fingerprints converge on one content object."""
    sources = SourceObjectRepository(connection)
    contents = ContentObjectRepository(connection)

    content = await contents.upsert(
        ContentObject.build(fingerprint=_fingerprint("a"), size_bytes=10, media_type="image/png")
    )
    again = await contents.upsert(
        ContentObject.build(fingerprint=_fingerprint("a"), size_bytes=10, media_type="image/png")
    )
    assert again.id == content.id

    first = await sources.upsert(_source(native_id="dup-a", path="/one.png"))
    second = await sources.upsert(_source(native_id="dup-b", path="/two.png"))
    await sources.attach_content_object(first.id, content.id)
    await sources.attach_content_object(second.id, content.id)

    attached = await sources.find_by_content_object(content.id)
    assert {source.id for source in attached} == {first.id, second.id}
    assert all(source.state is SourceObjectState.FINGERPRINTED for source in attached)


async def test_a_content_object_is_found_by_its_fingerprint(connection: AsyncConnection) -> None:
    """A content object is found by its fingerprint, and an unknown one returns None."""
    contents = ContentObjectRepository(connection)
    stored = await contents.upsert(
        ContentObject.build(fingerprint=_fingerprint("b"), size_bytes=3, media_type="image/png")
    )
    assert await contents.find_by_fingerprint(_fingerprint("b")) == stored
    assert await contents.find_by_fingerprint(_fingerprint("f")) is None


async def test_upserting_without_a_perceptual_hash_does_not_erase_a_recorded_one(
    connection: AsyncConnection,
) -> None:
    """A later caller that only computed the strong hash must not lose the image hash."""
    contents = ContentObjectRepository(connection)
    await contents.upsert(
        ContentObject.build(
            fingerprint=_fingerprint("c"),
            size_bytes=3,
            media_type="image/png",
            perceptual_fingerprint=PerceptualFingerprint(value=1234),
        )
    )
    after = await contents.upsert(
        ContentObject.build(fingerprint=_fingerprint("c"), size_bytes=3, media_type="image/png")
    )
    assert after.perceptual_fingerprint == PerceptualFingerprint(value=1234)


async def test_a_failed_source_object_records_its_reason_and_gains_no_content_object(
    connection: AsyncConnection,
) -> None:
    """Marking a source object failed records the reason without setting a content object."""
    sources = SourceObjectRepository(connection)
    stored = await sources.upsert(_source(native_id="broken"))
    await sources.mark_failed(stored.id, "provider returned 404 for /a/x.jpg")

    reloaded = await sources.get(stored.id)
    assert reloaded is not None
    assert reloaded.state is SourceObjectState.FAILED
    assert reloaded.failure_reason is not None
    assert reloaded.content_object_id is None


async def test_full_rescan_candidates_come_back_never_verified_first(
    connection: AsyncConnection,
) -> None:
    """Ordering nulls first is what makes an interrupted full rescan resumable."""
    sources = SourceObjectRepository(connection)
    verified = await sources.upsert(_source(native_id="seen", path="/seen.jpg"))
    never = await sources.upsert(_source(native_id="unseen", path="/unseen.jpg"))
    await sources.record_verification(verified.id, _WHEN)

    candidates = await sources.find_stale_for_full_rescan(
        provider=Provider.PCLOUD, account="acct-1", before=_WHEN + timedelta(days=1), limit=10
    )
    assert candidates[0].id == never.id
    assert verified.id in {candidate.id for candidate in candidates}


async def test_a_verified_source_is_excluded_once_it_is_recent_enough(
    connection: AsyncConnection,
) -> None:
    """A source verified after the cutoff is not a full-rescan candidate."""
    sources = SourceObjectRepository(connection)
    stored = await sources.upsert(_source(native_id="fresh"))
    await sources.record_verification(stored.id, _WHEN)

    candidates = await sources.find_stale_for_full_rescan(
        provider=Provider.PCLOUD, account="acct-1", before=_WHEN - timedelta(days=1), limit=10
    )
    assert candidates == []


async def test_a_placement_is_found_by_content_object_and_by_locator(
    connection: AsyncConnection,
) -> None:
    """A placement is found via its content object and via its destination/locator."""
    contents = ContentObjectRepository(connection)
    placements = PlacementRepository(connection)
    content = await contents.upsert(
        ContentObject.build(fingerprint=_fingerprint("d"), size_bytes=1, media_type="image/png")
    )
    await DestinationRepository(connection).ensure(_ARCHIVE.identity)
    stored = await placements.upsert(
        Placement.build(
            content_object_id=content.id,
            destination_id=_ARCHIVE.id,
            destination_name="archive",
            locator="/Archive/x.png",
            template_version="v1",
        )
    )

    assert await placements.get(stored.id) == stored
    assert await placements.find(content.id, _ARCHIVE.id) == stored
    assert await placements.find_for_content_object(content.id) == [stored]
    assert (
        await placements.find_at_locator(destination_id=_ARCHIVE.id, locator="/Archive/x.png")
        == stored
    )
    assert await placements.find_at_locator(destination_id=_ARCHIVE.id, locator="/nope") is None
    elsewhere = _ARCHIVE.model_copy(update={"account": "acct-2"})
    assert (
        await placements.find_at_locator(destination_id=elsewhere.id, locator="/Archive/x.png")
        is None
    )
    assert await placements.find(content.id, elsewhere.id) is None


async def test_re_executing_a_placement_updates_one_row_rather_than_duplicating_it(
    connection: AsyncConnection,
) -> None:
    """Re-upserting the same placement identity updates the row instead of duplicating it."""
    contents = ContentObjectRepository(connection)
    placements = PlacementRepository(connection)
    content = await contents.upsert(
        ContentObject.build(fingerprint=_fingerprint("e"), size_bytes=1, media_type="image/png")
    )
    await DestinationRepository(connection).ensure(_ARCHIVE.identity)
    pending = Placement.build(
        content_object_id=content.id,
        destination_id=_ARCHIVE.id,
        destination_name="archive",
        locator="/Archive/x.png",
        template_version="v1",
    )
    await placements.upsert(pending)
    registered = await placements.upsert(
        pending.model_copy(
            update={
                "state": PlacementState.PRESENT,
                "verification_level": VerificationLevel.STRONG_COMMON_HASH,
                "verified_at": _WHEN,
            }
        )
    )

    assert registered.id == pending.id
    assert await placements.find_for_content_object(content.id) == [registered]


async def test_a_rename_relabels_the_placement_and_keeps_its_identifier(
    connection: AsyncConnection,
) -> None:
    """Placed again under a new name at one identity, the row keeps its id and takes the name."""
    contents = ContentObjectRepository(connection)
    placements = PlacementRepository(connection)
    content = await contents.upsert(
        ContentObject.build(fingerprint=_fingerprint("9"), size_bytes=1, media_type="image/png")
    )
    await DestinationRepository(connection).ensure(_ARCHIVE.identity)
    first = await placements.upsert(
        Placement.build(
            content_object_id=content.id,
            destination_id=_ARCHIVE.id,
            destination_name="archive",
            locator="/Archive/x.png",
            template_version="v1",
        )
    )
    renamed = await placements.upsert(
        first.model_copy(update={"id": uuid4(), "destination_name": "vault"})
    )
    assert (renamed.id, renamed.destination_name) == (first.id, "vault")


async def test_a_destination_identity_is_recorded_once(connection: AsyncConnection) -> None:
    """Ensuring an identity twice leaves one row, and it reads back as recorded."""
    destinations = DestinationRepository(connection)
    identity = _ARCHIVE.model_copy(update={"account": f"acct-{uuid4().hex[:8]}"}).identity
    assert await destinations.get(identity.id) is None
    assert await destinations.ensure(identity) == identity.id
    assert await destinations.ensure(identity) == identity.id
    assert await destinations.get(identity.id) == identity
    assert await destinations.get_many([identity.id, identity.id]) == {identity.id: identity}


async def test_audit_history_comes_back_in_order(connection: AsyncConnection) -> None:
    """Audit history for a content object comes back ordered by occurrence."""
    contents = ContentObjectRepository(connection)
    audit = AuditRepository(connection)
    content = await contents.upsert(
        ContentObject.build(fingerprint=_fingerprint("f"), size_bytes=1, media_type="image/png")
    )

    for index, entry_type in enumerate(
        (
            AuditEntryType.CLASSIFICATION_DECIDED,
            AuditEntryType.PLACEMENT_STARTED,
            AuditEntryType.VERIFICATION_SUCCEEDED,
        )
    ):
        await audit.append(
            AuditEntry(
                entry_type=entry_type,
                occurred_at=_WHEN + timedelta(minutes=index),
                decided_by="test",
                details={"step": index},
                content_object_id=content.id,
            )
        )

    history = await audit.history_for_content_object(content.id)
    assert [entry.entry_type for entry in history] == [
        AuditEntryType.CLASSIFICATION_DECIDED,
        AuditEntryType.PLACEMENT_STARTED,
        AuditEntryType.VERIFICATION_SUCCEEDED,
    ]


async def test_audit_history_for_an_unknown_content_object_is_empty(
    connection: AsyncConnection,
) -> None:
    """Audit history for a content object with no recorded rows is empty."""
    contents = ContentObjectRepository(connection)
    audit = AuditRepository(connection)
    content = ContentObject.build(
        fingerprint=_fingerprint("0"), size_bytes=1, media_type="image/png"
    )
    assert await contents.get(content.id) is None
    assert await audit.history_for_content_object(content.id) == []


async def test_a_historical_operator_command_entry_still_reads_back(
    connection: AsyncConnection,
) -> None:
    """An entry the removed Conductor commands wrote still maps back from its row."""
    entry = AuditEntry(
        entry_type=AuditEntryType.WORKFLOW_COMMAND_ISSUED,
        occurred_at=_WHEN,
        decided_by="conductor",
        details={"command": "cancel", "source": "conductor", "request_id": f"req-{uuid4()}"},
    )
    entry_id = await AuditRepository(connection).append(entry)

    result = await connection.execute(select(audit_entries).where(audit_entries.c.id == entry_id))

    assert to_audit_entry(result.mappings().one()) == entry
