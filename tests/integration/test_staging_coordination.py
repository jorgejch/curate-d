"""Cache index coordination across independent PostgreSQL connections.

Most tests here use two engines, because that is the only way to observe what the code
is for. Transaction advisory locks are re-entrant within one session, so a
single-connection test of a lock proves nothing about two pods; and a registration is
only "published" once another session can see it.

The object store is a fake, so the subject is the coordination and not the backend.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from structlog.testing import capture_logs

from curate_d.config import Settings
from curate_d.ledger.tables import staged_objects, staging_reservations
from curate_d.staging.cache_index import PostgresStagingCacheIndex
from curate_d.staging.errors import StagingObjectStoreError
from curate_d.staging.keys import StagingKey
from curate_d.staging.objects import StagedObject, StagedObjectRef
from curate_d.staging.occupancy import StagingOccupancy

pytestmark = pytest.mark.integration

_WHEN = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

# Two valid staging keys whose four-byte entry lock ids collide.
_COLLIDING_KEYS: tuple[StagingKey, StagingKey] = (
    StagingKey(f"{0xEAE7:032x}"),
    StagingKey(f"{0x12388:032x}"),
)

# Long enough that a lock wait is not mistaken for scheduling latency, short enough that
# the suite does not pay for it when the lock behaves.
_BLOCKED_FOR = 0.25

# A reservation that outlives any test, and one the database clock has already expired to
# every later transaction.
_LIVE = timedelta(minutes=10)
_EXPIRED = timedelta(0)


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

    def colliding_pair(self) -> tuple[StagingKey, StagingKey]:
        """Return two distinct keys that share an entry lock, remembered for cleanup."""
        self.issued.extend(_COLLIDING_KEYS)
        return _COLLIDING_KEYS


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


async def _delete_registrations(engine: AsyncEngine, keys: Iterable[StagingKey]) -> None:
    """Delete the registrations of `keys`."""
    async with engine.begin() as connection:
        await connection.execute(
            delete(staged_objects).where(staged_objects.c.staging_key.in_(keys))
        )


async def _delete_reservations(engine: AsyncEngine, keys: Iterable[StagingKey]) -> None:
    """Delete the reservations made under `keys`."""
    async with engine.begin() as connection:
        await connection.execute(
            delete(staging_reservations).where(staging_reservations.c.staging_key.in_(keys))
        )


@pytest.fixture
async def _cleanup(ledger_engine: AsyncEngine, new_key: KeyIssuer) -> AsyncIterator[None]:
    """Delete every row the test's keys named, however the test ended."""
    yield
    await _delete_registrations(ledger_engine, new_key.issued)
    await _delete_reservations(ledger_engine, new_key.issued)


@pytest.fixture
async def colliding_keys(
    ledger_engine: AsyncEngine, new_key: KeyIssuer
) -> tuple[StagingKey, StagingKey]:
    """Return the colliding pair, first deleting any row an interrupted run left for it.

    The pair is fixed, so a leftover winner would make `register` keep the stale row, and
    a leftover reservation would spare an orphan reconciliation is meant to collect.
    """
    await _delete_registrations(ledger_engine, _COLLIDING_KEYS)
    await _delete_reservations(ledger_engine, _COLLIDING_KEYS)
    return new_key.colliding_pair()


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


async def reservation_count(engine: AsyncEngine, attempt_id: UUID) -> int:
    """Count reservation rows for `attempt_id` directly, expired or not."""
    async with engine.connect() as connection:
        result = await connection.execute(
            select(func.count())
            .select_from(staging_reservations)
            .where(staging_reservations.c.attempt_id == attempt_id)
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


async def test_a_reader_waits_behind_a_writer_that_claimed_only_a_colliding_key(
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    colliding_keys: tuple[StagingKey, StagingKey],
) -> None:
    """On the blocking reader side a lock collision is a stall, not a skip."""
    reclaimed, read_key = colliding_keys
    winner = staged(read_key)
    orphan = staged(reclaimed)
    await index.register(winner)
    store = FakeObjectStore(orphan.object_key)

    async def read() -> StagedObject | None:
        async with index.hold_for_download(read_key) as held:
            return held

    with capture_logs() as events:
        async with other_index.writer_lock("test"):
            assert await other_index.reconcile(store) == 1
            reader = asyncio.create_task(read())
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(reader), timeout=_BLOCKED_FOR)
        assert await reader == winner

    assert store.deleted == [orphan.object_key]
    waits = [e for e in events if e["event"] == "staging.reader_lock_wait"]
    assert [(e["staging_key"], e["registered"]) for e in waits] == [(str(read_key), True)]
    assert waits[0]["wait_seconds"] > _BLOCKED_FOR / 2


async def test_a_reader_of_a_colliding_key_makes_reconciliation_skip_an_orphan(
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    colliding_keys: tuple[StagingKey, StagingKey],
) -> None:
    """On the writer's try-lock side a collision is an unnecessary skip, retried next pass."""
    reclaimed, read_key = colliding_keys
    winner = staged(read_key)
    orphan = staged(reclaimed)
    await index.register(winner)
    store = FakeObjectStore(orphan.object_key)

    async with index.hold_for_download(read_key) as held:
        assert held == winner
        assert await other_index.reconcile(store) == 0
        assert store.deleted == []

    assert await other_index.reconcile(store) == 1
    assert store.deleted == [orphan.object_key]


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


async def test_a_reservation_is_counted_by_another_worker(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """Headroom one pod reserved is spent for every pod before its bytes land."""
    baseline = await other_index.total_reserved_bytes()

    await index.reserve(new_key(), uuid4(), 4096, _LIVE)

    assert await other_index.total_reserved_bytes() - baseline == 4096


async def test_a_reservation_is_consumed_exactly_once(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """The first consumer proves the bytes were protected; a repeat finds nothing to consume."""
    attempt = uuid4()
    await index.reserve(new_key(), attempt, 1024, _LIVE)

    assert await other_index.consume_reservation(attempt) is True
    assert await index.consume_reservation(attempt) is False


async def test_an_expired_reservation_cannot_be_consumed(
    ledger_engine: AsyncEngine,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """Past its expiry by the database clock, the reservation no longer protects anything."""
    attempt = uuid4()
    baseline = await other_index.total_reserved_bytes()
    await index.reserve(new_key(), attempt, 1024, _EXPIRED)

    assert await other_index.consume_reservation(attempt) is False
    assert await other_index.total_reserved_bytes() == baseline
    # Not consumed, so the row stays until reconciliation or abandonment removes it.
    assert await reservation_count(ledger_engine, attempt) == 1


async def test_reconciliation_collects_an_expired_reservations_object(
    ledger_engine: AsyncEngine,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """A dead pod's completed upload is an orphan once its reservation expires."""
    obj = staged(new_key())
    await index.reserve(obj.key, obj.attempt_id, obj.size_bytes, _EXPIRED)
    store = FakeObjectStore(obj.object_key)

    assert await other_index.reconcile(store) == 1

    assert store.deleted == [obj.object_key]
    assert await reservation_count(ledger_engine, obj.attempt_id) == 0


async def test_reconciliation_spares_another_workers_live_reservation(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """A completed upload awaiting its registration survives a sweep, and then registers."""
    obj = staged(new_key())
    await index.reserve(obj.key, obj.attempt_id, obj.size_bytes, _LIVE)
    store = FakeObjectStore(obj.object_key)

    assert await other_index.reconcile(store) == 0
    assert store.deleted == []

    async with index.writer_lock("occupancy.register"):
        assert await index.consume_reservation(obj.attempt_id) is True
        await index.register(obj)

    assert await other_index.lookup(obj.key) == obj
    assert await other_index.reconcile(store) == 0


async def test_a_forget_outside_any_holding_reports_an_abandon_holding(
    ledger_engine: AsyncEngine, index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """Abandonment is its own holding of the writer lock, labelled for the lock's demand."""
    attempt = uuid4()
    await index.reserve(new_key(), attempt, 1024, _LIVE)

    with capture_logs() as events:
        await index.forget_reservation(attempt)

    held = [e for e in events if e["event"] == "staging.writer_lock_held"]
    assert [(e["caller"], e["entry_claims"]) for e in held] == [("occupancy.abandon", 0)]
    assert await reservation_count(ledger_engine, attempt) == 0


async def test_forgetting_removes_an_expired_reservation_too(
    ledger_engine: AsyncEngine,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """Abandonment removes the row whatever the clock says, so nothing waits for a sweep."""
    attempt = uuid4()
    await index.reserve(new_key(), attempt, 1024, _EXPIRED)

    await other_index.forget_reservation(attempt)

    assert await reservation_count(ledger_engine, attempt) == 0


class ShrinkingObjectStore(FakeObjectStore):
    """A fake store whose listing loses what it deletes, as the real one does.

    Admission lists again after reconciling, so a deleted orphan that stayed listed would
    look like occupancy and push it into evicting the very winner the reader wants.
    """

    async def delete(self, object_key: str) -> None:
        await super().delete(object_key)
        self.listing.remove(object_key)


def admitting(other_index: PostgresStagingCacheIndex, store: FakeObjectStore) -> StagingOccupancy:
    """Return a writer pod's occupancy whose 1500-byte bound a 1024-byte object pressures.

    Pinned to the fake listing's timestamp, so nothing it lists is past the age bound.
    """
    return StagingOccupancy(
        store,
        other_index,
        max_bytes=1500,
        max_age=timedelta(hours=24),
        reservation_ttl=_LIVE,
        clock=lambda: _WHEN,
    )


async def read_within_bound(
    index: PostgresStagingCacheIndex, key: StagingKey
) -> tuple[StagedObject | None, list[dict[str, object]]]:
    """Read `key`, failing if the entry lock keeps the reader waiting past `_BLOCKED_FOR`."""
    with capture_logs() as events:
        async with asyncio.timeout(_BLOCKED_FOR):
            async with index.hold_for_download(key) as held:
                pass
    return held, [dict(e) for e in events if e["event"] == "staging.reader_lock_wait"]


async def test_a_reader_is_not_blocked_by_an_admitted_upload_whose_admission_reclaimed_its_key(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """The orphan delete's entry claim ended with the admission holding, before the upload."""
    key = new_key()
    winner = staged(key)
    loser = staged(key)
    await index.register(winner)
    store = ShrinkingObjectStore(winner.object_key, loser.object_key)

    # 400 bytes overflow the bound beside both objects and fit once the orphan is gone,
    # so the winner is never an eviction candidate.
    async with admitting(other_index, store).reserve(new_key(), uuid4(), 400) as reservation:
        assert reservation.admission.reclaimed.orphans_deleted == 1
        assert reservation.admission.reclaimed.evicted == ()
        held, waits = await read_within_bound(index, key)

    assert store.deleted == [loser.object_key]
    assert held == winner
    assert [(e["staging_key"], e["registered"]) for e in waits] == [(str(key), True)]


async def test_a_reader_of_a_colliding_key_is_not_blocked_by_an_admitted_upload(
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    colliding_keys: tuple[StagingKey, StagingKey],
    new_key: KeyIssuer,
) -> None:
    """A collision with a reclaimed key costs at most the admission holding, not a transfer."""
    reclaimed, read_key = colliding_keys
    winner = staged(read_key)
    orphan = staged(reclaimed)
    await index.register(winner)
    store = ShrinkingObjectStore(orphan.object_key)

    async with admitting(other_index, store).reserve(new_key(), uuid4(), 1024) as reservation:
        assert reservation.admission.reclaimed.orphans_deleted == 1
        held, waits = await read_within_bound(index, read_key)

    assert held == winner
    assert [(e["staging_key"], e["registered"]) for e in waits] == [(str(read_key), True)]


async def test_a_reader_of_a_key_an_admission_evicted_gets_its_miss_without_waiting(
    index: PostgresStagingCacheIndex, other_index: PostgresStagingCacheIndex, new_key: KeyIssuer
) -> None:
    """The eviction committed with the admission, so the reader learns of it at once."""
    evicted = staged(new_key())
    await index.register(evicted)
    store = ShrinkingObjectStore(evicted.object_key)

    async with admitting(other_index, store).reserve(new_key(), uuid4(), 1024) as reservation:
        assert reservation.admission.reclaimed.evicted == (evicted.key,)
        held, waits = await read_within_bound(index, evicted.key)

    assert store.deleted == [evicted.object_key]
    assert held is None
    assert [(e["staging_key"], e["registered"]) for e in waits] == [(str(evicted.key), False)]
