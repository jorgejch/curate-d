"""Cache index coordination across independent PostgreSQL connections.

Most tests here use two engines, because that is the only way to observe what the code
is for. Transaction advisory locks are re-entrant within one session, so a
single-connection test of a lock proves nothing about two pods; and a registration is
only "published" once another session can see it.

The object store is a fake, so the subject is the coordination and not the backend.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from structlog.testing import capture_logs

from curate_d.config import Settings
from curate_d.ledger.tables import staged_objects
from curate_d.staging.cache_index import PostgresStagingCacheIndex
from curate_d.staging.errors import StagingObjectStoreError
from curate_d.staging.keys import StagingKey
from curate_d.staging.objects import StagedObject, StagedObjectRef

pytestmark = pytest.mark.integration

_WHEN = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

# Long enough that a lock wait is not mistaken for scheduling latency, short enough that
# the suite does not pay for it when the lock behaves.
_BLOCKED_FOR = 0.25


class WriterInterruptedError(Exception):
    """Stands in for a writer dying part way through a publication."""


class FakeObjectStore:
    """Lists the object names it was handed and records the deletions it is asked for."""

    def __init__(self, *object_keys: str) -> None:
        self.listing = list(object_keys)
        self.deleted: list[str] = []

    async def upload(
        self, target: StagedObject, source: BinaryIO, *, declared_max_bytes: int
    ) -> StagedObject:
        raise NotImplementedError

    async def download(self, obj: StagedObject, destination: BinaryIO) -> None:
        raise NotImplementedError

    async def delete(self, object_key: str) -> None:
        self.deleted.append(object_key)

    async def list_objects(self) -> AsyncIterator[StagedObjectRef]:
        for object_key in self.listing:
            yield StagedObjectRef(object_key=object_key, size_bytes=1024, last_modified=_WHEN)

    async def abort_incomplete_uploads(self, older_than: datetime) -> int:
        raise NotImplementedError

    @asynccontextmanager
    async def batch(self) -> AsyncGenerator[None, None]:
        """Share no client; this fake opens none."""
        yield


class KeyIssuer:
    """Hands out fresh staging keys and remembers them so the test can clean up."""

    def __init__(self) -> None:
        self.issued: list[StagingKey] = []

    def __call__(self) -> StagingKey:
        key = StagingKey(uuid4().hex)
        self.issued.append(key)
        return key


def staged(
    key: StagingKey, *, size: int = 1024, digest: str = "b" * 64, attempt: UUID | None = None
) -> StagedObject:
    """A verified upload for `key`, with distinguishable bytes per attempt."""
    return StagedObject(
        key=key,
        attempt_id=attempt if attempt is not None else uuid4(),
        size_bytes=size,
        sha256=digest,
        created_at=_WHEN,
    )


@pytest.fixture
def new_key() -> KeyIssuer:
    """Issue staging keys; the rows they name are removed in `_cleanup`."""
    return KeyIssuer()


@pytest.fixture
async def _cleanup(ledger_engine: AsyncEngine, new_key: KeyIssuer) -> AsyncIterator[None]:
    """Delete every row the test's keys named, however the test ended."""
    yield
    async with ledger_engine.begin() as connection:
        await connection.execute(
            delete(staged_objects).where(staged_objects.c.staging_key.in_(new_key.issued))
        )


@pytest.fixture
def index(ledger_engine: AsyncEngine, _cleanup: None) -> PostgresStagingCacheIndex:
    """The cache index one worker sees."""
    return PostgresStagingCacheIndex(ledger_engine)


@pytest.fixture
async def other_index(settings: Settings) -> AsyncIterator[PostgresStagingCacheIndex]:
    """A second worker: its own engine, so its locks are held by its own sessions."""
    engine = create_async_engine(str(settings.ledger_database_url), future=True)
    try:
        yield PostgresStagingCacheIndex(engine)
    finally:
        await engine.dispose()


async def registration_count(engine: AsyncEngine, key: StagingKey) -> int:
    """Count rows for `key` directly, independently of the index's own reads."""
    async with engine.connect() as connection:
        result = await connection.execute(
            select(func.count())
            .select_from(staged_objects)
            .where(staged_objects.c.staging_key == key)
        )
        return int(result.scalar_one())


async def test_a_registration_is_published_to_other_connections(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """Publication means committed: another session reads back exactly what was written."""
    obj = staged(new_key())
    await index.register(obj)
    assert await other_index.lookup(obj.key) == obj


async def test_zero_length_content_registers_like_any_other(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """Empty files stage normally; an empty object is not an absent one."""
    obj = staged(new_key(), size=0)
    await index.register(obj)
    assert await other_index.lookup(obj.key) == obj


async def test_exactly_one_of_two_concurrent_writers_wins(
    ledger_engine: AsyncEngine,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """Both writers succeed, one row exists, and it is one writer's bytes end to end."""
    key = new_key()
    first = staged(key, size=11, digest="a" * 64)
    second = staged(key, size=22, digest="c" * 64)

    await asyncio.gather(index.register(first), other_index.register(second))

    assert await registration_count(ledger_engine, key) == 1
    assert await index.lookup(key) in (first, second)


async def test_the_writer_lock_serializes_publication_across_connections(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """A second pod's publication waits for the first pod's writer lock, and readers do not."""
    obj = staged(new_key())
    async with index.writer_lock("test"):
        blocked = asyncio.create_task(other_index.register(obj))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(blocked), timeout=_BLOCKED_FOR)
        # Admission is serialized; reads are not, or every download would queue behind it.
        assert await other_index.lookup(obj.key) is None

    await blocked
    assert await other_index.lookup(obj.key) == obj


async def test_the_writer_lock_is_reentrant_for_the_task_holding_it(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """Publishing inside an admission decision must not queue behind that decision's lock."""
    obj = staged(new_key())
    async with index.writer_lock("test"):
        await asyncio.wait_for(index.register(obj), timeout=5)
        # Still inside the admission transaction, so nothing is published yet.
        assert await other_index.lookup(obj.key) is None
    assert await other_index.lookup(obj.key) == obj


async def test_a_failed_commit_registers_nothing_and_stays_repeatable(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """A writer that dies after inserting leaves no winner, and its retry publishes."""
    obj = staged(new_key())
    with pytest.raises(WriterInterruptedError):
        async with index.writer_lock("test"):
            await index.register(obj)
            raise WriterInterruptedError

    assert await other_index.lookup(obj.key) is None
    await index.register(obj)
    assert await other_index.lookup(obj.key) == obj


async def test_a_late_upload_cannot_displace_the_published_winner(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """The loser's bytes become an orphan for cleanup; the winner readers use is untouched."""
    key = new_key()
    winner = staged(key, size=11, digest="a" * 64)
    latecomer = staged(key, size=22, digest="c" * 64)
    await index.register(winner)

    await other_index.register(latecomer)

    assert await index.lookup(key) == winner
    store = FakeObjectStore(winner.object_key, latecomer.object_key)
    assert await other_index.reconcile(store) == 1
    assert store.deleted == [latecomer.object_key]


async def test_a_reader_is_protected_from_eviction_while_downloading(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """Eviction skips a held entry rather than deleting bytes a download is streaming."""
    obj = staged(new_key())
    await index.register(obj)

    async with index.hold_for_download(obj.key) as held:
        assert held == obj
        assert await other_index.evict(obj.key) is None
        assert await other_index.lookup(obj.key) == obj

    assert await other_index.evict(obj.key) == obj
    assert await other_index.lookup(obj.key) is None


async def test_eviction_of_an_unregistered_key_frees_nothing(
    index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """An absent entry and a held one both mean the evictor should try another candidate."""
    assert await index.evict(new_key()) is None


async def test_release_leaves_an_orphan_for_reconciliation(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """Release drops the registration only; the bytes go when cleanup can prove no reader."""
    obj = staged(new_key())
    await index.register(obj)
    store = FakeObjectStore(obj.object_key)

    await index.release(obj.key)

    assert await other_index.lookup(obj.key) is None
    assert store.deleted == []
    assert await other_index.reconcile(store) == 1
    assert store.deleted == [obj.object_key]


async def test_repeated_release_is_safe(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """Release is retry-safe operational state: a repeat after a lost reply is a no-op."""
    obj = staged(new_key())
    await index.register(obj)

    await index.release(obj.key)
    await other_index.release(obj.key)
    await index.release(obj.key)

    assert await index.lookup(obj.key) is None


async def test_reconciliation_spares_registered_objects_and_active_reads(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """A released entry mid-download outlives the sweep that would otherwise truncate it."""
    published = staged(new_key())
    reading = staged(new_key())
    await index.register(published)
    await index.register(reading)
    store = FakeObjectStore(published.object_key, reading.object_key)

    async with index.hold_for_download(reading.key):
        await other_index.release(reading.key)
        assert await other_index.reconcile(store) == 0

    assert await other_index.reconcile(store) == 1
    assert store.deleted == [reading.object_key]


async def test_reconciliation_of_a_consistent_store_deletes_nothing(
    index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """The sweep runs on a schedule; the usual outcome is that everything is referenced."""
    obj = staged(new_key())
    await index.register(obj)
    store = FakeObjectStore(obj.object_key)

    assert await index.reconcile(store) == 0
    assert store.deleted == []


async def test_registered_bytes_total_exactly_what_is_published(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """Admission subtracts this from the budget, so it tracks every publish and release."""
    baseline = await index.total_registered_bytes()
    small = staged(new_key(), size=7)
    empty = staged(new_key(), size=0)
    large = staged(new_key(), size=4096)
    for obj in (small, empty, large):
        await index.register(obj)

    assert await other_index.total_registered_bytes() - baseline == 4103

    await index.release(large.key)
    assert await other_index.total_registered_bytes() - baseline == 7


async def test_an_attempt_registered_against_two_keys_is_a_typed_failure(
    index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """One attempt names one object; reusing it across keys is a bug, not a lost race."""
    attempt = uuid4()
    await index.register(staged(new_key(), attempt=attempt))

    with pytest.raises(StagingObjectStoreError, match="register staging key"):
        await index.register(staged(new_key(), attempt=attempt))


async def test_the_writer_lock_is_reentrant_for_a_task_the_holder_started(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """A holder that fans work out to a task must not queue behind its own lock."""
    obj = staged(new_key())
    async with index.writer_lock("test"):
        published = asyncio.create_task(index.register(obj))
        await asyncio.wait_for(published, timeout=5)
        # Still inside the admission transaction, so nothing is published yet.
        assert await other_index.lookup(obj.key) is None

    assert await other_index.lookup(obj.key) == obj


async def test_the_registered_total_sees_the_writer_transactions_own_deletes(
    index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """Admission reports what eviction just removed, not a snapshot that still counts it."""
    obj = staged(new_key(), size=4096)
    await index.register(obj)
    baseline = await index.total_registered_bytes()

    async with index.writer_lock("test"):
        assert await index.evict(obj.key) == obj
        assert await index.total_registered_bytes() == baseline - 4096


async def test_a_reader_behind_a_reclaiming_writer_reports_its_wait(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """Reclamation claims a live key through its orphan; the reader's wait is the cost."""
    key = new_key()
    winner = staged(key)
    loser = staged(key)
    await index.register(winner)
    store = FakeObjectStore(winner.object_key, loser.object_key)

    async def read() -> StagedObject | None:
        async with index.hold_for_download(key) as held:
            return held

    with capture_logs() as events:
        async with other_index.writer_lock("occupancy.reserve"):
            assert await other_index.reconcile(store) == 1
            reader = asyncio.create_task(read())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(reader), timeout=_BLOCKED_FOR)
        assert await reader == winner

    waits = [e for e in events if e["event"] == "staging.reader_lock_wait"]
    assert len(waits) == 1
    assert waits[0]["staging_key"] == str(key)
    assert waits[0]["registered"] is True
    assert waits[0]["wait_seconds"] > _BLOCKED_FOR / 2
    # The holding that blocked the reader reports the one entry lock it held.
    held = [e for e in events if e["event"] == "staging.writer_lock_held"]
    assert [(e["caller"], e["entry_claims"]) for e in held] == [("occupancy.reserve", 1)]
    assert held[0]["held_seconds"] >= _BLOCKED_FOR


async def test_an_unblocked_reader_of_a_missing_key_reports_a_miss(
    index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """The miss is part of the event, so a wait that ended in nothing can be told apart."""
    key = new_key()

    with capture_logs() as events:
        async with index.hold_for_download(key) as held:
            assert held is None

    assert [(e["event"], e["staging_key"], e["registered"]) for e in events] == [
        ("staging.reader_lock_wait", str(key), False)
    ]
    assert events[0]["wait_seconds"] < _BLOCKED_FOR


async def test_writer_lock_events_report_one_pods_hold_as_the_others_wait(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """The waiter's wait and the holder's hold describe the same contention from both sides."""
    obj = staged(new_key())

    with capture_logs() as events:
        async with other_index.writer_lock("occupancy.sweep"):
            blocked = asyncio.create_task(index.register(obj))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(blocked), timeout=_BLOCKED_FOR)
        await blocked

    held = {e["caller"]: e for e in events if e["event"] == "staging.writer_lock_held"}
    assert set(held) == {"occupancy.sweep", "index.register"}
    assert held["occupancy.sweep"]["held_seconds"] >= _BLOCKED_FOR
    assert held["index.register"]["wait_seconds"] > _BLOCKED_FOR / 2


async def test_a_holding_reports_every_entry_it_evicted(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """Each evicted key's entry lock is held until the holding commits."""
    evicted = [staged(new_key()), staged(new_key())]
    for obj in evicted:
        await index.register(obj)

    with capture_logs() as events:
        async with other_index.writer_lock("occupancy.reserve"):
            for obj in evicted:
                assert await other_index.evict(obj.key) == obj

    held = [e for e in events if e["event"] == "staging.writer_lock_held"]
    assert [(e["caller"], e["entry_claims"]) for e in held] == [("occupancy.reserve", 2)]


async def test_a_claim_a_reader_refused_is_not_counted(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """The reader's shared lock wins the try-lock, so the writer holds nothing it could block."""
    obj = staged(new_key())
    await index.register(obj)

    with capture_logs() as events:
        async with (
            index.hold_for_download(obj.key),
            other_index.writer_lock("occupancy.sweep"),
        ):
            assert await other_index.evict(obj.key) is None

    held = [e for e in events if e["event"] == "staging.writer_lock_held"]
    assert [(e["caller"], e["entry_claims"]) for e in held] == [("occupancy.sweep", 0)]
