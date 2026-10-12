"""The PostgreSQL cache index over staged objects, and the locks that coordinate it.

Coordination is PostgreSQL transaction advisory locks, so every guarantee here survives a
pod dying mid-operation: the lock dies with the backend that held it and an uncommitted
transaction leaves nothing behind, which is what makes every operation in this module
safe to simply repeat. A global writer lock serializes admission, publication and
eviction; per-entry shared locks protect downloads from the eviction that would otherwise
delete bytes a reader is still streaming.

Registration is immutable. Two writers racing one staging key both upload — to distinct
attempt names — and exactly one row is written; the loser's bytes are an orphan for
reconciliation to collect, never an overwrite of the published winner. An upload landing
after its writer lost the database is the same case, arriving later.

The two lock classes are coupled, deliberately and widely: an entry's exclusive lock is
transaction-scoped, and the writer transaction outlives everything a caller does inside
`writer_lock()` — including the upload `occupancy.reserve()` admits. A reader whose key
reclamation merely touched therefore waits on `hold_for_download`'s shared lock until
that transaction ends, and the four-byte entry lock space means a key that only collides
with a reclaimed one waits with it. The wait is bounded by the holder's transfer timeout
rather than by a `lock_timeout`, because narrowing it means narrowing the writer lock
itself, which is the decision recorded in `occupancy`'s module docstring.
"""

import asyncio
import hashlib
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Collection, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from typing import Final

from sqlalchemy import BigInteger, cast, delete, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from curate_d.ledger import mapping
from curate_d.ledger.tables import staged_objects
from curate_d.observability import get_logger
from curate_d.staging.errors import StagingObjectStoreError
from curate_d.staging.keys import StagingKey
from curate_d.staging.objects import StagedObject, StagingObjectStore

# Advisory lock classes, kept distinct so an entry whose key hashes to the writer lock's
# object identifier cannot collide with it and block every writer in the cluster.
_WRITER_LOCK_CLASS: Final = 0x63647377  # b"cdsw"
_ENTRY_LOCK_CLASS: Final = 0x63647365  # b"cdse"

_WRITER_LOCK_PARAMS: Final[dict[str, int]] = {
    "lock_class": _WRITER_LOCK_CLASS,
    "lock_object": 0,
}

_ACQUIRE_WRITER = text("SELECT pg_advisory_xact_lock(:lock_class, :lock_object)")
_ACQUIRE_ENTRY_SHARED = text("SELECT pg_advisory_xact_lock_shared(:lock_class, :lock_object)")
_TRY_ENTRY_EXCLUSIVE = text("SELECT pg_try_advisory_xact_lock(:lock_class, :lock_object)")

type EntryClaim = Callable[[StagingKey], Awaitable[bool]]

logger = get_logger(__name__)

# The indexes whose writer lock the current execution context already holds. Kept in a
# context variable rather than compared against `asyncio.current_task()` because a task
# started inside the lock inherits the context but not the identity, and would otherwise
# wait on a lock its own parent holds while the parent waits on it.
_WRITER_HOLDERS: Final[ContextVar[frozenset[object]]] = ContextVar(
    "curate_d_staging_writer_holders", default=frozenset()
)


@contextmanager
def _failures_translated(action: str) -> Iterator[None]:
    """Re-raise any database failure during `action` as a typed staging failure.

    A lost connection, a rolled-back transaction and a violated constraint all arrive
    here, and all mean the same thing to a caller: the index was not changed, so the
    operation can be repeated.
    """
    try:
        yield
    except SQLAlchemyError as error:
        raise StagingObjectStoreError(f"could not {action}: {error}") from error


def entry_lock_id(key: StagingKey) -> int:
    """Return the advisory-lock object identifier for one staging key.

    Hashed rather than sliced out of the key so that any key shape maps into the lock
    space, and hashed with a fixed function rather than `hash()`, whose per-process salt
    would put two pods on different locks for the same entry. A collision costs an
    unnecessary eviction skip, never a lost protection.
    """
    digest = hashlib.blake2b(key.encode(), digest_size=4).digest()
    return int.from_bytes(digest, "big", signed=True)


def staging_key_of(object_key: str) -> StagingKey:
    """Return the staging key a remote object name belongs to.

    Orphans have no registration to read a key from, so cleanup recovers it from the
    name in order to lock the entry before deleting.
    """
    return StagingKey(object_key.split("/", 1)[0])


def to_staged_object(row: mapping.Row) -> StagedObject:
    """Build a `StagedObject` from one `staged_objects` row."""
    return StagedObject(
        key=StagingKey(mapping.text(row, "staging_key")),
        attempt_id=mapping.identifier(row, "attempt_id"),
        size_bytes=mapping.number(row, "size_bytes"),
        sha256=mapping.text(row, "sha256"),
        created_at=mapping.moment(row, "created_at"),
    )


def registration_values(obj: StagedObject) -> mapping.ColumnValues:
    """Return the column values registering `obj` as its key's winner."""
    return {
        "staging_key": str(obj.key),
        "attempt_id": obj.attempt_id,
        "size_bytes": obj.size_bytes,
        "sha256": obj.sha256,
        "created_at": obj.created_at,
    }


async def delete_orphans(
    store: StagingObjectStore, registered: Collection[str], *, claim: EntryClaim
) -> int:
    """Delete completed objects no registration refers to, returning how many went.

    Args:
        store: Object store to list and delete through.
        registered: Remote object keys that are currently registered. Never deleted.
        claim: Takes an entry's exclusive lock, returning False when a download holds it.
            A released entry can still be under an active read, and that read is the one
            case the writer lock does not already cover.

    The listing and every delete it provokes share one batched connection: a sweep that
    collects thousands of orphans would otherwise pay a connect and a handshake per
    delete, all of it while the writer lock is held.

    Returns:
        The number of objects deleted.

    Raises:
        StagingObjectStoreError: Listing or deleting failed. Cleanup stops rather than
            continuing against a store whose answers it no longer trusts.
    """
    deleted = 0
    async with store.batch():
        async for candidate in store.list_objects():
            if candidate.object_key in registered:
                continue
            if not await claim(staging_key_of(candidate.object_key)):
                continue
            await store.delete(candidate.object_key)
            deleted += 1
    return deleted


async def registration_of(connection: AsyncConnection, key: StagingKey) -> StagedObject | None:
    """Read `key`'s published winner on an existing connection, or None if unregistered."""
    result = await connection.execute(
        select(staged_objects).where(staged_objects.c.staging_key == key)
    )
    row = result.mappings().one_or_none()
    return None if row is None else to_staged_object(row)


async def _registered_object_keys(connection: AsyncConnection) -> set[str]:
    """Read every published object's remote name, for cleanup to diff a listing against."""
    result = await connection.execute(
        select(staged_objects.c.staging_key, staged_objects.c.attempt_id)
    )
    return {
        f"{mapping.text(row, 'staging_key')}/{mapping.identifier(row, 'attempt_id').hex}"
        for row in result.mappings()
    }


async def _claim_entry(connection: AsyncConnection, key: StagingKey) -> bool:
    """Try `key`'s exclusive entry lock, returning False when a download holds it."""
    result = await connection.execute(
        _TRY_ENTRY_EXCLUSIVE,
        {"lock_class": _ENTRY_LOCK_CLASS, "lock_object": entry_lock_id(key)},
    )
    return result.scalar_one() is True


class PostgresStagingCacheIndex:
    """The registered-winner record, coordinated by PostgreSQL advisory locks.

    Built on an engine rather than a connection: the locks are the point, and a lock that
    coordinates pods has to be held by a transaction of its own rather than borrowed from
    whatever transaction a caller happens to be in. Ledger writes still belong to the DBOS
    datasource; this index is retry-safe operational state and deliberately separate.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        """Wrap an engine; every operation takes its own connection from its pool."""
        self._engine = engine
        self._writer_gate = asyncio.Lock()
        self._writer: AsyncConnection | None = None
        # The entry lock ids the current holding has taken, for its timing event.
        self._claimed: set[int] = set()

    @asynccontextmanager
    async def writer_lock(self, caller: str) -> AsyncGenerator[AsyncConnection, None]:
        """Hold the global writer lock, yielding the connection that owns it.

        Serializes admission, publication, eviction and cleanup across pods. Re-entrant
        for the context already holding it — the holding coroutine and every task it
        starts — because an admission path that published through a second connection
        would queue behind a lock its own session owns and wait there forever. Work done
        on the yielded connection commits when the block exits, so a caller that fails
        part way registers nothing.

        A fresh acquisition logs `staging.writer_lock_held` once the transaction that owns
        the lock has ended, with the wait to acquire it, the time it was held and
        `entry_claims`: the number of distinct entry locks the holding took, every one of
        them held until the holding ended. A re-entrant acquisition logs nothing: its time
        and its claims are already inside the outer holding.

        Args:
            caller: Label for the operation taking the lock, carried on the event so
                demand for the lock can be read per operation.

        Yields:
            The connection that owns the lock. Not safe for concurrent use: re-entrant
            holders share this one connection and must issue their statements in turn.
        """
        holders = _WRITER_HOLDERS.get()
        if self._writer is not None and self in holders:
            yield self._writer
            return
        requested = time.monotonic()
        acquired: float | None = None
        claimed: set[int] = set()
        try:
            async with self._writer_gate:
                with _failures_translated("take the staging writer lock"):
                    async with self._engine.begin() as connection:
                        await connection.execute(_ACQUIRE_WRITER, _WRITER_LOCK_PARAMS)
                        acquired = time.monotonic()
                        self._writer = connection
                        self._claimed = claimed
                        token = _WRITER_HOLDERS.set(holders | {self})
                        try:
                            yield connection
                        finally:
                            _WRITER_HOLDERS.reset(token)
                            self._writer = None
                            self._claimed = set()
        finally:
            # After the transaction ends, because that is when PostgreSQL lets go.
            if acquired is not None:
                logger.info(
                    "staging.writer_lock_held",
                    caller=caller,
                    wait_seconds=acquired - requested,
                    held_seconds=time.monotonic() - acquired,
                    entry_claims=len(claimed),
                )

    async def _claim(self, connection: AsyncConnection, key: StagingKey) -> bool:
        """Try `key`'s exclusive entry lock, counting it against the current holding.

        Counted by lock id rather than by key, because the id is what blocks a reader: a
        key claimed twice, or two keys colliding on one id, hold one lock. A claim on a
        key that turns out to be unregistered counts too, since the lock stays held all
        the same — reclaiming an orphan blocks readers of its key's registered winner.
        """
        if not await _claim_entry(connection, key):
            return False
        self._claimed.add(entry_lock_id(key))
        return True

    @asynccontextmanager
    async def _current_connection(self) -> AsyncGenerator[AsyncConnection, None]:
        """Yield the writer connection when this context holds it, else a fresh one.

        A read issued on a second connection cannot see the writer transaction's
        uncommitted work, so an accounting total taken during reclamation would still
        count the rows eviction has just deleted.
        """
        if self._writer is not None and self in _WRITER_HOLDERS.get():
            yield self._writer
            return
        async with self._engine.connect() as connection:
            yield connection

    @asynccontextmanager
    async def hold_for_download(self, key: StagingKey) -> AsyncGenerator[StagedObject | None, None]:
        """Hold `key`'s shared reader lock and yield the registration it protects.

        The lock is taken before the row is read: the other order lets eviction slip in
        between and delete bytes the caller is about to stream. Yields None when nothing
        is registered, which is an ordinary cache miss and not an error.

        The block holds a pooled connection and an open transaction for as long as the
        caller keeps it, and can wait on the shared lock for as long as a reclaiming
        writer's transaction lasts. Callers keep the block to the remote read: work on
        bytes that already landed locally needs no protection from eviction and must not
        be done inside it.

        Raises:
            StagingObjectStoreError: The lock or the read did not complete.
        """
        with _failures_translated(f"hold staging key {key} for download"):
            async with self._engine.begin() as connection:
                requested = time.monotonic()
                await connection.execute(
                    _ACQUIRE_ENTRY_SHARED,
                    {"lock_class": _ENTRY_LOCK_CLASS, "lock_object": entry_lock_id(key)},
                )
                waited = time.monotonic() - requested
                registration = await registration_of(connection, key)
                logger.info(
                    "staging.reader_lock_wait",
                    staging_key=str(key),
                    wait_seconds=waited,
                    registered=registration is not None,
                )
                yield registration

    async def register(self, obj: StagedObject) -> None:
        """Publish `obj` as the winner for its key, if no winner is registered yet.

        Called only after a verified upload. A key that already has a winner keeps it:
        the conflict is expected traffic, not an error, so a late or slow writer finds
        its work superseded and its bytes left for cleanup rather than displacing what
        readers are already using.

        Raises:
            StagingObjectStoreError: The registration could not be committed. Nothing was
                written, so the call can be repeated once the cause clears.
        """
        async with self.writer_lock("index.register") as connection:
            with _failures_translated(f"register staging key {obj.key}"):
                statement = insert(staged_objects).values(**registration_values(obj))
                # Conflict on the key alone. An attempt identifier colliding across two
                # keys is a bug rather than a race, and must not be swallowed as one.
                await connection.execute(
                    statement.on_conflict_do_nothing(index_elements=["staging_key"])
                )

    async def lookup(self, key: StagingKey) -> StagedObject | None:
        """Return the currently published object for `key`, or None if nothing is registered.

        Takes no lock: the answer is a snapshot either way. A caller about to read the
        bytes wants `hold_for_download`, which keeps the answer true for the download.

        Raises:
            StagingObjectStoreError: The lookup did not complete.
        """
        with _failures_translated(f"look up staging key {key}"):
            async with self._engine.connect() as connection:
                return await registration_of(connection, key)

    async def release(self, key: StagingKey) -> None:
        """Drop `key`'s registration so its object becomes eligible for deletion.

        Deletes nothing remotely. The object becomes an orphan that `reconcile` collects
        under the writer lock and an entry claim, so a reader mid-download never races the
        delete. Safe to repeat: releasing an unregistered key is a no-op.

        Raises:
            StagingObjectStoreError: The release could not be committed.
        """
        async with self.writer_lock("index.release") as connection:
            with _failures_translated(f"release staging key {key}"):
                await connection.execute(
                    delete(staged_objects).where(staged_objects.c.staging_key == key)
                )

    async def total_registered_bytes(self) -> int:
        """Return the exact byte total across every registered winner.

        Admission subtracts this from the budget, so it is summed in the database rather
        than estimated from a listing; an approximation here admits work that does not fit.
        Read on the writer connection when the caller holds the writer lock, so a total
        taken after eviction counts what that transaction has already removed.

        Raises:
            StagingObjectStoreError: The total could not be read.
        """
        total = cast(func.coalesce(func.sum(staged_objects.c.size_bytes), 0), BigInteger)
        with _failures_translated("total the registered staging bytes"):
            async with self._current_connection() as connection:
                result = await connection.execute(select(total.label("total")))
                return mapping.number(result.mappings().one(), "total")

    async def evict(self, key: StagingKey) -> StagedObject | None:
        """Unregister `key` unless a download holds it, returning what it published.

        Returns None both when nothing is registered and when a reader holds the entry.
        The distinction does not change what an evictor does next — try another candidate
        — and collapsing it keeps eviction from treating a live download as an error.

        Raises:
            StagingObjectStoreError: The eviction could not be committed.
        """
        async with self.writer_lock("index.evict") as connection:
            with _failures_translated(f"evict staging key {key}"):
                if not await self._claim(connection, key):
                    return None
                registration = await registration_of(connection, key)
                if registration is None:
                    return None
                await connection.execute(
                    delete(staged_objects).where(staged_objects.c.staging_key == key)
                )
                return registration

    async def reconcile(self, store: StagingObjectStore) -> int:
        """Delete every completed remote object with no registration, returning the count.

        Runs under the writer lock, so no admission or publication is in flight and a
        listed object without a row is settled rather than in progress: a losing writer's
        attempt, or an upload that landed after its writer lost the database.

        That premise holds only while every upload happens inside `occupancy.reserve()`'s
        holding of the same lock. `register()` takes the lock for the row alone, so an
        upload performed outside a reservation leaves a completed object with no row for
        the duration of the gap, and a sweep on another pod is entitled to collect it —
        after which the writer commits a row naming bytes that no longer exist, which
        nothing repairs. The rule is a contract on the caller today; the caller that
        performs uploads should be given the reservation rather than asked to remember it.

        Raises:
            StagingObjectStoreError: Listing, locking or deleting failed part way. What
                was already deleted stays deleted; the sweep is safe to run again.
        """
        async with self.writer_lock("index.reconcile") as connection:
            with _failures_translated("list the registered staging objects"):
                registered = await _registered_object_keys(connection)

            async def claim(key: StagingKey) -> bool:
                with _failures_translated(f"claim staging key {key} for cleanup"):
                    return await self._claim(connection, key)

            return await delete_orphans(store, registered, claim=claim)
