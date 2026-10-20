"""Occupancy accounting, eviction and release against controlled fakes.

Both backends are fakes here, because the properties under test are arithmetic and
ordering rather than S3 or PostgreSQL behaviour: that a listing is consumed to its last
page, that another worker's bytes count against this worker's budget, that a held entry
survives pressure, and that a failed delete is never counted as freed space. The fake
index records whether the writer lock was held for every operation, so "cleanup runs
under the writer lock" is asserted rather than assumed.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from curate_d.staging import occupancy as occupancy_module
from curate_d.staging.errors import (
    StagingCapacityExhaustedError,
    StagingObjectStoreError,
    StagingReservationLostError,
)
from curate_d.staging.keys import StagingKey
from curate_d.staging.objects import (
    StagedObject,
    StagedObjectRef,
    StagingObjectStore,
)
from curate_d.staging.occupancy import (
    CoordinatedStagingIndex,
    OccupancyReport,
    Reservation,
    StagingOccupancy,
    list_staged_objects,
    measure_occupancy,
)

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_DIGEST = "b" * 64
_MAX_AGE = timedelta(hours=24)
_TTL = timedelta(minutes=10)
_KEY = StagingKey("f" * 32)


class FakeObjectStore:
    """Serves its listing in fixed-size pages and records every deletion it is asked for.

    Also models the real store's connection discipline: an operation runs on the client a
    surrounding `batch()` pinned, or on one opened for itself alone. Clients are numbered
    rather than counted, so a test can assert that N deletes travelled on one connection
    without depending on how many the rest of the pass opened.
    """

    def __init__(self, *, page_size: int = 1000, fail_after: int | None = None) -> None:
        self.objects: dict[str, StagedObjectRef] = {}
        self.deleted: list[str] = []
        self.rejects: set[str] = set()
        self.page_size = page_size
        self.fail_after = fail_after
        self.pages_served = 0
        self.clients_opened = 0
        self.delete_clients: list[int] = []
        self._pinned: int | None = None

    @asynccontextmanager
    async def batch(self) -> AsyncGenerator[None, None]:
        """Pin one client for the operations issued inside the scope."""
        pinned, self._pinned = self._pinned, self._open_client()
        try:
            yield
        finally:
            self._pinned = pinned

    async def upload(
        self, target: StagedObject, source: BinaryIO, *, declared_max_bytes: int
    ) -> StagedObject:
        raise NotImplementedError

    async def download(self, obj: StagedObject, destination: BinaryIO) -> None:
        raise NotImplementedError

    async def delete(self, object_key: str) -> None:
        self.delete_clients.append(self._client())
        if object_key in self.rejects:
            raise StagingObjectStoreError(f"deleting {object_key} was rejected")
        self.objects.pop(object_key, None)
        self.deleted.append(object_key)

    async def list_objects(self) -> AsyncIterator[StagedObjectRef]:
        self._client()
        # Snapshotted so a caller deleting while it iterates behaves like a paginator
        # reading pages the server already assembled.
        for served, ref in enumerate(list(self.objects.values())):
            if served % self.page_size == 0:
                self.pages_served += 1
            if served == self.fail_after:
                raise StagingObjectStoreError("the staging listing stopped part way")
            yield ref

    async def abort_incomplete_uploads(self, older_than: datetime) -> int:
        raise NotImplementedError

    def _client(self) -> int:
        """Return the client this operation runs on: the pinned one, or a fresh one."""
        return self._pinned if self._pinned is not None else self._open_client()

    def _open_client(self) -> int:
        """Open the next numbered client, standing in for a connect and a handshake."""
        self.clients_opened += 1
        return self.clients_opened


@dataclass(frozen=True)
class FakeReservation:
    """One in-flight attempt's claim on headroom, expiring by the fake index's clock."""

    key: StagingKey
    attempt_id: UUID
    reserved_bytes: int
    expires_at: datetime

    @property
    def object_key(self) -> str:
        """Return the remote key the reserved attempt uploads to."""
        return f"{self.key}/{self.attempt_id.hex}"


class FakeCacheIndex:
    """An in-memory index with the writer lock, entry holds and eviction the real one has."""

    def __init__(self, *, clock: Callable[[], datetime] = lambda: _NOW) -> None:
        self.registrations: dict[StagingKey, StagedObject] = {}
        self.reservations: dict[UUID, FakeReservation] = {}
        self.held: set[StagingKey] = set()
        self.released: list[StagingKey] = []
        self.lock_depth = 0
        self.lock_callers: list[str] = []
        self.operations: list[tuple[str, int]] = []
        self.clock = clock

    @asynccontextmanager
    async def writer_lock(self, caller: str) -> AsyncGenerator[object, None]:
        """Count the holding, re-entrantly, the way the PostgreSQL lock behaves."""
        self.lock_callers.append(caller)
        self.lock_depth += 1
        try:
            yield self
        finally:
            self.lock_depth -= 1

    async def register(self, obj: StagedObject) -> None:
        self._observe("register")
        self.registrations.setdefault(obj.key, obj)

    async def lookup(self, key: StagingKey) -> StagedObject | None:
        return self.registrations.get(key)

    async def release(self, key: StagingKey) -> None:
        self._observe("release")
        self.released.append(key)
        self.registrations.pop(key, None)

    async def total_registered_bytes(self) -> int:
        return sum(obj.size_bytes for obj in self.registrations.values())

    async def evict(self, key: StagingKey) -> StagedObject | None:
        self._observe("evict")
        if key in self.held:
            return None
        return self.registrations.pop(key, None)

    async def reconcile(self, store: StagingObjectStore) -> int:
        self._observe("reconcile")
        self._expire()
        protected = {obj.object_key for obj in self.registrations.values()}
        protected |= {row.object_key for row in self.reservations.values()}
        deleted = 0
        async for ref in store.list_objects():
            if ref.object_key in protected or _key_of(ref) in self.held:
                continue
            await store.delete(ref.object_key)
            deleted += 1
        return deleted

    async def reserve(
        self, key: StagingKey, attempt_id: UUID, reserved_bytes: int, ttl: timedelta
    ) -> None:
        self._observe("reserve")
        self.reservations[attempt_id] = FakeReservation(
            key=key,
            attempt_id=attempt_id,
            reserved_bytes=reserved_bytes,
            expires_at=self.clock() + ttl,
        )

    async def consume_reservation(self, attempt_id: UUID) -> bool:
        """Remove the reservation only if live, leaving an expired row as PostgreSQL does."""
        self._observe("consume_reservation")
        row = self.reservations.get(attempt_id)
        if row is None or row.expires_at <= self.clock():
            return False
        del self.reservations[attempt_id]
        return True

    async def forget_reservation(self, attempt_id: UUID) -> None:
        self._observe("forget_reservation")
        self.reservations.pop(attempt_id, None)

    async def total_reserved_bytes(self) -> int:
        now = self.clock()
        return sum(row.reserved_bytes for row in self.reservations.values() if row.expires_at > now)

    def _expire(self) -> None:
        """Drop reservations whose expiry has passed, as reconciliation does in PostgreSQL."""
        now = self.clock()
        self.reservations = {
            attempt: row for attempt, row in self.reservations.items() if row.expires_at > now
        }

    def _observe(self, operation: str) -> None:
        """Record the writer-lock depth an operation ran at, for the tests to assert on."""
        self.operations.append((operation, self.lock_depth))


class LockDepthStore(FakeObjectStore):
    """Records the writer-lock depth in force at each deletion it is asked for."""

    def __init__(self, index: FakeCacheIndex) -> None:
        super().__init__()
        self._index = index
        self.depths: list[int] = []

    async def delete(self, object_key: str) -> None:
        self.depths.append(self._index.lock_depth)
        await super().delete(object_key)


class ReleasingIndex(FakeCacheIndex):
    """An index whose reader lets go between reconciliation and eviction.

    The only window in which `_unpublish` sees a losing attempt at all: reconciliation skips
    the orphan because a download holds its key, the download then ends, and eviction
    for that same key succeeds against the winner instead.
    """

    @classmethod
    def wrap(cls, other: FakeCacheIndex) -> "ReleasingIndex":
        """Return a releasing index over another fake's registrations and holds."""
        index = cls()
        index.registrations = other.registrations
        index.held = other.held
        return index

    async def reconcile(self, store: StagingObjectStore) -> int:
        deleted = await super().reconcile(store)
        self.held.clear()
        return deleted


def _key_of(ref: StagedObjectRef) -> StagingKey:
    """The staging key an object name belongs to."""
    return StagingKey(ref.object_key.split("/", 1)[0])


def publish(
    index: FakeCacheIndex,
    store: FakeObjectStore,
    *,
    seed: int,
    size: int,
    age: timedelta = timedelta(),
) -> StagedObject:
    """Put one completed, registered object into both fakes."""
    obj = StagedObject(
        key=StagingKey(f"{seed:032x}"),
        attempt_id=UUID(int=seed),
        size_bytes=size,
        sha256=_DIGEST,
        created_at=_NOW - age,
    )
    index.registrations[obj.key] = obj
    store.objects[obj.object_key] = StagedObjectRef(
        object_key=obj.object_key, size_bytes=size, last_modified=_NOW - age
    )
    return obj


def orphan(
    store: FakeObjectStore, *, seed: int, size: int, age: timedelta = timedelta()
) -> StagedObjectRef:
    """Put one completed object with no registration into the store alone."""
    ref = StagedObjectRef(
        object_key=f"{seed:032x}/{UUID(int=seed).hex}",
        size_bytes=size,
        last_modified=_NOW - age,
    )
    store.objects[ref.object_key] = ref
    return ref


def uploaded(store: FakeObjectStore, attempt_id: UUID, *, size: int) -> StagedObject:
    """Put one completed attempt under the test key into the store, as its upload would."""
    obj = StagedObject(
        key=_KEY, attempt_id=attempt_id, size_bytes=size, sha256=_DIGEST, created_at=_NOW
    )
    store.objects[obj.object_key] = StagedObjectRef(
        object_key=obj.object_key, size_bytes=size, last_modified=_NOW
    )
    return obj


def occupancy_for(
    store: FakeObjectStore,
    index: FakeCacheIndex,
    *,
    max_bytes: int,
    max_age: timedelta = _MAX_AGE,
) -> StagingOccupancy:
    """A subject pinned to the test's clock so age eviction does not depend on runtime."""
    return StagingOccupancy(
        store,
        index,
        max_bytes=max_bytes,
        max_age=max_age,
        reservation_ttl=_TTL,
        clock=lambda: _NOW,
    )


def reserving(
    subject: StagingOccupancy, declared_max_bytes: int, *, attempt_id: UUID | None = None
) -> AbstractAsyncContextManager[Reservation]:
    """Open a reservation under the test key, for a fresh attempt unless one is given."""
    return subject.reserve(_KEY, attempt_id or uuid4(), declared_max_bytes)


@pytest.fixture
def store() -> FakeObjectStore:
    return FakeObjectStore()


@pytest.fixture
def index() -> FakeCacheIndex:
    return FakeCacheIndex()


def test_the_fakes_satisfy_the_contracts_they_stand_in_for(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """These tests exercise the same surfaces the S3 and PostgreSQL backends implement."""
    assert isinstance(store, StagingObjectStore)
    assert isinstance(index, CoordinatedStagingIndex)


def test_a_bound_that_refuses_everything_is_rejected_at_construction(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A zero budget is a misconfiguration, not a staging area that holds nothing."""
    with pytest.raises(ValueError, match="max_bytes"):
        StagingOccupancy(store, index, max_bytes=0, max_age=_MAX_AGE, reservation_ttl=_TTL)


def test_an_age_bound_of_zero_is_rejected_at_construction(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A zero age bound would evict content the moment it was published."""
    with pytest.raises(ValueError, match="max_age"):
        StagingOccupancy(store, index, max_bytes=1024, max_age=timedelta(), reservation_ttl=_TTL)


def test_a_negative_reservation_ttl_is_rejected_at_construction(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A reservation that expired before it was written would lose every upload."""
    with pytest.raises(ValueError, match="reservation_ttl"):
        StagingOccupancy(
            store, index, max_bytes=1024, max_age=_MAX_AGE, reservation_ttl=-timedelta(seconds=1)
        )


def test_the_reservation_ttl_is_kept_as_given(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Zero is allowed: it expires a reservation in the next transaction, for tests."""
    subject = StagingOccupancy(
        store, index, max_bytes=1024, max_age=_MAX_AGE, reservation_ttl=timedelta()
    )
    assert subject.reservation_ttl == timedelta()


async def test_occupancy_counts_every_page_of_the_listing(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A truncated listing understates usage, so the pages are consumed to the last one."""
    store.page_size = 2
    for seed in range(5):
        publish(index, store, seed=seed, size=100)

    report = await measure_occupancy(store, index)

    assert store.pages_served == 3
    assert report == OccupancyReport(total_bytes=500, object_count=5, registered_bytes=500)


async def test_occupancy_counts_objects_this_worker_never_registered(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Another worker's completed bytes are billed here too; rows alone would miss them."""
    orphan(store, seed=1, size=4096)

    report = await measure_occupancy(store, index)

    assert report.total_bytes == 4096
    assert report.registered_bytes == 0


async def test_unreferenced_bytes_are_reported_beside_the_registered_total(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Orphan overhead is visible before it shows up as a shortfall at admission."""
    publish(index, store, seed=1, size=600)
    orphan(store, seed=2, size=400)

    report = await measure_occupancy(store, index)

    assert report.unreferenced_bytes == 400


async def test_a_registration_without_its_object_is_not_negative_occupancy(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A remotely expired object leaves a stale row, which readers invalidate on miss."""
    publish(index, store, seed=1, size=600)
    store.objects.clear()

    assert (await measure_occupancy(store, index)).unreferenced_bytes == 0


async def test_a_listing_that_stops_part_way_fails_rather_than_understating(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """An uncertain occupancy is not a small one; admission must not be decided on it."""
    for seed in range(4):
        publish(index, store, seed=seed, size=100)
    store.fail_after = 2

    with pytest.raises(StagingObjectStoreError, match="stopped part way"):
        await measure_occupancy(store, index)


async def test_a_failed_listing_admits_nothing_and_evicts_nothing(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Failing closed means no object is deleted on the strength of an answer nobody has."""
    publish(index, store, seed=1, size=100, age=timedelta(days=9))
    store.fail_after = 0
    subject = occupancy_for(store, index, max_bytes=1000)

    with pytest.raises(StagingObjectStoreError):
        async with reserving(subject, 10):
            pass

    assert store.deleted == []
    assert index.registrations


async def test_the_listing_is_ordered_oldest_first(store: FakeObjectStore) -> None:
    """Eviction order is a property of the whole listing, so it is sorted once, here."""
    newest = orphan(store, seed=1, size=1, age=timedelta(hours=1))
    oldest = orphan(store, seed=2, size=1, age=timedelta(hours=9))
    middle = orphan(store, seed=3, size=1, age=timedelta(hours=5))

    assert await list_staged_objects(store) == [oldest, middle, newest]


async def test_admission_reserves_the_declared_maximum_before_the_stream(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """The upper bound is what is reserved; the actual size is not known until later."""
    publish(index, store, seed=1, size=400)
    subject = occupancy_for(store, index, max_bytes=1000)
    attempt = uuid4()

    async with reserving(subject, 600, attempt_id=attempt) as reservation:
        assert reservation.admission.reserved_bytes == 600
        assert reservation.admission.occupancy.total_bytes == 400
        assert index.reservations[attempt].reserved_bytes == 600
        assert index.reservations[attempt].key == _KEY

    assert store.deleted == []


async def test_no_lock_is_held_while_the_reservation_is_open(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """The reservation row keeps the headroom, so the upload runs outside every holding."""
    subject = occupancy_for(store, index, max_bytes=1000)

    async with reserving(subject, 100):
        assert index.lock_depth == 0
        assert ("reserve", 1) in index.operations


async def test_a_reservation_registers_and_is_consumed_in_one_holding(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """The row and the reservation's removal commit together, under the register holding."""
    subject = occupancy_for(store, index, max_bytes=1000)
    attempt = uuid4()

    async with reserving(subject, 100, attempt_id=attempt) as reservation:
        index.operations.clear()
        index.lock_callers.clear()
        await reservation.register(uploaded(store, attempt, size=100))
        assert reservation.registered
        assert reservation.registered_at is not None

    assert index.operations == [("consume_reservation", 1), ("register", 1)]
    assert index.lock_callers == ["occupancy.register"]
    assert index.reservations == {}
    assert index.registrations[_KEY].attempt_id == attempt


async def test_the_bound_is_inclusive_at_its_last_byte(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Exactly full is admissible; one byte past it is not, and nothing frees that byte."""
    held = publish(index, store, seed=1, size=400)
    index.held.add(held.key)
    subject = occupancy_for(store, index, max_bytes=1000)

    async with reserving(subject, 600) as reservation:
        assert reservation.admission.reclaimed.evicted == ()

    with pytest.raises(StagingCapacityExhaustedError):
        async with reserving(subject, 601):
            pass


async def test_content_larger_than_the_whole_bound_is_refused_outright(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Evicting everything could not make room, so nothing is evicted trying."""
    publish(index, store, seed=1, size=100, age=timedelta(days=9))
    subject = occupancy_for(store, index, max_bytes=1000)

    with pytest.raises(StagingCapacityExhaustedError, match="never fit"):
        async with reserving(subject, 1001):
            pass

    assert store.deleted == []


async def test_a_negative_declaration_is_a_programming_error(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A negative reservation would credit capacity rather than spend it."""
    subject = occupancy_for(store, index, max_bytes=1000)

    with pytest.raises(ValueError, match="nonnegative"):
        async with reserving(subject, -1):
            pass


async def test_empty_content_is_admitted_against_a_full_volume(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Zero-length files stage normally; an empty object occupies no budget."""
    publish(index, store, seed=1, size=1000)
    subject = occupancy_for(store, index, max_bytes=1000)

    async with reserving(subject, 0) as reservation:
        assert reservation.admission.reserved_bytes == 0


async def test_pressure_evicts_the_oldest_entries_first(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """The least recently staged entry is the one whose re-fetch is least likely needed."""
    newest = publish(index, store, seed=1, size=300, age=timedelta(hours=1))
    oldest = publish(index, store, seed=2, size=300, age=timedelta(hours=3))
    middle = publish(index, store, seed=3, size=300, age=timedelta(hours=2))
    subject = occupancy_for(store, index, max_bytes=1000)

    async with reserving(subject, 500) as reservation:
        assert reservation.admission.reclaimed.evicted == (oldest.key, middle.key)

    assert store.deleted == [oldest.object_key, middle.object_key]
    assert newest.key in index.registrations


async def test_pressure_stops_evicting_once_the_content_fits(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Eviction makes room; it is not a purge, and a warm cache is the point of staging."""
    oldest = publish(index, store, seed=1, size=400, age=timedelta(hours=3))
    for seed in (2, 3):
        publish(index, store, seed=seed, size=200, age=timedelta(hours=1))
    subject = occupancy_for(store, index, max_bytes=1000)

    async with reserving(subject, 300) as reservation:
        assert reservation.admission.reclaimed.evicted == (oldest.key,)
        assert reservation.admission.occupancy.total_bytes == 400


async def test_unreferenced_objects_are_reclaimed_before_live_entries(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """An orphan is pure waste, so freeing it can spare content someone is about to read."""
    live = publish(index, store, seed=1, size=400, age=timedelta(hours=3))
    stale = orphan(store, seed=2, size=400, age=timedelta(hours=9))
    subject = occupancy_for(store, index, max_bytes=1000)

    async with reserving(subject, 500) as reservation:
        assert reservation.admission.reclaimed.orphans_deleted == 1
        assert reservation.admission.reclaimed.evicted == ()

    assert store.deleted == [stale.object_key]
    assert live.key in index.registrations


async def test_an_entry_being_downloaded_is_never_evicted_under_pressure(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Admission fails rather than truncating a read; that is the whole protection."""
    held = publish(index, store, seed=1, size=900, age=timedelta(hours=9))
    index.held.add(held.key)
    subject = occupancy_for(store, index, max_bytes=1000)

    with pytest.raises(StagingCapacityExhaustedError, match="being downloaded"):
        async with reserving(subject, 500):
            pass

    assert store.deleted == []
    assert index.registrations[held.key] == held


async def test_a_protected_entry_does_not_stop_an_admission_that_still_fits(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Only the candidates that were needed matter; a held entry is skipped, not fatal."""
    held = publish(index, store, seed=1, size=300, age=timedelta(hours=9))
    index.held.add(held.key)
    evictable = publish(index, store, seed=2, size=400, age=timedelta(hours=8))
    subject = occupancy_for(store, index, max_bytes=1000)

    async with reserving(subject, 600) as reservation:
        assert reservation.admission.reclaimed.protected == (held.key,)
        assert reservation.admission.reclaimed.evicted == (evictable.key,)


async def test_a_failed_deletion_during_eviction_is_surfaced(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Space nobody proved was freed must not be handed to the next admission."""
    doomed = publish(index, store, seed=1, size=600, age=timedelta(hours=9))
    store.rejects.add(doomed.object_key)
    subject = occupancy_for(store, index, max_bytes=1000)

    with pytest.raises(StagingObjectStoreError, match="rejected"):
        async with reserving(subject, 600):
            pytest.fail("an admission whose eviction failed must not run its block")

    assert store.objects[doomed.object_key].size_bytes == 600
    # Abandoned once the deletion failed: the orphan counts in the listing instead.
    assert index.reservations == {}
    assert doomed.key not in index.registrations


async def test_reclamation_runs_while_the_writer_lock_is_held(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Reconciliation that raced a publication would collect an object as an orphan."""
    publish(index, store, seed=1, size=900, age=timedelta(hours=9))
    subject = occupancy_for(store, index, max_bytes=1000)

    async with reserving(subject, 500):
        pass

    assert index.operations
    assert all(depth >= 1 for _, depth in index.operations)


async def test_a_sweep_evicts_aged_entries_without_any_admission(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Cleanup has to reclaim when no new jobs arrive, or an idle cluster keeps billing."""
    stale = publish(index, store, seed=1, size=100, age=timedelta(hours=25))
    fresh = publish(index, store, seed=2, size=100, age=timedelta(hours=1))
    subject = occupancy_for(store, index, max_bytes=10_000)

    reclaimed = await subject.sweep()

    assert reclaimed.evicted == (stale.key,)
    assert reclaimed.freed_bytes == 100
    assert store.deleted == [stale.object_key]
    assert fresh.key in index.registrations


async def test_a_sweep_reclaims_unreferenced_objects_too(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A losing writer's bytes bill exactly like a winner's until something deletes them."""
    stale = orphan(store, seed=1, size=100, age=timedelta(minutes=5))
    subject = occupancy_for(store, index, max_bytes=10_000)

    reclaimed = await subject.sweep()

    assert reclaimed.orphans_deleted == 1
    assert store.deleted == [stale.object_key]


async def test_a_failed_deletion_mid_sweep_keeps_the_earlier_unregistrations(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A restored row naming deleted bytes is unrepairable; an orphan is not."""
    first = publish(index, store, seed=1, size=100, age=timedelta(hours=27))
    refused = publish(index, store, seed=2, size=100, age=timedelta(hours=26))
    last = publish(index, store, seed=3, size=100, age=timedelta(hours=25))
    store.rejects.add(refused.object_key)
    subject = occupancy_for(store, index, max_bytes=10_000)

    with pytest.raises(StagingObjectStoreError, match="next reconciliation collects"):
        await subject.sweep()

    # One refusal ends neither the pass nor the unregistrations it already made.
    assert store.deleted == [first.object_key, last.object_key]
    assert index.registrations == {}
    assert refused.object_key in store.objects


async def test_a_failed_deletion_mid_sweep_leaves_a_collectable_orphan(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """The bytes that survived their row are what the next reconciliation is for."""
    doomed = publish(index, store, seed=1, size=100, age=timedelta(hours=25))
    store.rejects.add(doomed.object_key)
    subject = occupancy_for(store, index, max_bytes=10_000)

    with pytest.raises(StagingObjectStoreError):
        await subject.sweep()
    store.rejects.clear()

    assert (await subject.sweep()).orphans_deleted == 1
    assert store.objects == {}


async def test_an_older_losing_attempt_evicts_its_key_once_and_charges_the_winner(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """The bytes deleted are the registration's, so the size credited must be too."""
    winner = publish(index, store, seed=1, size=400, age=timedelta(hours=24))
    losing = StagedObjectRef(
        object_key=f"{winner.key}/{UUID(int=99).hex}",
        size_bytes=900,
        last_modified=_NOW - timedelta(hours=25),
    )
    store.objects[losing.object_key] = losing
    index.held.add(winner.key)
    subject = occupancy_for(store, ReleasingIndex.wrap(index), max_bytes=10_000)

    reclaimed = await subject.sweep()

    assert reclaimed.evicted == (winner.key,)
    assert reclaimed.protected == ()
    assert reclaimed.freed_bytes == 400
    assert store.deleted == [winner.object_key]
    assert losing.object_key in store.objects


async def test_a_sweep_spares_an_aged_entry_a_download_holds(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Age is not authority to delete bytes a reader is still streaming."""
    held = publish(index, store, seed=1, size=100, age=timedelta(hours=25))
    index.held.add(held.key)
    subject = occupancy_for(store, index, max_bytes=10_000)

    reclaimed = await subject.sweep()

    assert reclaimed.protected == (held.key,)
    assert store.deleted == []


async def test_a_sweep_of_a_healthy_volume_removes_nothing(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """The sweep runs on a schedule, so most runs find everything referenced and fresh."""
    publish(index, store, seed=1, size=100, age=timedelta(hours=1))
    subject = occupancy_for(store, index, max_bytes=10_000)

    reclaimed = await subject.sweep()

    assert reclaimed.freed_bytes == 0
    assert reclaimed.orphans_deleted == 0
    assert store.deleted == []


async def test_release_unregisters_and_deletes_the_bytes(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Completion frees the budget promptly; waiting for the sweep would keep billing."""
    obj = publish(index, store, seed=1, size=512)
    subject = occupancy_for(store, index, max_bytes=10_000)

    released = await subject.release(obj.key)

    assert released.deleted is True
    assert released.freed_bytes == 512
    assert store.deleted == [obj.object_key]
    assert await index.lookup(obj.key) is None


async def test_repeated_release_is_safe(store: FakeObjectStore, index: FakeCacheIndex) -> None:
    """A completion handler retried after a lost reply must not fail on its own success."""
    obj = publish(index, store, seed=1, size=512)
    subject = occupancy_for(store, index, max_bytes=10_000)

    first = await subject.release(obj.key)
    second = await subject.release(obj.key)

    assert (first.deleted, second.deleted) == (True, False)
    assert second.freed_bytes == 0
    assert store.deleted == [obj.object_key]


async def test_releasing_a_key_that_was_never_staged_is_a_no_op(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Release is retry-safe operational state, not an assertion that the key existed."""
    subject = occupancy_for(store, index, max_bytes=10_000)

    released = await subject.release(StagingKey(f"{99:032x}"))

    assert released.deleted is False
    assert store.deleted == []


async def test_release_during_a_download_unpublishes_but_keeps_the_bytes(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """The reader keeps reading; the object becomes an orphan the next sweep collects."""
    obj = publish(index, store, seed=1, size=512)
    index.held.add(obj.key)
    subject = occupancy_for(store, index, max_bytes=10_000)

    released = await subject.release(obj.key)

    assert released.deleted is False
    assert store.deleted == []
    assert await index.lookup(obj.key) is None
    assert index.released == [obj.key]


async def test_a_failed_release_deletion_is_surfaced(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A release that could not delete is unfinished, and the caller has to know that."""
    obj = publish(index, store, seed=1, size=512)
    store.rejects.add(obj.object_key)
    subject = occupancy_for(store, index, max_bytes=10_000)

    with pytest.raises(StagingObjectStoreError, match="rejected"):
        await subject.release(obj.key)

    assert obj.object_key in store.objects
    # The row goes anyway: the bytes are an orphan the next sweep collects, whereas a
    # registration restored over a deleted object is a key nothing can stage again.
    assert await index.lookup(obj.key) is None


async def test_release_runs_under_the_writer_lock(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Otherwise a release could delete bytes a publication was about to register."""
    obj = publish(index, store, seed=1, size=512)
    subject = occupancy_for(store, index, max_bytes=10_000)

    await subject.release(obj.key)

    assert index.operations == [("evict", 1)]


async def test_each_entry_point_labels_the_writer_lock_it_takes(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """The holding's timing is read per caller, so each entry point names itself."""
    obj = publish(index, store, seed=1, size=512)
    subject = occupancy_for(store, index, max_bytes=10_000)

    attempt = uuid4()
    async with reserving(subject, 100, attempt_id=attempt) as reservation:
        await reservation.register(uploaded(store, attempt, size=100))
    async with reserving(subject, 100):
        pass
    await subject.sweep()
    await subject.release(obj.key)

    assert index.lock_callers == [
        "occupancy.reserve",
        "occupancy.register",
        "occupancy.reserve",
        "occupancy.abandon",
        "occupancy.sweep",
        "occupancy.release",
    ]


async def test_measuring_reports_the_same_totals_the_subject_admits_against(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """The reported number and the enforced number are one measurement, not two."""
    publish(index, store, seed=1, size=250)
    orphan(store, seed=2, size=250)
    subject = occupancy_for(store, index, max_bytes=1000)

    report = await subject.measure()

    assert report.total_bytes == 500
    async with reserving(subject, 500) as reservation:
        assert reservation.admission.occupancy == report


async def test_a_sweep_deletes_bytes_only_after_the_unregistration_has_committed(
    index: FakeCacheIndex,
) -> None:
    """Deleting inside the transaction is what lets a later failure restore the row."""
    store = LockDepthStore(index)
    stale = publish(index, store, seed=1, size=100, age=timedelta(hours=25))
    subject = occupancy_for(store, index, max_bytes=10_000)

    reclaimed = await subject.sweep()

    assert reclaimed.evicted == (stale.key,)
    assert store.deleted == [stale.object_key]
    assert store.depths == [0]


async def test_release_deletes_bytes_only_after_the_unregistration_has_committed(
    index: FakeCacheIndex,
) -> None:
    """A cancelled or refused delete after the commit is an orphan, not a poisoned key."""
    store = LockDepthStore(index)
    obj = publish(index, store, seed=1, size=512)
    subject = occupancy_for(store, index, max_bytes=10_000)

    assert (await subject.release(obj.key)).deleted is True
    assert store.depths == [0]


async def test_admission_deletes_evicted_bytes_only_after_its_holding_has_committed(
    index: FakeCacheIndex,
) -> None:
    """The reservation row keeps the headroom, so admission can wait for its own commit."""
    store = LockDepthStore(index)
    stale = publish(index, store, seed=1, size=900, age=timedelta(hours=25))
    subject = occupancy_for(store, index, max_bytes=1000)

    async with reserving(subject, 500) as reservation:
        assert store.deleted == [stale.object_key]
        assert reservation.admission.reclaimed.evicted == (stale.key,)

    assert store.depths == [0]
    assert ("evict", 1) in index.operations


async def test_a_sweep_deletes_every_aged_entry_on_one_connection(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A connection per delete is a connect and a handshake each, under the writer lock."""
    for seed in range(8):
        publish(index, store, seed=seed, size=100, age=timedelta(hours=25))
    subject = occupancy_for(store, index, max_bytes=10_000)

    reclaimed = await subject.sweep()

    assert len(reclaimed.evicted) == 8
    assert len(store.delete_clients) == 8
    assert len(set(store.delete_clients)) == 1


async def test_pressure_eviction_deletes_on_one_connection_too(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Admission holds the writer lock while it deletes, so its pass batches as well."""
    for seed in range(8):
        publish(index, store, seed=seed, size=100, age=timedelta(hours=1))
    subject = occupancy_for(store, index, max_bytes=1000)

    async with reserving(subject, 800) as reservation:
        # Six of the eight entries are the 600 bytes of surplus this admission needs.
        assert len(reservation.admission.reclaimed.evicted) == 6

    assert len(store.delete_clients) == 6
    assert len(set(store.delete_clients)) == 1


async def test_a_pass_with_nothing_to_delete_opens_no_connection_for_it(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Batching must not cost an idle sweep the connection it had no use for."""
    publish(index, store, seed=1, size=100, age=timedelta(hours=1))
    subject = occupancy_for(store, index, max_bytes=10_000)
    opened_before = store.clients_opened

    reclaimed = await subject.sweep()

    assert reclaimed.evicted == ()
    # Two listings — reconciliation's and the eviction candidates' — and nothing else.
    assert store.clients_opened - opened_before == 2


class SlowListingStore(FakeObjectStore):
    """Takes `delay` seconds to start every listing, standing in for a slow paginator."""

    def __init__(self, delay: float) -> None:
        """Start empty, with every listing delayed by `delay` seconds."""
        super().__init__()
        self.delay = delay

    async def list_objects(self) -> AsyncIterator[StagedObjectRef]:
        """Wait `delay` seconds, then list as the fake store does."""
        await asyncio.sleep(self.delay)
        async for ref in super().list_objects():
            yield ref


async def test_an_admission_reports_how_long_its_decision_took(index: FakeCacheIndex) -> None:
    """The decision's time under the lock is the admission share of a reservation's hold."""
    delay = 0.02
    store = SlowListingStore(delay)
    subject = occupancy_for(store, index, max_bytes=1000)

    async with reserving(subject, 100) as reservation:
        assert reservation.admission.admission_seconds >= delay


async def test_an_admission_that_reclaims_counts_every_listing_it_waited_on(
    index: FakeCacheIndex,
) -> None:
    """Measurement, reconciliation and the candidate listing all fall inside the decision."""
    delay = 0.02
    store = SlowListingStore(delay)
    publish(index, store, seed=1, size=600)
    subject = occupancy_for(store, index, max_bytes=1000)

    async with reserving(subject, 600) as reservation:
        assert reservation.admission.reclaimed.evicted == (StagingKey(f"{1:032x}"),)
        assert reservation.admission.admission_seconds >= 3 * delay


async def test_an_uncontended_admission_reports_its_time(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A fast decision still reports its time rather than leaving the default in place."""
    subject = occupancy_for(store, index, max_bytes=1000)

    async with reserving(subject, 0) as reservation:
        assert reservation.admission.admission_seconds > 0


async def test_a_refusal_raises_as_before_the_timing_was_added(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A refusal yields no admission, so it has no admission time to report."""
    held = publish(index, store, seed=1, size=800)
    index.held.add(held.key)
    subject = occupancy_for(store, index, max_bytes=1000)

    with pytest.raises(StagingCapacityExhaustedError, match="being downloaded"):
        async with reserving(subject, 500):
            pytest.fail("a refused reservation must not run its block")


class Clock:
    """A settable instant, shared by a fake index judging reservation expiry."""

    def __init__(self) -> None:
        """Start at the test's fixed instant."""
        self.now = _NOW

    def __call__(self) -> datetime:
        """Return the current instant."""
        return self.now


def reservation_events[E: Mapping[str, object]](events: list[E], name: str) -> list[E]:
    """Return the events named `name` among `events`."""
    return [e for e in events if e["event"] == name]


async def test_measuring_reports_live_reservations_beside_the_listing(
    store: FakeObjectStore,
) -> None:
    """An in-flight upload's claim is occupancy before its bytes land; an expired one is not."""
    clock = Clock()
    index = FakeCacheIndex(clock=clock)
    publish(index, store, seed=1, size=100)
    await index.reserve(_KEY, uuid4(), 300, timedelta(minutes=1))
    await index.reserve(_KEY, uuid4(), 50, timedelta(minutes=5))
    clock.now += timedelta(minutes=2)

    report = await occupancy_for(store, index, max_bytes=1000).measure()

    assert (report.total_bytes, report.reserved_bytes, report.claimed_bytes) == (100, 50, 150)


async def test_a_plain_index_reports_no_reservations(store: FakeObjectStore) -> None:
    """A reader-side index keeps no reservations, so it reports none rather than failing."""

    class PlainIndex:
        """Registrations only, as `StagingCacheIndex` declares them."""

        async def total_registered_bytes(self) -> int:
            """Report nothing registered."""
            return 0

    report = await measure_occupancy(store, PlainIndex())  # type: ignore[arg-type]

    assert report.reserved_bytes == 0


async def test_an_open_reservation_counts_against_another_admission(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Two workers cannot spend one headroom, though neither holds a lock while uploading."""
    first = occupancy_for(store, index, max_bytes=1000)
    second = occupancy_for(store, index, max_bytes=1000)

    async with reserving(first, 600):
        with pytest.raises(StagingCapacityExhaustedError, match="600 reserved"):
            async with reserving(second, 600):
                pytest.fail("the second admission spent headroom the first had reserved")
        async with reserving(second, 400) as beside:
            assert beside.admission.occupancy.reserved_bytes == 600
            assert len(index.reservations) == 2


async def test_an_expired_reservation_no_longer_counts_against_admission(
    store: FakeObjectStore,
) -> None:
    """A dead worker's headroom returns once the database clock passes its expiry."""
    clock = Clock()
    index = FakeCacheIndex(clock=clock)
    await index.reserve(_KEY, uuid4(), 600, _TTL)
    subject = occupancy_for(store, index, max_bytes=1000)

    with pytest.raises(StagingCapacityExhaustedError):
        async with reserving(subject, 600):
            pass
    clock.now += _TTL

    async with reserving(subject, 600) as reservation:
        assert reservation.admission.occupancy.reserved_bytes == 0


async def test_a_refusal_reserves_nothing(store: FakeObjectStore, index: FakeCacheIndex) -> None:
    """Refused headroom is not held for an upload that will never run."""
    held = publish(index, store, seed=1, size=800)
    index.held.add(held.key)
    subject = occupancy_for(store, index, max_bytes=1000)

    with pytest.raises(StagingCapacityExhaustedError):
        async with reserving(subject, 500):
            pass

    assert index.reservations == {}
    assert ("reserve", 1) not in index.operations


async def test_a_refusal_after_eviction_still_frees_what_it_unregistered(
    index: FakeCacheIndex,
) -> None:
    """The unregistrations commit and their bytes go after it, refusal or not."""
    store = LockDepthStore(index)
    stale = publish(index, store, seed=1, size=300, age=timedelta(hours=25))
    held = publish(index, store, seed=2, size=600)
    index.held.add(held.key)
    subject = occupancy_for(store, index, max_bytes=1000)

    with pytest.raises(StagingCapacityExhaustedError, match="being downloaded"):
        async with reserving(subject, 500):
            pass

    assert store.deleted == [stale.object_key]
    assert store.depths == [0]
    assert stale.key not in index.registrations
    assert index.reservations == {}


async def test_a_refusal_whose_eviction_fails_reports_the_failure(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A failed delete is the louder problem, and the refusal travels with it as a note."""
    stale = publish(index, store, seed=1, size=300, age=timedelta(hours=25))
    store.rejects.add(stale.object_key)
    held = publish(index, store, seed=2, size=600)
    index.held.add(held.key)
    subject = occupancy_for(store, index, max_bytes=1000)

    with pytest.raises(StagingObjectStoreError, match="rejected") as raised:
        async with reserving(subject, 500):
            pass

    assert any("refused as well" in note for note in raised.value.__notes__)
    assert index.reservations == {}


async def test_a_lost_reservation_registers_nothing_and_removes_its_bytes(
    store: FakeObjectStore,
) -> None:
    """An upload that outlived its reservation may name bytes reconciliation took."""
    clock = Clock()
    index = FakeCacheIndex(clock=clock)
    subject = occupancy_for(store, index, max_bytes=1000)
    attempt = uuid4()

    with capture_logs() as events, pytest.raises(StagingReservationLostError, match="deleted"):
        async with reserving(subject, 100, attempt_id=attempt) as reservation:
            staged = uploaded(store, attempt, size=100)
            clock.now += _TTL
            await reservation.register(staged)

    assert not reservation.registered
    assert reservation.registered_at is None
    assert index.registrations == {}
    # Consuming leaves an expired row; the block's abandonment is what removes it.
    assert index.reservations == {}
    assert index.operations[-1] == ("forget_reservation", 1)
    assert index.lock_callers[-2:] == ["occupancy.register", "occupancy.abandon"]
    assert store.deleted == [staged.object_key]
    assert ("register", 1) not in index.operations
    [event] = reservation_events(events, "staging.reservation_lost")
    assert event["log_level"] == "warning"
    assert (event["staging_key"], event["attempt_id"], event["reserved_bytes"]) == (
        str(_KEY),
        str(attempt),
        100,
    )


async def test_a_lost_reservation_whose_bytes_cannot_be_deleted_is_still_lost(
    store: FakeObjectStore,
) -> None:
    """The caller needs the retryable failure; the orphan is reconciliation's."""
    clock = Clock()
    index = FakeCacheIndex(clock=clock)
    subject = occupancy_for(store, index, max_bytes=1000)
    attempt = uuid4()
    staged = uploaded(store, attempt, size=100)
    store.rejects.add(staged.object_key)

    with pytest.raises(StagingReservationLostError, match="orphan") as raised:
        async with reserving(subject, 100, attempt_id=attempt) as reservation:
            clock.now += _TTL
            await reservation.register(staged)

    assert isinstance(raised.value.__cause__, StagingObjectStoreError)
    assert staged.object_key in store.objects
    assert index.registrations == {}


async def test_a_reservation_refuses_another_attempts_object(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Consuming one attempt's reservation for another's bytes would publish unreserved bytes."""
    subject = occupancy_for(store, index, max_bytes=1000)

    with pytest.raises(ValueError, match="cannot register"):
        async with reserving(subject, 100) as reservation:
            await reservation.register(uploaded(store, uuid4(), size=100))

    assert index.registrations == {}
    assert index.reservations == {}


async def test_a_reservation_registers_once(store: FakeObjectStore, index: FakeCacheIndex) -> None:
    """A second registration would find no reservation and delete the published bytes."""
    subject = occupancy_for(store, index, max_bytes=1000)
    attempt = uuid4()

    async with reserving(subject, 100, attempt_id=attempt) as reservation:
        staged = uploaded(store, attempt, size=100)
        await reservation.register(staged)
        with pytest.raises(RuntimeError, match="already used"):
            await reservation.register(staged)

    assert index.registrations[_KEY] == staged
    assert staged.object_key in store.objects


async def test_a_block_that_raises_abandons_its_reservation(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A failed upload's headroom returns at once rather than at expiry."""
    subject = occupancy_for(store, index, max_bytes=1000)

    with pytest.raises(StagingObjectStoreError, match="upload failed"):
        async with reserving(subject, 100):
            raise StagingObjectStoreError("upload failed")

    assert index.reservations == {}
    assert ("forget_reservation", 1) in index.operations
    assert index.lock_callers == ["occupancy.reserve", "occupancy.abandon"]


async def test_a_cancelled_block_abandons_its_reservation(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """A drained worker's reservation goes with its upload, and the cancellation propagates."""
    subject = occupancy_for(store, index, max_bytes=1000)
    entered = asyncio.Event()

    async def upload_forever() -> None:
        """Hold the reservation open until cancelled."""
        async with reserving(subject, 100):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(upload_forever())
    await entered.wait()
    assert len(index.reservations) == 1
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert index.reservations == {}


async def test_a_failed_abandonment_is_logged_and_the_block_error_propagates(
    store: FakeObjectStore, index: FakeCacheIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cleanup failure must not hide why the upload failed; expiry removes the row."""

    async def unreachable(attempt_id: UUID) -> None:
        """Fail as the database would when it cannot be reached."""
        raise StagingObjectStoreError("database unreachable")

    monkeypatch.setattr(index, "forget_reservation", unreachable)
    subject = occupancy_for(store, index, max_bytes=1000)
    attempt = uuid4()

    with capture_logs() as events, pytest.raises(ValueError, match="the upload's own"):
        async with reserving(subject, 100, attempt_id=attempt):
            raise ValueError("the upload's own failure")

    [event] = reservation_events(events, "staging.reservation_abandon_failed")
    assert event["log_level"] == "warning"
    assert (event["staging_key"], event["attempt_id"]) == (str(_KEY), str(attempt))
    assert event["reserved_bytes"] == 100
    assert event["error_type"] == "StagingObjectStoreError"
    assert attempt in index.reservations


async def test_a_cancelled_abandonment_is_bounded_and_the_cancellation_propagates(
    store: FakeObjectStore, index: FakeCacheIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung removal cannot hold a drain past its bound; the row is left to expire."""

    answered = asyncio.Event()

    async def hung(attempt_id: UUID) -> None:
        """Answer only once the test lets it, as a database stalled mid-drain would."""
        await answered.wait()

    monkeypatch.setattr(index, "forget_reservation", hung)
    monkeypatch.setattr(occupancy_module, "_ABANDON_SECONDS", 0.01)
    subject = occupancy_for(store, index, max_bytes=1000)
    entered = asyncio.Event()

    async def upload_forever() -> None:
        """Hold the reservation open until cancelled."""
        async with reserving(subject, 100):
            entered.set()
            await asyncio.Event().wait()

    with capture_logs() as events:
        task = asyncio.create_task(upload_forever())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    [event] = reservation_events(events, "staging.reservation_abandon_failed")
    assert event["error_type"] == "TimeoutError"
    # The shielded removal outlives the bound in the background; let it finish.
    answered.set()
    await asyncio.sleep(0)


async def test_a_block_that_does_not_register_abandons_its_reservation(
    store: FakeObjectStore, index: FakeCacheIndex
) -> None:
    """Leaving without publishing is an abandonment too, not a reservation left to expire."""
    subject = occupancy_for(store, index, max_bytes=1000)

    async with reserving(subject, 100):
        pass

    assert index.reservations == {}


async def test_a_second_cancellation_during_abandonment_still_ends_in_the_first(
    store: FakeObjectStore, index: FakeCacheIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A drain that cancels again only gives up on the cleanup, which is logged."""
    answered = asyncio.Event()
    forgetting = asyncio.Event()

    async def stalled(attempt_id: UUID) -> None:
        """Answer only once the test lets it."""
        forgetting.set()
        await answered.wait()

    monkeypatch.setattr(index, "forget_reservation", stalled)
    subject = occupancy_for(store, index, max_bytes=1000)
    entered = asyncio.Event()

    async def upload_forever() -> None:
        """Hold the reservation open until cancelled."""
        async with reserving(subject, 100):
            entered.set()
            await asyncio.Event().wait()

    with capture_logs() as events:
        task = asyncio.create_task(upload_forever())
        await entered.wait()
        task.cancel()
        await forgetting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    [event] = reservation_events(events, "staging.reservation_abandon_failed")
    assert event["error_type"] == "CancelledError"
    answered.set()
    await asyncio.sleep(0)


async def test_a_cancellation_during_an_ordinary_abandonment_propagates(
    store: FakeObjectStore, index: FakeCacheIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a block already cancelled shields its cleanup; a new cancellation is obeyed."""
    forgetting = asyncio.Event()

    async def stalled(attempt_id: UUID) -> None:
        """Wait until cancelled."""
        forgetting.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(index, "forget_reservation", stalled)
    subject = occupancy_for(store, index, max_bytes=1000)

    async def failing_upload() -> None:
        """Fail inside the reservation."""
        async with reserving(subject, 100):
            raise StagingObjectStoreError("upload failed")

    task = asyncio.create_task(failing_upload())
    await forgetting.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
