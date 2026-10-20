"""Occupancy accounting and eviction across independent workers.

Two `PostgresStagingCacheIndex` instances on separate engines stand in for two pods, and
the object store is the real S3 adapter against local RustFS. Both halves matter: a single
connection cannot demonstrate that one worker's bytes or reservations bound another
worker's admission, because advisory locks are re-entrant within a session and a row is
only published once a different session can read it; and an in-memory store cannot
demonstrate that occupancy is measured from a listing that really paginates.

Each test owns a content sub-prefix, so an occupancy total is this test's objects and
nothing else. Registered and reserved byte totals are database-wide and are therefore
never asserted on exactly. A reservation's expiry uses a TTL of zero, which the database
clock has passed in every transaction after the one that wrote it, so nothing sleeps.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest
import structlog
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from structlog.testing import capture_logs

from curate_d.config import S3AddressingStyle, Settings, StagingRole
from curate_d.ledger.tables import staged_objects, staging_reservations
from curate_d.staging.cache_index import PostgresStagingCacheIndex
from curate_d.staging.errors import (
    StagingCapacityExhaustedError,
    StagingObjectStoreError,
    StagingReservationLostError,
)
from curate_d.staging.factories import StagingWriterBackend
from curate_d.staging.keys import StagingKey
from curate_d.staging.object_store import S3StagingObjectStore
from curate_d.staging.objects import StagedObject, StagedObjectRef, StagingObjectStore
from curate_d.staging.occupancy import StagingOccupancy
from curate_d.staging.publish import publish

pytestmark = pytest.mark.integration

_ENDPOINT = "http://127.0.0.1:9100"
_BUCKET = "curate-d-staging"
_WRITER = ("curate-d-staging-writer", "writer-development-secret")

# Small enough that a handful of 64-byte objects creates real pressure, so the tests
# exercise eviction rather than a bound nothing ever reaches.
_PAYLOAD = b"x" * 64

# Long enough that a lock wait is not mistaken for scheduling latency, short enough that
# the suite does not pay for it when the lock behaves.
_BLOCKED_FOR = 0.25

# A reservation that outlives any test, and one the database clock has already expired to
# every later transaction.
_LIVE = timedelta(minutes=10)
_EXPIRED = timedelta(0)


class KeyIssuer:
    """Hands out fresh staging keys and remembers them so the test can clean up."""

    def __init__(self) -> None:
        self.issued: list[StagingKey] = []

    def __call__(self) -> StagingKey:
        key = StagingKey(uuid4().hex)
        self.issued.append(key)
        return key


def writer_settings(prefix: str) -> Settings:
    """Writer-role settings for local RustFS, scoped to one test's content sub-prefix."""
    return Settings(
        staging_s3_endpoint_url=_ENDPOINT,  # type: ignore[arg-type]
        staging_s3_bucket=_BUCKET,
        staging_s3_region="us-east-1",
        staging_s3_role=StagingRole.WRITER,
        staging_s3_access_key_id=_WRITER[0],  # type: ignore[arg-type]
        staging_s3_secret_access_key=_WRITER[1],  # type: ignore[arg-type]
        staging_s3_addressing_style=S3AddressingStyle.PATH,
        staging_s3_verify_tls=False,
        staging_s3_content_prefix=prefix,
    )


@pytest.fixture
def new_key() -> KeyIssuer:
    """Issue staging keys; the rows they name are removed in `_cleanup`."""
    return KeyIssuer()


@pytest.fixture
async def store() -> AsyncIterator[S3StagingObjectStore]:
    """The writer's view of a content sub-prefix nothing else in the suite touches."""
    subject = S3StagingObjectStore(writer_settings(f"content/occupancy-{uuid4().hex}/"))
    try:
        yield subject
    finally:
        # Cleanup runs however the test ended: a failed assertion must not leave billed
        # objects behind any more than a passing one does.
        async for ref in subject.list_objects():
            await subject.delete(ref.object_key)


@pytest.fixture
async def _cleanup(ledger_engine: AsyncEngine, new_key: KeyIssuer) -> AsyncIterator[None]:
    """Delete every row the test's keys named, however the test ended.

    Reservations included: a live one left behind would count against every later
    test's small bound until it expired.
    """
    yield
    async with ledger_engine.begin() as connection:
        await connection.execute(
            delete(staged_objects).where(staged_objects.c.staging_key.in_(new_key.issued))
        )
        await connection.execute(
            delete(staging_reservations).where(
                staging_reservations.c.staging_key.in_(new_key.issued)
            )
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


async def reservation_count(engine: AsyncEngine, key: StagingKey) -> int:
    """Count reservation rows under `key` directly, expired or not."""
    async with engine.connect() as connection:
        result = await connection.execute(
            select(func.count())
            .select_from(staging_reservations)
            .where(staging_reservations.c.staging_key == key)
        )
        return int(result.scalar_one())


async def stage(
    store: S3StagingObjectStore,
    index: PostgresStagingCacheIndex | None,
    key: StagingKey,
    *,
    payload: bytes = _PAYLOAD,
    attempt_id: UUID | None = None,
) -> StagedObject:
    """Upload one verified object, registering it unless `index` is None.

    A None index is how an orphan is made: bytes that landed and bill, with no row
    referring to them, which is what a losing writer leaves behind — or, with a
    reservation for `attempt_id`, what a pod that died before registering leaves.
    """
    target = StagedObject(
        key=key,
        attempt_id=attempt_id or uuid4(),
        size_bytes=len(payload),
        sha256="",
        created_at=datetime.now(UTC),
    )
    uploaded = await store.upload(target, BytesIO(payload), declared_max_bytes=len(payload))
    if index is not None:
        await index.register(uploaded)
    return uploaded


class ThrottledDeleteStore:
    """The writer's store, refusing every deletion after the first it is asked for.

    Models a throttled or timed-out delete part way through a reclamation pass, which is
    the moment the pass's earlier deletions are exposed to a rollback.
    """

    def __init__(self, store: S3StagingObjectStore) -> None:
        self._store = store
        self.deleted: list[str] = []

    async def upload(
        self, target: StagedObject, source: BinaryIO, *, declared_max_bytes: int
    ) -> StagedObject:
        return await self._store.upload(target, source, declared_max_bytes=declared_max_bytes)

    async def download(self, obj: StagedObject, destination: BinaryIO) -> None:
        await self._store.download(obj, destination)

    async def delete(self, object_key: str) -> None:
        if self.deleted:
            raise StagingObjectStoreError(f"deleting {object_key} was throttled")
        await self._store.delete(object_key)
        self.deleted.append(object_key)

    def list_objects(self) -> AsyncIterator[StagedObjectRef]:
        return self._store.list_objects()

    async def abort_incomplete_uploads(self, older_than: datetime) -> int:
        return await self._store.abort_incomplete_uploads(older_than)

    def batch(self) -> AbstractAsyncContextManager[None]:
        return self._store.batch()


def occupancy_for(
    store: StagingObjectStore,
    index: PostgresStagingCacheIndex,
    *,
    max_bytes: int,
    max_age: timedelta = timedelta(hours=24),
    aged_by: timedelta = timedelta(),
    reservation_ttl: timedelta = _LIVE,
) -> StagingOccupancy:
    """A subject whose clock is offset by `aged_by`, so age eviction needs no waiting."""
    return StagingOccupancy(
        store,
        index,
        max_bytes=max_bytes,
        max_age=max_age,
        reservation_ttl=reservation_ttl,
        clock=lambda: datetime.now(UTC) + aged_by,
    )


def writer_backend(
    store: StagingObjectStore, index: PostgresStagingCacheIndex, occupancy: StagingOccupancy
) -> StagingWriterBackend:
    """Assemble the writer side `publish` runs against."""
    return StagingWriterBackend(
        store=store, index=index, occupancy=occupancy, upload_max_age=timedelta(hours=1)
    )


async def test_another_workers_objects_count_against_this_workers_budget(
    store: S3StagingObjectStore,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """Occupancy is billed bytes, not this session's rows, so it is read from the listing."""
    await stage(store, index, new_key())
    await stage(store, index, new_key())

    report = await occupancy_for(store, other_index, max_bytes=256).measure()

    assert report.total_bytes == 128
    assert report.object_count == 2


async def test_capacity_exhausted_when_another_workers_entry_cannot_be_freed(
    store: S3StagingObjectStore,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """A download on another connection protects the only candidate, so admission fails."""
    held = await stage(store, index, new_key())
    subject = occupancy_for(store, other_index, max_bytes=100)

    async with index.hold_for_download(held.key):
        with pytest.raises(StagingCapacityExhaustedError, match="being downloaded"):
            async with subject.reserve(new_key(), uuid4(), 64):
                pass

    assert await other_index.lookup(held.key) == held
    assert [ref.object_key async for ref in store.list_objects()] == [held.object_key]


async def test_pressure_evicts_another_workers_idle_entry(
    store: S3StagingObjectStore,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """Nothing holds the entry, so the second worker may reclaim the bytes and the row."""
    idle = await stage(store, index, new_key())
    subject = occupancy_for(store, other_index, max_bytes=100)

    async with subject.reserve(new_key(), uuid4(), 64) as reservation:
        assert reservation.admission.reclaimed.evicted == (idle.key,)
        # Gone before the upload starts: unregistered in the admission holding, which has
        # committed, and the bytes deleted after it. A reader is told so at once.
        async with asyncio.timeout(_BLOCKED_FOR):
            async with index.hold_for_download(idle.key) as held:
                assert held is None
        assert [ref async for ref in store.list_objects()] == []

    assert await index.lookup(idle.key) is None


async def test_an_orphan_counts_as_occupancy_and_is_reclaimed_first(
    store: S3StagingObjectStore,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """A losing writer's bytes bill like a winner's until reconciliation collects them."""
    live = await stage(store, index, new_key())
    stranded = await stage(store, None, new_key())
    subject = occupancy_for(store, other_index, max_bytes=192)

    assert (await subject.measure()).total_bytes == 128

    async with subject.reserve(new_key(), uuid4(), 128) as reservation:
        assert reservation.admission.reclaimed.orphans_deleted == 1
        assert reservation.admission.reclaimed.evicted == ()

    assert await index.lookup(live.key) == live
    assert [ref.object_key async for ref in store.list_objects()] == [live.object_key]
    assert stranded.object_key != live.object_key


async def test_another_workers_admission_proceeds_beside_an_open_reservation(
    store: S3StagingObjectStore,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """An open reservation holds no lock, so a second pod's fitting admission does not wait."""
    subject = occupancy_for(store, index, max_bytes=256)
    other = occupancy_for(store, other_index, max_bytes=256)

    async def admit() -> int:
        async with other.reserve(new_key(), uuid4(), 64) as reservation:
            return reservation.admission.occupancy.reserved_bytes

    async with subject.reserve(new_key(), uuid4(), 64):
        reserved_beside = await asyncio.wait_for(admit(), timeout=_BLOCKED_FOR)

    assert reserved_beside >= 64


async def test_an_open_reservation_spends_headroom_its_bytes_have_not_used(
    store: S3StagingObjectStore,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """The listing shows none of the reserved bytes, yet the second admission is refused."""
    subject = occupancy_for(store, index, max_bytes=100)
    other = occupancy_for(store, other_index, max_bytes=100)

    async with subject.reserve(new_key(), uuid4(), 64):
        assert [ref async for ref in store.list_objects()] == []
        with pytest.raises(StagingCapacityExhaustedError, match="reserved"):
            async with other.reserve(new_key(), uuid4(), 64):
                pytest.fail("two pods spent the same headroom")


async def test_a_cancelled_upload_leaves_no_reservation_and_no_registration(
    store: S3StagingObjectStore,
    other_index: PostgresStagingCacheIndex,
    ledger_engine: AsyncEngine,
    new_key: KeyIssuer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A drained worker's headroom returns with its cancellation, and nothing is readable."""
    key = new_key()
    uploading = asyncio.Event()

    async def stalled_upload(
        target: StagedObject, source: BinaryIO, *, declared_max_bytes: int
    ) -> StagedObject:
        """Start an upload that never finishes."""
        uploading.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(store, "upload", stalled_upload)
    backend = writer_backend(store, other_index, occupancy_for(store, other_index, max_bytes=256))

    publishing = asyncio.create_task(
        publish(backend, key, BytesIO(_PAYLOAD), size_bytes=len(_PAYLOAD))
    )
    await uploading.wait()
    assert await reservation_count(ledger_engine, key) == 1
    publishing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await publishing

    assert await reservation_count(ledger_engine, key) == 0
    assert await other_index.lookup(key) is None
    assert [ref async for ref in store.list_objects()] == []


async def test_a_registration_after_expiry_publishes_nothing_and_removes_its_bytes(
    store: S3StagingObjectStore,
    other_index: PostgresStagingCacheIndex,
    ledger_engine: AsyncEngine,
    new_key: KeyIssuer,
) -> None:
    """An upload that outlived its reservation may name bytes reconciliation already took."""
    key = new_key()
    occupancy = occupancy_for(store, other_index, max_bytes=256, reservation_ttl=_EXPIRED)

    with capture_logs() as events, pytest.raises(StagingReservationLostError):
        await publish(
            writer_backend(store, other_index, occupancy),
            key,
            BytesIO(_PAYLOAD),
            size_bytes=len(_PAYLOAD),
        )

    assert await other_index.lookup(key) is None
    assert await reservation_count(ledger_engine, key) == 0
    assert [ref async for ref in store.list_objects()] == []
    [lost] = [e for e in events if e["event"] == "staging.reservation_lost"]
    assert (lost["log_level"], lost["staging_key"], lost["reserved_bytes"]) == (
        "warning",
        str(key),
        len(_PAYLOAD),
    )


async def test_a_crashed_pods_reservation_counts_until_it_expires(
    store: S3StagingObjectStore,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """Its completed object is spared while the reservation lives, and counted twice."""
    crashed = await stage(store, None, new_key())
    await other_index.reserve(crashed.key, crashed.attempt_id, len(_PAYLOAD), _LIVE)
    subject = occupancy_for(store, index, max_bytes=100)

    with pytest.raises(StagingCapacityExhaustedError, match="reserved"):
        async with subject.reserve(new_key(), uuid4(), 1):
            pytest.fail("a dead pod's live reservation must still count")

    assert [ref.object_key async for ref in store.list_objects()] == [crashed.object_key]


async def test_a_crashed_pods_object_is_collected_once_its_reservation_expired(
    store: S3StagingObjectStore,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    ledger_engine: AsyncEngine,
    new_key: KeyIssuer,
) -> None:
    """Past its expiry the reservation protects nothing, so the next sweep reclaims both."""
    crashed = await stage(store, None, new_key())
    await other_index.reserve(crashed.key, crashed.attempt_id, len(_PAYLOAD), _EXPIRED)
    subject = occupancy_for(store, index, max_bytes=100)

    assert (await subject.sweep()).orphans_deleted == 1

    assert [ref async for ref in store.list_objects()] == []
    assert await reservation_count(ledger_engine, crashed.key) == 0


async def test_release_frees_capacity_for_another_worker_and_repeats_safely(
    store: S3StagingObjectStore,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """Completion returns the budget promptly, and a retried release is not an error."""
    obj = await stage(store, index, new_key())
    subject = occupancy_for(store, index, max_bytes=100)

    first = await subject.release(obj.key)
    second = await occupancy_for(store, other_index, max_bytes=100).release(obj.key)

    assert (first.deleted, first.freed_bytes) == (True, 64)
    assert (second.deleted, second.freed_bytes) == (False, 0)
    assert await other_index.lookup(obj.key) is None
    assert (await subject.measure()).total_bytes == 0


async def test_release_during_another_workers_download_keeps_the_bytes(
    store: S3StagingObjectStore,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """Unpublishing is immediate; the bytes wait for the reader that is still streaming."""
    obj = await stage(store, index, new_key())
    subject = occupancy_for(store, other_index, max_bytes=256)

    async with index.hold_for_download(obj.key):
        released = await subject.release(obj.key)
        assert released.deleted is False
        assert [ref.object_key async for ref in store.list_objects()] == [obj.object_key]

    assert await index.lookup(obj.key) is None
    assert (await subject.sweep()).orphans_deleted == 1


async def test_a_sweep_reclaims_aged_entries_without_any_admission(
    store: S3StagingObjectStore,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """Cleanup runs on a schedule, so an idle cluster stops billing for stale content."""
    stale = await stage(store, index, new_key())
    subject = occupancy_for(
        store, other_index, max_bytes=4096, max_age=timedelta(minutes=1), aged_by=timedelta(hours=1)
    )

    reclaimed = await subject.sweep()

    assert reclaimed.evicted == (stale.key,)
    assert reclaimed.freed_bytes == 64
    assert await index.lookup(stale.key) is None
    assert [ref async for ref in store.list_objects()] == []


async def test_a_failed_deletion_mid_sweep_keeps_the_rows_it_already_removed(
    store: S3StagingObjectStore,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
) -> None:
    """A rolled-back unregistration republishes a key whose bytes the pass already deleted."""
    entries = [await stage(store, index, new_key()) for _ in range(2)]
    throttled = ThrottledDeleteStore(store)
    subject = occupancy_for(
        throttled,
        other_index,
        max_bytes=4096,
        max_age=timedelta(minutes=1),
        aged_by=timedelta(hours=1),
    )

    with pytest.raises(StagingObjectStoreError, match="throttled"):
        await subject.sweep()

    assert len(throttled.deleted) == 1
    assert throttled.deleted[0] not in [ref.object_key async for ref in store.list_objects()]
    # Neither row survives the pass: the deleted entry's row would name bytes that are
    # gone, and the refused entry's object is an orphan reconciliation can still collect.
    assert [await index.lookup(entry.key) for entry in entries] == [None, None]

    repaired = occupancy_for(store, other_index, max_bytes=4096)
    assert (await repaired.sweep()).orphans_deleted == 1
    assert [ref async for ref in store.list_objects()] == []


async def test_a_reclaiming_publication_splits_its_hold_into_phases(
    store: S3StagingObjectStore,
    index: PostgresStagingCacheIndex,
    other_index: PostgresStagingCacheIndex,
    new_key: KeyIssuer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The admission holding ends before the upload starts, and registration takes its own."""
    idle = await stage(store, index, new_key())
    key = new_key()
    backend = writer_backend(store, other_index, occupancy_for(store, other_index, max_bytes=100))
    upload = store.upload

    async def marked_upload(
        target: StagedObject, source: BinaryIO, *, declared_max_bytes: int
    ) -> StagedObject:
        """Upload, logging when it starts so the holdings can be ordered around it."""
        structlog.get_logger().info("test.upload_started")
        return await upload(target, source, declared_max_bytes=declared_max_bytes)

    monkeypatch.setattr(store, "upload", marked_upload)

    with capture_logs() as events:
        published = await publish(backend, key, BytesIO(_PAYLOAD), size_bytes=len(_PAYLOAD))

    assert published.staged.key == key
    assert await index.lookup(idle.key) is None
    [phases] = [e for e in events if e["event"] == "staging.reservation_phases"]
    assert phases["outcome"] == "registered"
    assert phases["evicted"] >= 1
    assert phases["admission_seconds"] >= 0
    holdings = [
        (e["caller"], e)
        for e in events
        if e["event"] == "staging.writer_lock_held"
        and e["caller"] in ("occupancy.reserve", "occupancy.register")
    ]
    assert [caller for caller, _ in holdings] == ["occupancy.reserve", "occupancy.register"]
    reserve, register = (e for _, e in holdings)
    assert reserve["entry_claims"] >= 1
    assert register["entry_claims"] == 0
    # Each holding's event is logged when its transaction ends, so the order is the proof
    # that neither spans the upload.
    started = next(i for i, e in enumerate(events) if e["event"] == "test.upload_started")
    assert events.index(reserve) < started < events.index(register)
