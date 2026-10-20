"""Occupancy accounting, eviction and completion release for S3-backed staging.

The bound is measured from the object store's own listing, not from the cache index's
registered rows. Registered bytes are what is currently published; occupied bytes are
every completed object under the content prefix — another worker's as much as this
one's, and orphans no registration refers to any more. Accounting from rows alone
understates the second number and admits work that does not fit, so the listing is
paginated to exhaustion and a listing that stopped early is treated as no answer rather
than as a smaller one.

Reservation happens before a byte of the source is read: reserving afterwards is not a
bound, it is a report. A reservation is a row in `staging_reservations`, written by the
admission decision and counted by every worker's admission beside the listing, so two
workers cannot spend the same headroom although no lock spans the upload. A publication
takes the global writer lock twice, briefly each time: once to admit (measure, reclaim,
record the reservation; commit) and once to register (consume the reservation, write the
row; commit). A reservation that ends without registering is abandoned in a third
holding, and a dead worker's expires by the database clock after twice the transfer
timeout. The entry locks reclamation takes end with the admitting holding, so a reader
waits at most for one reclamation, never for a transfer.

Admission evicts the way `sweep` and `release` do: it unregisters under the lock and
deletes the bytes once that has committed. A failure after the commit leaves an orphan the
next reconciliation collects, never a registration naming bytes that are gone.

Until 2026-10-18 the reservation was the lock itself: admission held it across the
decision, the upload it admitted and the registration that published it, so the cluster
staged one object at a time, a long transfer delayed the idle sweep, and the entry locks
reclamation took were held for the whole transfer, stalling a reader of a key that merely
collided with a reclaimed one. Reviewed and kept on 2026-09-15:
`staging_transfer_timeout_seconds` bounded the worst case, and the alternative — narrowing
the lock to admission plus registration, with a reservation row carrying the interval
between them — was a different design whose crash semantics needed their own evidence, to
be revisited when measured upload concurrency showed the lock binding (#1, #4). The
`staging.writer_lock_held`, `staging.reader_lock_wait` and `staging.reservation_phases`
log events were that measurement. Superseded on 2026-10-18 by the reservation table
(`resolve-staging-lock-contention`): the workload could not produce a representative
reading and the defect was understood without one, so deterministic tests are the
evidence instead and the events stay as regression alarms (see `docs/staging.md`).

What this bounds is active staged content. It is not a provider billing cap: incomplete
multipart uploads bill separately and are reclaimed by explicit abort and the backend's
residual cleanup, and are reported rather than folded into this budget.
"""

import asyncio
import time
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final, Protocol, runtime_checkable
from uuid import UUID

from curate_d.clock import utc_now
from curate_d.observability import get_logger
from curate_d.staging.cache_index import staging_key_of
from curate_d.staging.errors import (
    StagingCapacityExhaustedError,
    StagingObjectStoreError,
    StagingReservationLostError,
)
from curate_d.staging.keys import StagingKey
from curate_d.staging.objects import (
    StagedObject,
    StagedObjectRef,
    StagingCacheIndex,
    StagingObjectStore,
)

type Clock = Callable[[], datetime]

logger = get_logger(__name__)

# How long a cancelled reservation may spend removing its row. Short, because it runs in
# a drain; a row it could not remove in time expires on its own.
_ABANDON_SECONDS: Final = 5.0


@dataclass(frozen=True)
class OccupancyReport:
    """What the object store actually holds under the content prefix, and what is promised.

    `total_bytes` is every completed object, whoever wrote it and whether or not a
    registration still refers to it. `registered_bytes` is the published subset, carried
    alongside so the unreferenced remainder is a reported quantity rather than a shortfall
    discovered at admission time. `reserved_bytes` is what live reservations claim for
    uploads still in flight, on any worker; admission bounds the sum of it and
    `total_bytes`.
    """

    total_bytes: int
    object_count: int
    registered_bytes: int
    reserved_bytes: int = 0

    @property
    def unreferenced_bytes(self) -> int:
        """Return the completed bytes no registration refers to, awaiting reconciliation.

        Never negative. A registration whose object has already been expired remotely is
        a stale row for a reader to invalidate, not negative occupancy.
        """
        return max(self.total_bytes - self.registered_bytes, 0)

    @property
    def claimed_bytes(self) -> int:
        """Return the bytes admission counts against the bound: completed plus reserved.

        A completed object whose registration has not yet consumed its reservation is in
        both, which overstates the claim until that registration commits. Conservative:
        the bound is under-used briefly, never exceeded.
        """
        return self.total_bytes + self.reserved_bytes


@dataclass(frozen=True)
class ReclaimReport:
    """What one reclamation pass removed, and what it deliberately did not."""

    evicted: tuple[StagingKey, ...]
    protected: tuple[StagingKey, ...]
    orphans_deleted: int
    freed_bytes: int


@dataclass(frozen=True)
class _Unpublished:
    """What one pass unregistered, and the keys it left published.

    Separating unregistration from deletion is what lets a caller commit the first and
    only then do the second: an unreferenced object is collectable, while a registration
    restored over a deleted object is not, so the row must never be the thing that can be
    rolled back after its bytes are gone.
    """

    registrations: tuple[StagedObject, ...]
    protected: tuple[StagingKey, ...]

    @property
    def size_bytes(self) -> int:
        """Return the bytes the unregistered entries name, freed once they are deleted."""
        return sum(registration.size_bytes for registration in self.registrations)

    def __add__(self, other: "_Unpublished") -> "_Unpublished":
        """Return both passes' unregistrations, each protected key listed once."""
        return _Unpublished(
            registrations=self.registrations + other.registrations,
            protected=tuple(dict.fromkeys(self.protected + other.protected)),
        )


@dataclass(frozen=True)
class _Reclamation:
    """One deletion pass's report, and the deletion failure it deferred.

    A pass reports a failed deletion rather than raising it, so the caller finishes its
    own bookkeeping first — counting the orphans it collected, abandoning the reservation
    the deletion was making room for — and raises it afterwards. The unregistrations it
    deleted for have committed by then; an unregistered object is the repairable side of
    the failure.
    """

    report: ReclaimReport
    failure: StagingObjectStoreError | None = None


@dataclass(frozen=True)
class AdmissionReport:
    """The accepted reservation and the state of staging it was accepted against.

    `admission_seconds` is the time from acquiring the writer lock to the admission
    decision: measuring, reclaiming, unregistering evicted entries and recording the
    reservation. The wait for the lock is not in it; the holding's
    `staging.writer_lock_held` event already reports that.
    """

    occupancy: OccupancyReport
    reserved_bytes: int
    reclaimed: ReclaimReport
    admission_seconds: float = 0.0


@dataclass(frozen=True)
class _Decision:
    """One admission decided under the writer lock, and the deletions it left for after.

    `refusal` is set when reclamation still could not make room. It is carried out of the
    holding rather than raised in it, so the unregistrations reclamation made commit and
    their bytes can be freed: a refused admission still leaves the room it found for the
    next one.
    """

    admission: AdmissionReport
    unpublished: _Unpublished
    refusal: StagingCapacityExhaustedError | None = None


@dataclass(frozen=True)
class ReleaseReport:
    """What releasing one completed entry did.

    `deleted` is False both when nothing was registered — a repeated release — and when
    a download still holds the entry. Neither is a failure: the registration is gone
    either way, and in the second case the bytes are an orphan the reader keeps reading
    and the next sweep collects.
    """

    key: StagingKey
    freed_bytes: int
    deleted: bool


_NOTHING_RECLAIMED: Final = ReclaimReport(
    evicted=(), protected=(), orphans_deleted=0, freed_bytes=0
)
_NOTHING_UNPUBLISHED: Final = _Unpublished(registrations=(), protected=())


def _deletion_deferred(
    object_keys: Sequence[str], cause: StagingObjectStoreError
) -> StagingObjectStoreError:
    """Return the failure to raise once the unregistrations it interrupted have committed."""
    failure = StagingObjectStoreError(
        f"staging unregistered {len(object_keys)} entries whose bytes it could not "
        f"delete ({', '.join(object_keys)}); the next reconciliation collects them: {cause}"
    )
    failure.__cause__ = cause
    return failure


@runtime_checkable
class CoordinatedStagingIndex(StagingCacheIndex, Protocol):
    """The cache-index surface occupancy needs beyond publication and lookup.

    Kept separate from `StagingCacheIndex` because a process that only reads staged
    content needs none of it: these are the operations that serialize or shrink the
    whole volume. Declaring them as a protocol is what lets admission and eviction be
    tested against a fake instead of a database.
    """

    def writer_lock(self, caller: str) -> AbstractAsyncContextManager[object]:
        """Hold the global writer lock, re-entrantly for the context already holding it.

        `caller` labels the timing a fresh acquisition reports; a re-entrant one reports
        nothing.
        """
        ...

    async def evict(self, key: StagingKey) -> StagedObject | None:
        """Unregister `key` unless a download holds it, returning what it published."""
        ...

    async def reconcile(self, store: StagingObjectStore) -> int:
        """Delete every completed object neither registered nor reserved, returning the count.

        Expires dead reservations first, so the object of an attempt whose reservation
        ended without a registration is collected like any other orphan.
        """
        ...

    async def reserve(
        self, key: StagingKey, attempt_id: UUID, reserved_bytes: int, ttl: timedelta
    ) -> None:
        """Record `attempt_id`'s claim on `reserved_bytes` of headroom until `ttl` passes.

        Expiry is judged by the database clock alone, written as its `now()` plus `ttl` and
        compared against its `now()` on every read, so workers with skewed clocks agree.
        Runs inside the writer lock its caller holds.
        """
        ...

    async def consume_reservation(self, attempt_id: UUID) -> bool:
        """Remove `attempt_id`'s live reservation, returning whether one was there.

        Returns False for an expired or absent reservation: the registration it guards
        must then publish nothing. Runs inside the writer lock its caller holds, so the
        removal commits with the registration.
        """
        ...

    async def forget_reservation(self, attempt_id: UUID) -> None:
        """Remove `attempt_id`'s reservation whether or not it has expired.

        Takes the writer lock as `occupancy.abandon` when the caller holds none.
        """
        ...

    async def total_reserved_bytes(self) -> int:
        """Return the bytes live reservations claim, by the database clock."""
        ...


async def measure_occupancy(store: StagingObjectStore, index: StagingCacheIndex) -> OccupancyReport:
    """Total every completed object under the content prefix, paginating to exhaustion.

    Live reservations are reported beside the listing when `index` keeps them, and as 0
    when it is a plain `StagingCacheIndex`.

    Raises:
        StagingObjectStoreError: The listing or an index total did not complete. A
            truncated listing is not a smaller occupancy, it is an unknown one, and
            admitting against it would spend capacity nobody measured.
    """
    total_bytes = 0
    object_count = 0
    async for ref in store.list_objects():
        total_bytes += ref.size_bytes
        object_count += 1
    return OccupancyReport(
        total_bytes=total_bytes,
        object_count=object_count,
        registered_bytes=await index.total_registered_bytes(),
        reserved_bytes=(
            await index.total_reserved_bytes() if isinstance(index, CoordinatedStagingIndex) else 0
        ),
    )


async def list_staged_objects(store: StagingObjectStore) -> list[StagedObjectRef]:
    """Return every completed object under the content prefix, oldest first.

    Materialized rather than streamed because eviction order is a property of the whole
    listing: an oldest-first pass cannot begin until the last page has been read.
    """
    refs = [ref async for ref in store.list_objects()]
    refs.sort(key=lambda ref: ref.last_modified)
    return refs


class Reservation:
    """One admitted upload's claim on headroom, from its admission to its registration.

    Yielded by `StagingOccupancy.reserve()`. The claim is a row every worker's admission
    counts, not a held lock, so the upload runs with no lock held. `register()` is the only
    way to publish under it: the registration and the row's removal commit together, and
    a reservation that has expired or been removed publishes nothing.
    """

    def __init__(
        self,
        index: CoordinatedStagingIndex,
        store: StagingObjectStore,
        *,
        key: StagingKey,
        attempt_id: UUID,
        admission: AdmissionReport,
    ) -> None:
        """Bind the reservation recorded for `attempt_id` under `key`."""
        self._index = index
        self._store = store
        self._key = key
        self._attempt_id = attempt_id
        self._admission = admission
        self._used = False
        self._registered = False
        self._registered_at: float | None = None

    @property
    def admission(self) -> AdmissionReport:
        """Return the admission decision this reservation was recorded by."""
        return self._admission

    @property
    def registered(self) -> bool:
        """Return whether the registration holding consumed the reservation and committed."""
        return self._registered

    @property
    def registered_at(self) -> float | None:
        """Return the monotonic instant the registration row was written, None before.

        Set inside the registration holding, before its commit, so a caller timing the
        publication can split registration from commit.
        """
        return self._registered_at

    async def register(self, staged: StagedObject) -> None:
        """Publish `staged` by consuming this reservation in the same holding.

        Holds the writer lock as `occupancy.register`. Registration keeps its first-writer
        rule: a key another attempt already won keeps its winner, so the caller confirms
        the outcome by a lookup once this returns.

        Args:
            staged: The verified upload of this reservation's attempt.

        Raises:
            ValueError: `staged` is not this reservation's key and attempt.
            RuntimeError: The reservation was already used to register.
            StagingReservationLostError: The reservation had expired or been removed.
                Nothing was registered and the attempt's bytes were deleted, or left as an
                orphan for reconciliation when the delete failed.
            StagingObjectStoreError: The index could not be changed; nothing committed.
        """
        if staged.key != self._key or staged.attempt_id != self._attempt_id:
            raise ValueError(
                f"reservation for attempt {self._attempt_id} under {self._key} cannot "
                f"register attempt {staged.attempt_id} under {staged.key}"
            )
        if self._used:
            raise RuntimeError(f"reservation for attempt {self._attempt_id} was already used")
        self._used = True
        async with self._index.writer_lock("occupancy.register"):
            live = await self._index.consume_reservation(self._attempt_id)
            if live:
                await self._index.register(staged)
                self._registered_at = time.monotonic()
        if live:
            self._registered = True
            return
        logger.warning(
            "staging.reservation_lost",
            staging_key=str(self._key),
            attempt_id=str(self._attempt_id),
            reserved_bytes=self._admission.reserved_bytes,
        )
        failure = await self._discard(staged)
        raise failure

    async def _discard(self, staged: StagedObject) -> StagingReservationLostError:
        """Delete a lost reservation's bytes best effort, returning the error to raise.

        After the registration holding, so no lock is held across an object-store call.
        A refused delete is folded into the error rather than raised: the object is an
        orphan either way, and the caller needs to know the reservation was lost.
        """
        lost = (
            f"staging reservation for attempt {self._attempt_id} under {self._key} expired "
            f"or was removed before its upload registered; nothing was published"
        )
        try:
            await self._store.delete(staged.object_key)
        except StagingObjectStoreError as error:
            failure = StagingReservationLostError(
                f"{lost}, and its bytes remain as an orphan for reconciliation: {error}"
            )
            failure.__cause__ = error
            return failure
        return StagingReservationLostError(f"{lost} and its bytes were deleted")


class StagingOccupancy:
    """Keeps completed staging occupancy inside its configured byte bound.

    Owns no state of its own: the bound is enforced against what the object store and
    the cache index say at the moment of admission, so two pods running this against one
    bucket agree without sharing anything but PostgreSQL and the listing.
    """

    def __init__(
        self,
        store: StagingObjectStore,
        index: CoordinatedStagingIndex,
        *,
        max_bytes: int,
        max_age: timedelta,
        reservation_ttl: timedelta,
        clock: Clock = utc_now,
    ) -> None:
        """Bind the backends and the bounds admission is decided against.

        Args:
            store: Object store whose listing is the authoritative occupancy.
            index: Cache index providing the writer lock, the protected-eviction path and
                the reservation rows.
            max_bytes: Ceiling on active completed and reserved staged bytes.
            max_age: Age at which a staged entry becomes evictable regardless of pressure.
            reservation_ttl: How long a reservation stays live, by the database clock,
                without the registration that consumes it. It must outlast the upload;
                the factory derives it as twice the transfer timeout, never zero. Zero
                expires a reservation in the next database transaction, and exists so
                tests can lose one deterministically, without waiting.
            clock: Source of the current instant, injected so age eviction is pinned by
                the caller rather than by how long a test took to run.

        Raises:
            ValueError: A non-positive bound, which would refuse every admission, or a
                negative reservation TTL.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_age <= timedelta():
            raise ValueError("max_age must be positive")
        if reservation_ttl < timedelta():
            raise ValueError("reservation_ttl must be nonnegative")
        self._store = store
        self._index = index
        self._max_bytes = max_bytes
        self._max_age = max_age
        self._reservation_ttl = reservation_ttl
        self._clock = clock

    @property
    def reservation_ttl(self) -> timedelta:
        """Return how long a reservation stays live without its registration."""
        return self._reservation_ttl

    async def measure(self) -> OccupancyReport:
        """Return current occupancy, including objects this worker never wrote.

        Raises:
            StagingObjectStoreError: The listing or an index total did not complete.
        """
        return await measure_occupancy(self._store, self._index)

    @asynccontextmanager
    async def reserve(
        self, key: StagingKey, attempt_id: UUID, declared_max_bytes: int
    ) -> AsyncGenerator[Reservation, None]:
        """Reserve `declared_max_bytes` for `attempt_id`'s upload under `key`.

        Admission runs in one holding of the writer lock as `occupancy.reserve`: measure
        the listing and the live reservations, reclaim under pressure, record the
        reservation, commit. Evicted entries' bytes are deleted after that commit and
        before the block runs. The block itself holds no lock: upload in it, then publish
        through the yielded reservation's `register()`. Enter this before reading the
        source; a reservation taken after the bytes have landed bounds nothing.

        A block that exits without registering — it raised, was cancelled or simply did
        not register — abandons the reservation in a holding of its own
        (`occupancy.abandon`), best effort: a failure is logged as
        `staging.reservation_abandon_failed` and left to expiry, and never replaces the
        block's own exception. After a cancellation the abandonment is shielded and
        bounded, so a drain cannot cancel the cleanup itself.

        Args:
            key: The staging key the upload is for.
            attempt_id: The upload attempt the reservation belongs to, and the only one
                its `register()` accepts.
            declared_max_bytes: Upper bound on what the incoming stream may deliver. The
                object store enforces it while uploading, so an oversized stream fails
                rather than consuming capacity nobody reserved.

        Yields:
            The live reservation, carrying the admission it was accepted by.

        Raises:
            ValueError: A negative declaration.
            StagingCapacityExhaustedError: The declaration exceeds the whole bound, or
                eviction could not free enough because the remaining candidates are
                being downloaded. Nothing is reserved; what eviction unregistered stays
                unregistered and its bytes are deleted before this is raised.
            StagingObjectStoreError: Occupancy could not be measured or the index could
                not be changed, and nothing committed; or a deletion during eviction
                failed after the admission committed, in which case the reservation has
                been abandoned and the undeleted bytes are orphans for reconciliation.
        """
        async with self._index.writer_lock("occupancy.reserve"):
            started = time.monotonic()
            decision = await self._admit(declared_max_bytes)
            if decision.refusal is None:
                await self._index.reserve(
                    key, attempt_id, declared_max_bytes, self._reservation_ttl
                )
            admission_seconds = time.monotonic() - started
        # Outside the lock, so the unregistrations have committed before a byte is
        # deleted, and a failure from here on cannot restore a row over absent bytes.
        if decision.refusal is not None:
            reclamation = await self._delete_unpublished(decision.unpublished)
            if reclamation.failure is not None:
                reclamation.failure.add_note(f"admission was refused as well: {decision.refusal}")
                raise reclamation.failure
            raise decision.refusal
        reservation = Reservation(
            self._index,
            self._store,
            key=key,
            attempt_id=attempt_id,
            admission=replace(decision.admission, admission_seconds=admission_seconds),
        )
        cancelled = False
        try:
            reclamation = await self._delete_unpublished(decision.unpublished)
            if reclamation.failure is not None:
                raise reclamation.failure
            yield reservation
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if not reservation.registered:
                await self._abandon(key, attempt_id, declared_max_bytes, cancelled=cancelled)

    async def sweep(self) -> ReclaimReport:
        """Reclaim orphans and aged entries, whether or not anything is being staged.

        The periodic cleanup path, and the normal one: pressure eviction is what happens
        when it has not run often enough. Reconciliation spares every object a live
        reservation names, and runs under the writer lock so it cannot interleave with
        the registration holding that consumes a reservation and publishes its object.

        The aged entries' bytes are deleted only once those unregistrations have
        committed. Nothing after the commit can restore a row, so a failure, a cancelled
        drain or a pod dying part way leaves at worst an unreferenced object the next
        reconciliation collects — never a registration naming bytes that are gone, which
        nothing repairs. Deleting first would also risk the reverse, an entry restaged
        between the two steps, since the delete names the attempt that was unregistered.

        Raises:
            StagingObjectStoreError: Listing or deleting failed. What was already deleted
                stays deleted, what could not be deleted is unregistered and collectable,
                and the sweep is safe to run again.
        """
        async with self._index.writer_lock("occupancy.sweep"):
            orphans_deleted = await self._index.reconcile(self._store)
            refs = await list_staged_objects(self._store)
            unpublished = await self._unpublish(self._expired(refs), limit=None)
        reclamation = await self._delete_unpublished(unpublished)
        if reclamation.failure is not None:
            raise reclamation.failure
        return replace(reclamation.report, orphans_deleted=orphans_deleted)

    async def release(self, key: StagingKey) -> ReleaseReport:
        """Drop `key`'s registration and delete its bytes when no download holds them.

        Prompt rather than deferred to the next sweep, because the bytes bill until they
        are gone. Safe to repeat: a key with nothing registered releases to a no-op, so a
        completion handler retried after a lost reply does not have to know whether its
        first attempt landed.

        Returns:
            What was dropped, and whether the bytes went with the registration.

        Raises:
            StagingObjectStoreError: The index could not be changed, or the deletion
                failed. A failed deletion leaves an unreferenced object the next sweep
                collects; the unregistration stands, because an orphan is repairable and
                a registration naming deleted bytes is not.
        """
        async with self._index.writer_lock("occupancy.release"):
            registration = await self._index.evict(key)
            if registration is None:
                # Either nothing is registered or a download holds the entry. The second
                # case still unpublishes: the reader keeps its bytes and the sweep
                # collects them once the hold is gone.
                await self._index.release(key)
                return ReleaseReport(key=key, freed_bytes=0, deleted=False)
        # Outside the lock, so the unregistration is committed before a byte is deleted.
        # Nothing that happens from here — a refusal, a timeout, a cancellation — can put
        # the row back over bytes that have already gone.
        await self._store.delete(registration.object_key)
        return ReleaseReport(key=key, freed_bytes=registration.size_bytes, deleted=True)

    async def _admit(self, declared_max_bytes: int) -> _Decision:
        """Decide one admission under an already-held writer lock.

        Raises:
            ValueError: A negative declaration.
            StagingCapacityExhaustedError: The declaration exceeds the whole bound.
            StagingObjectStoreError: Measuring, reconciling or unregistering failed. Raised
                inside the holding, which rolls back: no bytes have been deleted for the
                unregistrations it undoes.
        """
        if declared_max_bytes < 0:
            raise ValueError("declared_max_bytes must be nonnegative")
        if declared_max_bytes > self._max_bytes:
            raise StagingCapacityExhaustedError(
                f"staging cannot hold {declared_max_bytes} bytes: the active bound is "
                f"{self._max_bytes} bytes, so this content will never fit"
            )

        occupancy = await self.measure()
        if occupancy.claimed_bytes + declared_max_bytes <= self._max_bytes:
            return _Decision(
                admission=AdmissionReport(
                    occupancy=occupancy,
                    reserved_bytes=declared_max_bytes,
                    reclaimed=_NOTHING_RECLAIMED,
                ),
                unpublished=_NOTHING_UNPUBLISHED,
            )

        reclaimed, unpublished, remaining = await self._reclaim(
            declared_max_bytes, occupancy.reserved_bytes
        )
        refusal = None
        if remaining.claimed_bytes + declared_max_bytes > self._max_bytes:
            refusal = StagingCapacityExhaustedError(
                f"staging needs {declared_max_bytes} bytes but holds "
                f"{remaining.total_bytes} and has {remaining.reserved_bytes} reserved of "
                f"{self._max_bytes} after freeing {reclaimed.freed_bytes}; "
                f"{len(reclaimed.protected)} entries are being downloaded and were not "
                f"evicted"
            )
        return _Decision(
            admission=AdmissionReport(
                occupancy=remaining, reserved_bytes=declared_max_bytes, reclaimed=reclaimed
            ),
            unpublished=unpublished,
            refusal=refusal,
        )

    async def _reclaim(
        self, needed_bytes: int, reserved_bytes: int
    ) -> tuple[ReclaimReport, _Unpublished, OccupancyReport]:
        """Unregister enough for `needed_bytes`, returning the pass and what is left.

        Unreferenced objects go first: they are pure waste, and freeing them may make
        evicting content another worker is about to read unnecessary. Reconciliation
        deletes them here, under their entry claims, because those claims are what keep a
        reader mid-download of a released attempt safe. The listing is taken again
        afterwards, because what reconciliation deleted is exactly what must not be
        counted as either occupancy or an eviction candidate.

        Evicted entries are only unregistered. Their bytes count as freed for the
        decision and are deleted by the caller once the holding has committed; a delete
        that then fails leaves an orphan the next admission's listing counts, never a
        registration naming absent bytes.

        Returns:
            The reclamation as reported, the unregistrations whose bytes the caller must
            delete, and the occupancy left once they are gone.
        """
        orphans_deleted = await self._index.reconcile(self._store)
        refs = await list_staged_objects(self._store)
        listed = sum(ref.size_bytes for ref in refs)

        deadline = self._clock() - self._max_age
        aged = await self._unpublish(
            [ref for ref in refs if ref.last_modified <= deadline], limit=None
        )

        # Oldest first under pressure: the least recently staged entry is the one whose
        # re-fetch is least likely to be needed again shortly.
        surplus = listed - aged.size_bytes + reserved_bytes + needed_bytes - self._max_bytes
        pressured = (
            await self._unpublish(
                [ref for ref in refs if ref.last_modified > deadline], limit=surplus
            )
            if surplus > 0
            else _NOTHING_UNPUBLISHED
        )

        unpublished = aged + pressured
        reclaimed = ReclaimReport(
            evicted=tuple(registration.key for registration in unpublished.registrations),
            protected=unpublished.protected,
            orphans_deleted=orphans_deleted,
            freed_bytes=unpublished.size_bytes,
        )
        remaining = OccupancyReport(
            total_bytes=listed - unpublished.size_bytes,
            object_count=len(refs) - len(unpublished.registrations),
            registered_bytes=await self._index.total_registered_bytes(),
            reserved_bytes=reserved_bytes,
        )
        return reclaimed, unpublished, remaining

    async def _abandon(
        self, key: StagingKey, attempt_id: UUID, reserved_bytes: int, *, cancelled: bool
    ) -> None:
        """Remove a reservation its block ended without registering, best effort.

        Holds the writer lock as `occupancy.abandon`. After a cancellation the removal is
        shielded from the cancellation that is already unwinding the block and bounded by
        `_ABANDON_SECONDS`; a timed-out removal may still land in the background. Every
        failure is logged and left to expiry, so it never replaces the exception the
        block is ending with.
        """
        try:
            if cancelled:
                async with asyncio.timeout(_ABANDON_SECONDS):
                    await asyncio.shield(self._forget(attempt_id))
            else:
                await self._forget(attempt_id)
        except Exception as error:
            failure: BaseException = error
        except asyncio.CancelledError as error:
            if not cancelled:
                raise
            # A second cancellation during the shielded removal: the block is already
            # ending with one, so this one changes nothing but the cleanup.
            failure = error
        else:
            return
        logger.warning(
            "staging.reservation_abandon_failed",
            staging_key=str(key),
            attempt_id=str(attempt_id),
            reserved_bytes=reserved_bytes,
            error_type=type(failure).__name__,
        )

    async def _forget(self, attempt_id: UUID) -> None:
        """Remove `attempt_id`'s reservation in a holding of its own."""
        async with self._index.writer_lock("occupancy.abandon"):
            await self._index.forget_reservation(attempt_id)

    async def _unpublish(
        self, candidates: Sequence[StagedObjectRef], *, limit: int | None
    ) -> _Unpublished:
        """Unregister `candidates` in order, stopping once `limit` bytes are unpublished.

        Every candidate goes through the index's try-lock eviction rather than a direct
        delete, so an entry a download holds is skipped instead of truncated. A key is
        considered once per pass: a second object under a key this pass already evicted
        is a losing attempt, and evicting the key again would unregister whatever has
        been staged under it since. Runs under the writer lock; the bytes are the
        caller's to delete once it has committed.
        """
        registrations: list[StagedObject] = []
        protected: list[StagingKey] = []
        considered: set[StagingKey] = set()
        unpublished = 0
        for ref in candidates:
            if limit is not None and unpublished >= limit:
                break
            key = staging_key_of(ref.object_key)
            if key in considered:
                continue
            considered.add(key)
            registration = await self._index.evict(key)
            if registration is None:
                # None is both "a download holds it" and "nothing is registered"; either
                # answer means the same thing here, which is to try the next candidate.
                protected.append(key)
                continue
            # Counted from the registration, which names the object that will be deleted;
            # `ref` may be an older attempt that lost this key.
            registrations.append(registration)
            unpublished += registration.size_bytes
        return _Unpublished(registrations=tuple(registrations), protected=tuple(protected))

    async def _delete_unpublished(self, unpublished: _Unpublished) -> _Reclamation:
        """Delete the bytes of already-unregistered entries, carrying any failure back.

        Called only once the unregistrations have committed. One refusal does not end the
        pass: every remaining entry is unregistered too, and deleting what can be deleted
        is the point. The failure is returned rather than raised so the caller can finish
        its own bookkeeping before reporting it.

        The whole pass runs on one batched connection. A pass that reclaims thousands of
        entries is thousands of serial deletes, and opening a connection for each of them
        would pay a connect and a handshake per delete while the sweep, or the admission
        waiting to upload, waits on them.
        """
        if not unpublished.registrations:
            # An idle pass must not open a connection only to find it has nothing to do.
            return _Reclamation(report=replace(_NOTHING_RECLAIMED, protected=unpublished.protected))
        evicted: list[StagingKey] = []
        undeleted: list[str] = []
        failure: StagingObjectStoreError | None = None
        freed = 0
        async with self._store.batch():
            for registration in unpublished.registrations:
                try:
                    await self._store.delete(registration.object_key)
                except StagingObjectStoreError as error:
                    undeleted.append(registration.object_key)
                    if failure is None:
                        failure = error
                    continue
                evicted.append(registration.key)
                freed += registration.size_bytes
        return _Reclamation(
            report=ReclaimReport(
                evicted=tuple(evicted),
                protected=unpublished.protected,
                orphans_deleted=0,
                freed_bytes=freed,
            ),
            failure=None if failure is None else _deletion_deferred(undeleted, failure),
        )

    def _expired(self, refs: Sequence[StagedObjectRef]) -> list[StagedObjectRef]:
        """Return the entries past the age bound, oldest first."""
        deadline = self._clock() - self._max_age
        return [ref for ref in refs if ref.last_modified <= deadline]
