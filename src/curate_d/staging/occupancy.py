"""Occupancy accounting, eviction and completion release for S3-backed staging.

The bound is measured from the object store's own listing, not from the cache index's
registered rows. Registered bytes are what is currently published; occupied bytes are
every completed object under the content prefix — another worker's as much as this
one's, and orphans no registration refers to any more. Accounting from rows alone
understates the second number and admits work that does not fit, so the listing is
paginated to exhaustion and a listing that stopped early is treated as no answer rather
than as a smaller one.

Reservation happens before a byte of the source is read: reserving afterwards is not a
bound, it is a report. Admission holds the global writer lock across the decision, the
upload it admits and the registration that publishes it, so two workers cannot spend the
same headroom and reconciliation cannot race a publication. The lock is re-entrant for
the admitting context, so publishing inside the reservation does not queue behind it.

That lock is global, so the ceiling is explicit: with any number of I/O pods the cluster
stages one object at a time, a long transfer also delays the idle sweep, and the entry
locks reclamation took are held for that whole window, so a reader of a key that merely
collides with a reclaimed one waits behind the transfer. Reviewed and kept on 2026-09-15:
`staging_transfer_timeout_seconds` bounds the worst case, and the alternative — narrowing
the lock to admission plus registration, with a reservation row carrying the interval
between them — is a different design whose crash semantics need their own evidence.
Revisit when measured upload concurrency, rather than speculation, shows it binding. The
`staging.writer_lock_held` and `staging.reader_lock_wait` log events are that measurement
(see `docs/staging.md`).

What this bounds is active staged content. It is not a provider billing cap: incomplete
multipart uploads bill separately and are reclaimed by explicit abort and the backend's
residual cleanup, and are reported rather than folded into this budget.
"""

from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final, Protocol, runtime_checkable

from curate_d.clock import utc_now
from curate_d.staging.cache_index import staging_key_of
from curate_d.staging.errors import (
    StagingCapacityExhaustedError,
    StagingError,
    StagingObjectStoreError,
)
from curate_d.staging.keys import StagingKey
from curate_d.staging.objects import (
    StagedObject,
    StagedObjectRef,
    StagingCacheIndex,
    StagingObjectStore,
)

type Clock = Callable[[], datetime]


@dataclass(frozen=True)
class OccupancyReport:
    """What the object store actually holds under the content prefix.

    `total_bytes` is the number admission bounds: every completed object, whoever wrote
    it and whether or not a registration still refers to it. `registered_bytes` is the
    published subset, carried alongside so the unreferenced remainder is a reported
    quantity rather than a shortfall discovered at admission time.
    """

    total_bytes: int
    object_count: int
    registered_bytes: int

    @property
    def unreferenced_bytes(self) -> int:
        """Completed bytes no registration refers to, awaiting reconciliation.

        Never negative. A registration whose object has already been expired remotely is
        a stale row for a reader to invalidate, not negative occupancy.
        """
        return max(self.total_bytes - self.registered_bytes, 0)


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


@dataclass(frozen=True)
class _Reclamation:
    """One reclamation pass's report, and the deletion failure it deferred.

    A pass reports a failed deletion rather than raising it, because its row deletions
    run on the caller's still-open writer transaction: unwinding through that transaction
    would restore registrations naming bytes that are really gone. Nothing repairs such a
    row — reconciliation walks the listing, so it never visits a row whose object is
    absent, and registration's conflict rule lets the stale row beat a fresh re-stage.
    An unregistered object is the repairable side of the same failure.
    """

    report: ReclaimReport
    failure: StagingObjectStoreError | None = None


@dataclass(frozen=True)
class AdmissionReport:
    """The accepted reservation and the state of staging it was accepted against."""

    occupancy: OccupancyReport
    reserved_bytes: int
    reclaimed: ReclaimReport


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
_NOTHING_DROPPED: Final = _Reclamation(report=_NOTHING_RECLAIMED)


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
        """Delete every completed object no registration refers to, returning the count."""
        ...


async def measure_occupancy(store: StagingObjectStore, index: StagingCacheIndex) -> OccupancyReport:
    """Total every completed object under the content prefix, paginating to exhaustion.

    Raises:
        StagingObjectStoreError: The listing or the registered total did not complete. A
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
    )


async def list_staged_objects(store: StagingObjectStore) -> list[StagedObjectRef]:
    """Return every completed object under the content prefix, oldest first.

    Materialized rather than streamed because eviction order is a property of the whole
    listing: an oldest-first pass cannot begin until the last page has been read.
    """
    refs = [ref async for ref in store.list_objects()]
    refs.sort(key=lambda ref: ref.last_modified)
    return refs


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
        clock: Clock = utc_now,
    ) -> None:
        """Bind the backends and the bounds admission is decided against.

        Args:
            store: Object store whose listing is the authoritative occupancy.
            index: Cache index providing the writer lock and the protected-eviction path.
            max_bytes: Ceiling on active completed staged bytes.
            max_age: Age at which a staged entry becomes evictable regardless of pressure.
            clock: Source of the current instant, injected so age eviction is pinned by
                the caller rather than by how long a test took to run.

        Raises:
            ValueError: A non-positive bound, which would refuse every admission.
        """
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if max_age <= timedelta():
            raise ValueError("max_age must be positive")
        self._store = store
        self._index = index
        self._max_bytes = max_bytes
        self._max_age = max_age
        self._clock = clock

    async def measure(self) -> OccupancyReport:
        """Return current occupancy, including objects this worker never wrote.

        Raises:
            StagingObjectStoreError: The listing or the registered total did not
                complete.
        """
        return await measure_occupancy(self._store, self._index)

    @asynccontextmanager
    async def reserve(self, declared_max_bytes: int) -> AsyncGenerator[AdmissionReport, None]:
        """Reserve `declared_max_bytes` for the duration of the block.

        The reservation is the writer lock itself: admission, the upload it admits and
        the registration that publishes it happen inside one holding, so the headroom
        cannot be handed to a second worker in between. Enter this before reading the
        source; a reservation taken after the bytes have landed bounds nothing.

        Args:
            declared_max_bytes: Upper bound on what the incoming stream may deliver. The
                object store enforces it while uploading, so an oversized stream fails
                rather than consuming capacity nobody reserved.

        Yields:
            The accepted reservation and the occupancy it was accepted against.

        Raises:
            ValueError: A negative declaration.
            StagingCapacityExhaustedError: The declaration exceeds the whole bound, or
                eviction could not free enough because the remaining candidates are
                being downloaded.
            StagingObjectStoreError: Occupancy could not be measured, or a deletion
                during eviction failed. Raised after the admission transaction has
                committed, so the entries eviction already deleted stay unregistered.
        """
        async with self._index.writer_lock("occupancy.reserve"):
            try:
                admitted = await self._admit(declared_max_bytes)
            except StagingError as refusal:
                # Carried out of the block rather than raised inside it: admission runs
                # eviction, and unwinding through the writer transaction would restore
                # registrations for objects this pass has already deleted.
                deferred = refusal
            else:
                yield admitted
                return
        raise deferred

    async def sweep(self) -> ReclaimReport:
        """Reclaim orphans and aged entries, whether or not anything is being staged.

        The periodic cleanup path, and the normal one: pressure eviction is what happens
        when it has not run often enough. Reconciliation and unregistration are held
        under the writer lock so reconciliation cannot mistake an object whose
        registration is still in flight for an orphan.

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

    async def _admit(self, declared_max_bytes: int) -> AdmissionReport:
        """Decide one admission under an already-held writer lock."""
        if declared_max_bytes < 0:
            raise ValueError("declared_max_bytes must be nonnegative")
        if declared_max_bytes > self._max_bytes:
            raise StagingCapacityExhaustedError(
                f"staging cannot hold {declared_max_bytes} bytes: the active bound is "
                f"{self._max_bytes} bytes, so this content will never fit"
            )

        occupancy = await self.measure()
        if occupancy.total_bytes + declared_max_bytes <= self._max_bytes:
            return AdmissionReport(
                occupancy=occupancy,
                reserved_bytes=declared_max_bytes,
                reclaimed=_NOTHING_RECLAIMED,
            )

        reclamation, occupied, object_count = await self._reclaim(declared_max_bytes)
        if reclamation.failure is not None:
            raise reclamation.failure
        reclaimed = reclamation.report
        if occupied + declared_max_bytes > self._max_bytes:
            raise StagingCapacityExhaustedError(
                f"staging needs {declared_max_bytes} bytes but holds {occupied} of "
                f"{self._max_bytes} after freeing {reclaimed.freed_bytes}; "
                f"{len(reclaimed.protected)} entries are being downloaded and were not "
                f"evicted"
            )
        return AdmissionReport(
            occupancy=OccupancyReport(
                total_bytes=occupied,
                object_count=object_count,
                registered_bytes=await self._index.total_registered_bytes(),
            ),
            reserved_bytes=declared_max_bytes,
            reclaimed=reclaimed,
        )

    async def _reclaim(self, needed_bytes: int) -> tuple[_Reclamation, int, int]:
        """Free room for `needed_bytes`, returning the pass and what is left.

        Unreferenced objects go first: they are pure waste, and freeing them may make
        evicting content another worker is about to read unnecessary. The listing is
        taken again afterwards, because what reconciliation deleted is exactly what must
        not be counted as either occupancy or an eviction candidate.
        """
        orphans_deleted = await self._index.reconcile(self._store)
        refs = await list_staged_objects(self._store)
        occupied = sum(ref.size_bytes for ref in refs)

        deadline = self._clock() - self._max_age
        expired = [ref for ref in refs if ref.last_modified <= deadline]
        aged = await self._drop(expired, limit=None)
        occupied -= aged.report.freed_bytes

        # Oldest first under pressure: the least recently staged entry is the one whose
        # re-fetch is least likely to be needed again shortly. A failed deletion stops
        # the pass here: admission is going to fail anyway, and unregistering further
        # entries against a store that just refused only makes more orphans.
        surplus = occupied + needed_bytes - self._max_bytes
        fresh = [ref for ref in refs if ref.last_modified > deadline]
        pressured = (
            await self._drop(fresh, limit=surplus)
            if aged.failure is None and surplus > 0
            else _NOTHING_DROPPED
        )
        occupied -= pressured.report.freed_bytes

        reclaimed = ReclaimReport(
            evicted=aged.report.evicted + pressured.report.evicted,
            protected=tuple(dict.fromkeys(aged.report.protected + pressured.report.protected)),
            orphans_deleted=orphans_deleted,
            freed_bytes=aged.report.freed_bytes + pressured.report.freed_bytes,
        )
        return (
            _Reclamation(report=reclaimed, failure=aged.failure or pressured.failure),
            occupied,
            len(refs) - len(reclaimed.evicted),
        )

    async def _drop(
        self, candidates: Sequence[StagedObjectRef], *, limit: int | None
    ) -> _Reclamation:
        """Unpublish and delete `candidates` in one holding of the writer lock.

        The admission path's shape, where the lock is held across the upload being
        admitted and the deletion therefore cannot wait for the commit. `sweep` and
        `release` use the two halves separately and delete afterwards, which is the
        stronger ordering; this one relies on the caller reporting a failure only once
        the block has committed.
        """
        return await self._delete_unpublished(await self._unpublish(candidates, limit=limit))

    async def _unpublish(
        self, candidates: Sequence[StagedObjectRef], *, limit: int | None
    ) -> _Unpublished:
        """Unregister `candidates` in order, stopping once `limit` bytes are unpublished.

        Every candidate goes through the index's try-lock eviction rather than a direct
        delete, so an entry a download holds is skipped instead of truncated. A key is
        considered once per pass: a second object under a key this pass already evicted
        is a losing attempt, and evicting the key again would unregister whatever has
        been staged under it since.
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

        One refusal does not end the pass: every remaining entry is unregistered too, and
        deleting what can be deleted is the point. The failure is returned rather than
        raised so a caller still inside the writer transaction can commit before
        reporting it.

        The whole pass runs on one batched connection. A pass that reclaims thousands of
        entries is thousands of serial deletes, and opening a connection for each of them
        would pay a connect and a handshake per delete while the writer lock is held.
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
