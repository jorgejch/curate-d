"""Cache index behaviour that needs no database: lock derivation, mapping, cleanup, reservations.

The object store is a fake with a scripted listing, and the "database" is an engine
pointed at a closed port, which is how the typed-failure contract is checked without
depending on a server being up. Cross-session locking is the integration suite's job;
the lock timings are checked here against a scripted engine whose acquisitions take a
known time.
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import BinaryIO, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Executable, TextClause
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from structlog.testing import capture_logs

from curate_d.ledger.mapping import LedgerRowError
from curate_d.staging.cache_index import (
    _ENTRY_LOCK_CLASS,
    PostgresStagingCacheIndex,
    _claim_entry,
    delete_orphans,
    entry_lock_id,
    registration_values,
    staging_key_of,
    to_staged_object,
)
from curate_d.staging.errors import StagingObjectStoreError
from curate_d.staging.keys import StagingKey
from curate_d.staging.objects import (
    StagedObject,
    StagedObjectRef,
    StagingCacheIndex,
    StagingObjectStore,
)

_WHEN = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
_DIGEST = "b" * 64

# A closed port rather than a bad hostname: the refusal is immediate and needs no DNS.
_UNREACHABLE = "postgresql+psycopg://curate:curate@127.0.0.1:1/absent"

_INT4_MIN = -(2**31)
_INT4_MAX = 2**31 - 1


def key_for(seed: int) -> StagingKey:
    """A valid staging key, distinct per seed."""
    return StagingKey(f"{seed:032x}")


def staged(key: StagingKey, *, attempt: UUID | None = None, size: int = 1024) -> StagedObject:
    """A verified upload for `key`, with an optional attempt identifier and size."""
    return StagedObject(
        key=key,
        attempt_id=attempt if attempt is not None else uuid4(),
        size_bytes=size,
        sha256=_DIGEST,
        created_at=_WHEN,
    )


class FakeObjectStore:
    """Lists the object names it was handed and records the deletions it is asked for.

    Numbers the client each operation runs on — the one a surrounding `batch()` pinned,
    or one opened for the operation alone — so a test can tell a cleanup pass that shares
    a connection from one that opens a connection per delete.
    """

    def __init__(self, *object_keys: str, rejects: str | None = None) -> None:
        self.listing = list(object_keys)
        self.deleted: list[str] = []
        self.delete_clients: list[int] = []
        self.clients_opened = 0
        self._rejects = rejects
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
        if object_key == self._rejects:
            raise StagingObjectStoreError(f"delete of {object_key} was rejected")
        self.deleted.append(object_key)

    async def list_objects(self) -> AsyncIterator[StagedObjectRef]:
        self._client()
        for object_key in self.listing:
            yield StagedObjectRef(object_key=object_key, size_bytes=1024, last_modified=_WHEN)

    async def abort_incomplete_uploads(self, older_than: datetime) -> int:
        raise NotImplementedError

    def _client(self) -> int:
        """Return the client this operation runs on: the pinned one, or a fresh one."""
        return self._pinned if self._pinned is not None else self._open_client()

    def _open_client(self) -> int:
        """Open the next numbered client, standing in for a connect and a handshake."""
        self.clients_opened += 1
        return self.clients_opened


class ClaimRecorder:
    """An entry claim with a fixed answer, remembering which keys it was asked about."""

    def __init__(self, *, granted: bool = True) -> None:
        self.granted = granted
        self.asked: list[StagingKey] = []

    async def __call__(self, key: StagingKey) -> bool:
        self.asked.append(key)
        return self.granted


class ScriptedResult:
    """One scalar answer, standing in for the cursor a lock probe returns."""

    def __init__(self, scalar: object) -> None:
        self._scalar = scalar

    def scalar_one(self) -> object:
        return self._scalar


class ProbeConnection:
    """Answers every statement with a fixed scalar, remembering what it was asked."""

    def __init__(self, scalar: object) -> None:
        self._scalar = scalar
        self.statements: list[str] = []
        self.parameters: list[Mapping[str, int]] = []

    async def execute(self, statement: TextClause, parameters: Mapping[str, int]) -> ScriptedResult:
        self.statements.append(str(statement))
        self.parameters.append(parameters)
        return ScriptedResult(self._scalar)


async def claim(connection: ProbeConnection, key: StagingKey) -> bool:
    """Run the real claim against a scripted connection."""
    return await _claim_entry(cast(AsyncConnection, connection), key)


async def test_a_granted_claim_is_the_authority_to_delete_an_entrys_bytes() -> None:
    """Both eviction and cleanup gate destructive work on exactly this answer."""
    assert await claim(ProbeConnection(True), key_for(1)) is True


async def test_a_refused_claim_means_a_download_holds_the_entry() -> None:
    """The reader's shared lock is the one thing the writer lock does not already cover."""
    assert await claim(ProbeConnection(False), key_for(1)) is False


@pytest.mark.parametrize("scalar", [None, 1, "t"])
async def test_only_a_true_answer_grants_the_claim(scalar: object) -> None:
    """Anything else is an answer nobody can read as "no reader holds this"."""
    assert await claim(ProbeConnection(scalar), key_for(1)) is False


async def test_a_claim_never_waits_on_the_lock_it_probes() -> None:
    """A blocking acquisition here would stall reclamation behind the download it skips."""
    connection = ProbeConnection(True)

    await claim(connection, key_for(1))

    assert connection.statements == ["SELECT pg_try_advisory_xact_lock(:lock_class, :lock_object)"]


async def test_a_claim_locks_the_entry_rather_than_the_whole_writer() -> None:
    """A key hashing onto the writer lock would otherwise block every writer in the cluster."""
    key = key_for(5)
    connection = ProbeConnection(True)

    await claim(connection, key)

    assert connection.parameters == [
        {"lock_class": _ENTRY_LOCK_CLASS, "lock_object": entry_lock_id(key)}
    ]


@pytest.fixture
async def unreachable_index() -> AsyncIterator[PostgresStagingCacheIndex]:
    """An index whose database cannot be reached, standing in for a lost connection."""
    engine = create_async_engine(_UNREACHABLE, connect_args={"connect_timeout": 2})
    try:
        yield PostgresStagingCacheIndex(engine)
    finally:
        await engine.dispose()


def test_the_index_satisfies_the_cache_index_contract(
    unreachable_index: PostgresStagingCacheIndex,
) -> None:
    """The implementation is usable everywhere the protocol is the declared dependency."""
    index: StagingCacheIndex = unreachable_index
    assert isinstance(index, StagingCacheIndex)


def test_the_fake_store_satisfies_the_object_store_contract() -> None:
    """The cleanup tests exercise the same surface the real backend implements."""
    store: StagingObjectStore = FakeObjectStore()
    assert isinstance(store, StagingObjectStore)


def test_entry_lock_ids_are_stable_across_processes() -> None:
    """Two pods must derive the same lock for one entry, so the value is pinned here."""
    assert entry_lock_id(key_for(1)) == entry_lock_id(key_for(1))
    assert entry_lock_id(StagingKey("0" * 32)) == 1785464681


def test_entry_lock_ids_separate_distinct_keys() -> None:
    """Distinct entries lock independently; a shared id would serialize unrelated reads."""
    ids = {entry_lock_id(key_for(seed)) for seed in range(64)}
    assert len(ids) == 64


def test_two_distinct_keys_can_share_an_entry_lock() -> None:
    """The lock space is four bytes, so a reader can wait behind a key it never asked for."""
    first, second = key_for(0xEAE7), key_for(0x12388)
    assert first != second
    assert entry_lock_id(first) == entry_lock_id(second)


@pytest.mark.parametrize("seed", [0, 1, 2**127, 2**64 - 1])
def test_entry_lock_ids_fit_a_postgres_integer(seed: int) -> None:
    """Advisory lock arguments are int4; an out-of-range value would fail at the server."""
    assert _INT4_MIN <= entry_lock_id(key_for(seed)) <= _INT4_MAX


def test_the_staging_key_of_an_object_name_is_its_prefix() -> None:
    """Cleanup locks an orphan's entry, and an orphan's key exists only in its name."""
    obj = staged(key_for(7))
    assert staging_key_of(obj.object_key) == obj.key


def test_an_object_name_without_an_attempt_still_yields_a_key() -> None:
    """A stray name is still locked before deletion rather than deleted unprotected."""
    assert staging_key_of("0" * 32) == key_for(0)


def test_a_registration_round_trips_through_its_row() -> None:
    """What is written is what is read back, including the attempt the winner published."""
    obj = staged(key_for(3))
    assert to_staged_object(registration_values(obj)) == obj


def test_a_row_of_the_wrong_shape_is_rejected_by_column() -> None:
    """A schema drift fails naming the column, not three frames later inside a decoder."""
    row = registration_values(staged(key_for(3)))
    row["size_bytes"] = "1024"
    with pytest.raises(LedgerRowError, match="size_bytes"):
        to_staged_object(row)


def test_a_naive_timestamp_is_rejected() -> None:
    """A naive created_at is rejected; the column is timestamptz, so naive is schema drift."""
    row = registration_values(staged(key_for(3)))
    row["created_at"] = datetime(2026, 5, 1, 12, 0)  # noqa: DTZ001 - the point of the test
    with pytest.raises(LedgerRowError, match="timezone"):
        to_staged_object(row)


async def test_cleanup_deletes_objects_no_registration_refers_to() -> None:
    """An orphan is a completed object with no row; that is the whole definition."""
    winner = staged(key_for(1))
    loser = staged(key_for(1))
    store = FakeObjectStore(winner.object_key, loser.object_key)

    deleted = await delete_orphans(store, {winner.object_key}, claim=ClaimRecorder())

    assert deleted == 1
    assert store.deleted == [loser.object_key]


async def test_cleanup_leaves_a_fully_registered_store_alone() -> None:
    """Reconciliation is a sweep, not a purge; a clean store loses nothing."""
    objects = [staged(key_for(seed)) for seed in range(3)]
    store = FakeObjectStore(*(obj.object_key for obj in objects))

    deleted = await delete_orphans(
        store, {obj.object_key for obj in objects}, claim=ClaimRecorder()
    )

    assert deleted == 0
    assert store.deleted == []


async def test_cleanup_of_an_empty_store_is_not_an_error() -> None:
    """The sweep runs on a schedule, so most runs find nothing to do."""
    store = FakeObjectStore()
    assert await delete_orphans(store, set(), claim=ClaimRecorder()) == 0


async def test_cleanup_spares_an_orphan_a_reader_still_holds() -> None:
    """A released entry can still be mid-download; deleting it would truncate that read."""
    released = staged(key_for(2))
    store = FakeObjectStore(released.object_key)

    deleted = await delete_orphans(store, set(), claim=ClaimRecorder(granted=False))

    assert deleted == 0
    assert store.deleted == []


async def test_cleanup_claims_the_entry_rather_than_the_object() -> None:
    """The lock protecting a download is per staging key, not per upload attempt."""
    orphan = staged(key_for(5))
    store = FakeObjectStore(orphan.object_key)
    claim = ClaimRecorder()

    await delete_orphans(store, set(), claim=claim)

    assert claim.asked == [orphan.key]


async def test_cleanup_stops_when_the_store_rejects_a_deletion() -> None:
    """A store that refuses is not one whose remaining answers should be acted on."""
    first = staged(key_for(1))
    second = staged(key_for(2))
    third = staged(key_for(3))
    store = FakeObjectStore(
        first.object_key, second.object_key, third.object_key, rejects=second.object_key
    )

    with pytest.raises(StagingObjectStoreError):
        await delete_orphans(store, set(), claim=ClaimRecorder())

    assert store.deleted == [first.object_key]


async def test_cleanup_deletes_every_orphan_on_one_connection() -> None:
    """A sweep collecting thousands of orphans must not connect once per delete."""
    orphans = [staged(key_for(seed)) for seed in range(10)]
    store = FakeObjectStore(*(obj.object_key for obj in orphans))

    deleted = await delete_orphans(store, set(), claim=ClaimRecorder())

    assert deleted == 10
    assert len(store.delete_clients) == 10
    assert len(set(store.delete_clients)) == 1
    # The listing rides the same pinned client, so the whole pass is one connection.
    assert store.clients_opened == 1


async def test_registering_without_a_database_is_a_typed_failure(
    unreachable_index: PostgresStagingCacheIndex,
) -> None:
    """A lost connection surfaces as a staging failure, not as a driver exception."""
    with pytest.raises(StagingObjectStoreError, match="writer lock"):
        await unreachable_index.register(staged(key_for(1)))


async def test_lookup_without_a_database_is_a_typed_failure(
    unreachable_index: PostgresStagingCacheIndex,
) -> None:
    """A reader cannot be left deciding whether a driver error means a cache miss."""
    with pytest.raises(StagingObjectStoreError, match="look up"):
        await unreachable_index.lookup(key_for(1))


async def test_release_without_a_database_is_a_typed_failure(
    unreachable_index: PostgresStagingCacheIndex,
) -> None:
    """A release that did not commit must be visibly unfinished so it can be repeated."""
    with pytest.raises(StagingObjectStoreError):
        await unreachable_index.release(key_for(1))


async def test_accounting_without_a_database_is_a_typed_failure(
    unreachable_index: PostgresStagingCacheIndex,
) -> None:
    """Admission must fail closed rather than read an unreachable total as zero bytes."""
    with pytest.raises(StagingObjectStoreError):
        await unreachable_index.total_registered_bytes()


async def test_holding_for_download_without_a_database_is_a_typed_failure(
    unreachable_index: PostgresStagingCacheIndex,
) -> None:
    """An unheld reader lock must never look like a held one."""
    with pytest.raises(StagingObjectStoreError, match="hold staging key"):
        async with unreachable_index.hold_for_download(key_for(1)):
            pass


async def test_eviction_without_a_database_is_a_typed_failure(
    unreachable_index: PostgresStagingCacheIndex,
) -> None:
    """Eviction that cannot take the writer lock must not proceed to delete anything."""
    with pytest.raises(StagingObjectStoreError):
        await unreachable_index.evict(key_for(1))


async def test_reconciliation_without_a_database_touches_no_object(
    unreachable_index: PostgresStagingCacheIndex,
) -> None:
    """Without the registration list every object looks like an orphan; delete none."""
    store = FakeObjectStore(staged(key_for(1)).object_key)

    with pytest.raises(StagingObjectStoreError):
        await unreachable_index.reconcile(store)

    assert store.deleted == []


# Long enough to tell a measured wait from scheduling noise, short enough to be cheap.
_LOCK_DELAY = 0.02


class EmptyResult:
    """Answers every lock probe with `granted` and finds no row."""

    def __init__(self, granted: bool = True) -> None:
        """Answer every lock probe with `granted`."""
        self._granted = granted

    def scalar_one(self) -> bool:
        return self._granted

    def mappings(self) -> "EmptyResult":
        return self

    def one_or_none(self) -> None:
        return None

    def all(self) -> list[object]:
        """Return no rows."""
        return []

    def __iter__(self) -> Iterator[object]:
        """Iterate over no rows."""
        return iter(())


class SlowLockConnection:
    """Takes `delay` seconds to grant any blocking advisory lock, and finds no rows.

    `granted` is the answer to every entry try-lock, so a test can stand in for a reader
    holding the entry.
    """

    def __init__(self, delay: float, *, granted: bool = True) -> None:
        self.delay = delay
        self.granted = granted

    async def execute(
        self, statement: Executable, parameters: Mapping[str, int] | None = None
    ) -> EmptyResult:
        if "pg_advisory_xact_lock" in str(statement):
            await asyncio.sleep(self.delay)
        return EmptyResult(self.granted)


class ScriptedEngine:
    """Hands every transaction the same scripted connection.

    When built with `finalized`, ending a transaction waits for that event before it
    counts as committed or rolled back, standing in for a slow COMMIT or ROLLBACK:
    PostgreSQL holds a transaction-scoped lock until that point.
    """

    def __init__(
        self,
        connection: "SlowLockConnection | ReservationConnection",
        finalized: asyncio.Event | None = None,
    ) -> None:
        self.connection = connection
        self.finalized = finalized
        self.finalizing = asyncio.Event()
        self.outcomes: list[str] = []

    @asynccontextmanager
    async def begin(self) -> AsyncGenerator["SlowLockConnection | ReservationConnection", None]:
        outcome = "rollback"
        try:
            yield self.connection
            outcome = "commit"
        finally:
            self.finalizing.set()
            if self.finalized is not None:
                await self.finalized.wait()
            self.outcomes.append(outcome)

    @asynccontextmanager
    async def connect(self) -> AsyncGenerator["SlowLockConnection | ReservationConnection", None]:
        """Hand out the scripted connection outside any transaction."""
        yield self.connection


def slow_index(delay: float = _LOCK_DELAY, *, granted: bool = True) -> PostgresStagingCacheIndex:
    """Return an index whose lock acquisitions each take `delay` seconds."""
    connection = SlowLockConnection(delay, granted=granted)
    return PostgresStagingCacheIndex(cast(AsyncEngine, ScriptedEngine(connection)))


async def test_a_fresh_writer_lock_reports_its_wait_and_hold_on_release() -> None:
    """One event per holding, labelled by caller, timed from request and acquisition."""
    index = slow_index()

    with capture_logs() as events:
        async with index.writer_lock("occupancy.reserve"):
            assert events == []
            await asyncio.sleep(_LOCK_DELAY)

    assert len(events) == 1
    event = events[0]
    assert event["event"] == "staging.writer_lock_held"
    assert event["log_level"] == "info"
    assert event["caller"] == "occupancy.reserve"
    assert event["wait_seconds"] >= _LOCK_DELAY
    assert event["held_seconds"] >= _LOCK_DELAY
    assert event["entry_claims"] == 0


async def test_a_reentrant_writer_lock_reports_nothing_of_its_own() -> None:
    """The inner time is already inside the outer holding; counting it twice inflates demand."""
    index = slow_index()

    with capture_logs() as events:
        async with index.writer_lock("occupancy.reserve"):
            await index.register(staged(key_for(1)))
            async with index.writer_lock("occupancy.release"):
                pass

    assert [(e["event"], e["caller"]) for e in events] == [
        ("staging.writer_lock_held", "occupancy.reserve")
    ]


async def test_a_holding_that_fails_still_reports_its_time() -> None:
    """A failed admission held the lock all the same, and other writers waited for it."""
    index = slow_index()

    with capture_logs() as events, pytest.raises(RuntimeError):
        async with index.writer_lock("occupancy.sweep"):
            raise RuntimeError

    assert [e["caller"] for e in events] == ["occupancy.sweep"]


async def _hold_and_end(
    index: PostgresStagingCacheIndex, ending: str, inside: asyncio.Event
) -> None:
    """Take the writer lock and leave it by `ending`: commit, error or cancel."""
    async with index.writer_lock("occupancy.reserve"):
        inside.set()
        if ending == "error":
            raise RuntimeError
        if ending == "cancel":
            await asyncio.Event().wait()


@pytest.mark.parametrize(
    ("ending", "outcome"),
    [("commit", "commit"), ("error", "rollback"), ("cancel", "rollback")],
)
async def test_a_holding_is_reported_only_once_its_transaction_has_ended(
    ending: str, outcome: str
) -> None:
    """The lock is PostgreSQL's until COMMIT or ROLLBACK, so the hold runs to that point."""
    finalized = asyncio.Event()
    inside = asyncio.Event()
    engine = ScriptedEngine(SlowLockConnection(_LOCK_DELAY), finalized)
    index = PostgresStagingCacheIndex(cast(AsyncEngine, engine))

    with capture_logs() as events:
        holding = asyncio.create_task(_hold_and_end(index, ending, inside))
        if ending == "cancel":
            await inside.wait()
            holding.cancel()
        await engine.finalizing.wait()
        await asyncio.sleep(_LOCK_DELAY)
        assert events == []

        finalized.set()
        with suppress(RuntimeError, asyncio.CancelledError):
            await holding

    assert engine.outcomes == [outcome]
    assert len(events) == 1
    assert events[0]["event"] == "staging.writer_lock_held"
    assert events[0]["held_seconds"] >= _LOCK_DELAY


async def test_a_writer_lock_never_acquired_reports_no_holding(
    unreachable_index: PostgresStagingCacheIndex,
) -> None:
    """No acquisition, no hold: the failure itself is the caller's to report."""
    with capture_logs() as events, pytest.raises(StagingObjectStoreError):
        async with unreachable_index.writer_lock("occupancy.reserve"):
            pass

    assert events == []


async def test_a_reader_reports_its_shared_lock_wait_and_a_miss() -> None:
    """A wait on the reader's shared lock is time spent behind a reclaiming writer."""
    index = slow_index()
    key = key_for(7)

    with capture_logs() as events:
        async with index.hold_for_download(key) as held:
            assert held is None

    assert len(events) == 1
    event = events[0]
    assert event["event"] == "staging.reader_lock_wait"
    assert event["log_level"] == "info"
    assert event["staging_key"] == str(key)
    assert event["registered"] is False
    assert event["wait_seconds"] >= _LOCK_DELAY


def held_events[E: Mapping[str, object]](events: list[E]) -> list[E]:
    """Return the writer-lock holding events among `events`."""
    return [e for e in events if e["event"] == "staging.writer_lock_held"]


async def test_a_holding_counts_the_entry_locks_its_evictions_took() -> None:
    """Both claims stay held to the end of the holding, registered or not."""
    index = slow_index(0)

    with capture_logs() as events:
        async with index.writer_lock("occupancy.reserve"):
            assert await index.evict(key_for(1)) is None
            assert await index.evict(key_for(2)) is None

    assert [(e["caller"], e["entry_claims"]) for e in held_events(events)] == [
        ("occupancy.reserve", 2)
    ]


async def test_one_entry_lock_claimed_twice_counts_once() -> None:
    """A re-taken advisory lock is one lock, and one lock is what a reader waits on."""
    index = slow_index(0)

    with capture_logs() as events:
        async with index.writer_lock("occupancy.reserve"):
            await index.evict(key_for(1))
            await index.evict(key_for(1))

    assert held_events(events)[0]["entry_claims"] == 1


async def test_keys_colliding_on_one_entry_lock_count_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count is of locks held, not of keys touched."""
    monkeypatch.setattr("curate_d.staging.cache_index.entry_lock_id", lambda key: 42)
    index = slow_index(0)

    with capture_logs() as events:
        async with index.writer_lock("occupancy.reserve"):
            await index.evict(key_for(1))
            await index.evict(key_for(2))

    assert held_events(events)[0]["entry_claims"] == 1


async def test_a_refused_claim_is_not_counted() -> None:
    """A reader holding the entry means the writer holds nothing a reader could wait on."""
    index = slow_index(0, granted=False)

    with capture_logs() as events:
        async with index.writer_lock("occupancy.sweep"):
            assert await index.evict(key_for(1)) is None

    assert held_events(events)[0]["entry_claims"] == 0


async def test_the_claim_count_starts_afresh_for_each_holding() -> None:
    """Locks taken by an earlier transaction were released with it."""
    index = slow_index(0)

    with capture_logs() as events:
        async with index.writer_lock("occupancy.reserve"):
            await index.evict(key_for(1))
            await index.evict(key_for(2))
        async with index.writer_lock("occupancy.release"):
            await index.evict(key_for(3))
        async with index.writer_lock("occupancy.sweep"):
            pass

    assert [(e["caller"], e["entry_claims"]) for e in held_events(events)] == [
        ("occupancy.reserve", 2),
        ("occupancy.release", 1),
        ("occupancy.sweep", 0),
    ]


async def test_reconciliation_counts_the_claim_it_takes_on_an_orphan() -> None:
    """Collecting an orphan locks its key, which is the window #1 describes."""
    index = slow_index(0)
    orphan = staged(key_for(4))
    store = FakeObjectStore(orphan.object_key)

    with capture_logs() as events:
        async with index.writer_lock("occupancy.sweep"):
            assert await index.reconcile(store) == 1

    assert store.deleted == [orphan.object_key]
    assert [(e["caller"], e["entry_claims"]) for e in held_events(events)] == [
        ("occupancy.sweep", 1)
    ]


async def test_a_fresh_evict_reports_its_own_claim() -> None:
    """Outside an occupancy holding the index's own acquisition carries the count."""
    index = slow_index(0)

    with capture_logs() as events:
        await index.evict(key_for(1))

    assert [(e["caller"], e["entry_claims"]) for e in held_events(events)] == [("index.evict", 1)]


async def test_a_failed_holding_still_reports_its_claims() -> None:
    """The claims were held until the rollback, and readers waited on them all the same."""
    index = slow_index(0)

    with capture_logs() as events, pytest.raises(RuntimeError):
        async with index.writer_lock("occupancy.reserve"):
            await index.evict(key_for(1))
            raise RuntimeError

    assert held_events(events)[0]["entry_claims"] == 1


async def test_cleanup_spares_an_object_a_live_reservation_names() -> None:
    """A completed upload awaiting its registration is in flight, not an orphan."""
    reserved = staged(key_for(6))
    orphan = staged(key_for(7))
    store = FakeObjectStore(reserved.object_key, orphan.object_key)

    deleted = await delete_orphans(store, {reserved.object_key}, claim=ClaimRecorder())

    assert deleted == 1
    assert store.deleted == [orphan.object_key]


async def test_reserving_without_a_database_is_a_typed_failure(
    unreachable_index: PostgresStagingCacheIndex,
) -> None:
    """Admission must not proceed on headroom it could not record as claimed."""
    with pytest.raises(StagingObjectStoreError, match="writer lock"):
        await unreachable_index.reserve(key_for(1), uuid4(), 1024, timedelta(minutes=10))


async def test_consuming_a_reservation_without_a_database_is_a_typed_failure(
    unreachable_index: PostgresStagingCacheIndex,
) -> None:
    """A registration that cannot confirm its reservation must not read that as lost or held."""
    with pytest.raises(StagingObjectStoreError, match="writer lock"):
        await unreachable_index.consume_reservation(uuid4())


async def test_forgetting_a_reservation_without_a_database_is_a_typed_failure(
    unreachable_index: PostgresStagingCacheIndex,
) -> None:
    """An abandonment that did not commit is visible, so the caller can leave it to expiry."""
    with pytest.raises(StagingObjectStoreError, match="writer lock"):
        await unreachable_index.forget_reservation(uuid4())


async def test_reserved_accounting_without_a_database_is_a_typed_failure(
    unreachable_index: PostgresStagingCacheIndex,
) -> None:
    """Admission must fail closed rather than read unreachable reservations as none."""
    with pytest.raises(StagingObjectStoreError, match="reserved staging bytes"):
        await unreachable_index.total_reserved_bytes()


class RowsResult:
    """Grants every lock probe and answers row reads with the scripted rows."""

    def __init__(self, rows: Sequence[Mapping[str, object]]) -> None:
        """Answer row reads with `rows`."""
        self._rows = list(rows)

    def scalar_one(self) -> bool:
        """Grant the lock probe."""
        return True

    def mappings(self) -> "RowsResult":
        """Return the rows as mappings, which they already are."""
        return self

    def one_or_none(self) -> Mapping[str, object] | None:
        """Return the single scripted row, or None when there is none."""
        return self._rows[0] if self._rows else None

    def one(self) -> Mapping[str, object]:
        """Return the single scripted row."""
        [row] = self._rows
        return row

    def __iter__(self) -> Iterator[Mapping[str, object]]:
        """Iterate over the scripted rows."""
        return iter(self._rows)


class ReservationConnection:
    """Answers reservation statements from a script and records every statement it runs.

    `live` is what the reservation read returns, standing in for the rows the database
    clock has not expired; `consumable` is whether a consuming delete finds its row.
    """

    def __init__(self, *, live: Sequence[StagedObject] = (), consumable: bool = False) -> None:
        """Script the live reservations and the consuming delete's answer."""
        self.live = list(live)
        self.consumable = consumable
        self.statements: list[str] = []

    async def execute(
        self, statement: Executable, parameters: Mapping[str, int] | None = None
    ) -> RowsResult:
        """Record `statement` and answer it from the script."""
        sql = str(statement)
        self.statements.append(sql)
        if sql.startswith("SELECT staging_reservations.staging_key"):
            return RowsResult(
                [{"staging_key": str(obj.key), "attempt_id": obj.attempt_id} for obj in self.live]
            )
        if "sum(staging_reservations.reserved_bytes)" in sql:
            return RowsResult([{"total": sum(obj.size_bytes for obj in self.live)}])
        if sql.startswith("DELETE FROM staging_reservations") and "RETURNING" in sql:
            return RowsResult([{"attempt_id": uuid4()}] if self.consumable else [])
        return RowsResult([])

    def reservation_statements(self) -> list[str]:
        """Return the statements that touched the reservation table, in order."""
        return [sql for sql in self.statements if "staging_reservations" in sql]


def reservation_index(connection: ReservationConnection) -> PostgresStagingCacheIndex:
    """Return an index whose every transaction runs on `connection`."""
    return PostgresStagingCacheIndex(cast(AsyncEngine, ScriptedEngine(connection)))


async def test_consuming_an_absent_or_expired_reservation_reports_false() -> None:
    """No row deleted means nothing protected the bytes, so nothing may be registered."""
    index = reservation_index(ReservationConnection(consumable=False))
    assert await index.consume_reservation(uuid4()) is False


async def test_consuming_a_live_reservation_reports_true() -> None:
    """The row that went is the proof that the bytes were protected until now."""
    index = reservation_index(ReservationConnection(consumable=True))
    assert await index.consume_reservation(uuid4()) is True


async def test_consumption_removes_only_a_reservation_the_database_clock_keeps_live() -> None:
    """An expired reservation's bytes may already be collected; consuming it would hide that."""
    connection = ReservationConnection()

    await reservation_index(connection).consume_reservation(uuid4())

    [consume] = connection.reservation_statements()
    assert consume.startswith("DELETE FROM staging_reservations")
    assert "staging_reservations.expires_at > now()" in consume


async def test_a_reservation_expires_by_the_database_clock() -> None:
    """The expiry is written from the server's clock at the insert, never a pod's clock."""
    connection = ReservationConnection()

    await reservation_index(connection).reserve(key_for(1), uuid4(), 1024, timedelta(minutes=10))

    [reserve] = connection.reservation_statements()
    assert reserve.startswith("INSERT INTO staging_reservations")
    assert "(clock_timestamp() + " in reserve
    assert "now()" not in reserve


async def test_forgetting_removes_a_reservation_whether_or_not_it_expired() -> None:
    """Abandonment clears the row either way; an expired leftover would only wait for reconcile."""
    connection = ReservationConnection()

    await reservation_index(connection).forget_reservation(uuid4())

    [forget] = connection.reservation_statements()
    assert forget.startswith("DELETE FROM staging_reservations")
    assert "expires_at" not in forget


async def test_live_reserved_bytes_are_totalled_by_the_database_clock() -> None:
    """Admission counts only reservations the server still considers live."""
    connection = ReservationConnection(live=[staged(key_for(1), size=7), staged(key_for(2))])

    assert await reservation_index(connection).total_reserved_bytes() == 1031

    [total] = connection.reservation_statements()
    assert "FROM staging_reservations" in total
    assert "staging_reservations.expires_at > now()" in total


async def test_a_fresh_forget_reports_an_abandon_holding() -> None:
    """Abandonment outside any holding is its own, labelled, demand for the lock."""
    index = reservation_index(ReservationConnection())

    with capture_logs() as events:
        await index.forget_reservation(uuid4())

    assert [(e["caller"], e["entry_claims"]) for e in held_events(events)] == [
        ("occupancy.abandon", 0)
    ]


async def test_reservation_writes_join_the_holding_their_caller_has() -> None:
    """Admission's reservation and registration's consumption commit with their holdings."""
    index = reservation_index(ReservationConnection(consumable=True))

    with capture_logs() as events:
        async with index.writer_lock("occupancy.reserve"):
            await index.reserve(key_for(1), uuid4(), 1024, timedelta(minutes=10))
        async with index.writer_lock("occupancy.register"):
            assert await index.consume_reservation(uuid4()) is True
            await index.register(staged(key_for(1)))
        async with index.writer_lock("occupancy.abandon"):
            await index.forget_reservation(uuid4())

    assert [e["caller"] for e in held_events(events)] == [
        "occupancy.reserve",
        "occupancy.register",
        "occupancy.abandon",
    ]


async def test_reconciliation_expires_dead_reservations_before_reading_what_to_spare() -> None:
    """An expired reservation's headroom returns, and its object is no longer spared."""
    connection = ReservationConnection()

    await reservation_index(connection).reconcile(FakeObjectStore())

    expire, read = connection.reservation_statements()
    assert expire.startswith("DELETE FROM staging_reservations")
    assert "staging_reservations.expires_at <= now()" in expire
    assert read.startswith("SELECT staging_reservations.staging_key")
    assert "staging_reservations.expires_at > now()" in read


async def test_reconciliation_spares_an_object_under_a_live_reservation() -> None:
    """Another pod's completed upload awaiting registration survives the sweep."""
    reserved = staged(key_for(1))
    orphan = staged(key_for(2))
    store = FakeObjectStore(reserved.object_key, orphan.object_key)

    deleted = await reservation_index(ReservationConnection(live=[reserved])).reconcile(store)

    assert deleted == 1
    assert store.deleted == [orphan.object_key]


async def test_reconciliation_collects_an_object_whose_reservation_expired() -> None:
    """Once the database clock drops the reservation, the attempt's bytes are an orphan."""
    expired = staged(key_for(1))
    store = FakeObjectStore(expired.object_key)

    deleted = await reservation_index(ReservationConnection(live=[])).reconcile(store)

    assert deleted == 1
    assert store.deleted == [expired.object_key]
