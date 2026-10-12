"""Verified seekable local snapshots of staged objects, for the decoding profile.

A decoder needs to seek. S3 hands out a stream, so a registered object is materialized
into worker-local scratch first and the decoder reads that file. The snapshot is verified
against its registration — size and SHA-256 — before any handle is exposed, because a
handle returned first and checked afterwards has already fed attacker-influenced bytes to
a parser by the time the mismatch is noticed.

Two properties here are structural rather than conventional. The reader holds no provider
client and has no constructor parameter through which one could be supplied, so a missing
remote object raises instead of quietly becoming a re-fetch with a credential this process
was deliberately never given. And the snapshot is unlinked before its handle is yielded:
the decoder reads an inode nothing else can name, so remote release, eviction or another
reader cannot truncate it, and a process that dies mid-decode hands the bytes back to the
filesystem without needing a sweep.

Local scratch is finite, so every snapshot holds a byte reservation from admission until
its handle closes — not until its download finishes — and that bound covers every
concurrent reader in the process.
"""

import errno
import hashlib
import threading
from collections.abc import AsyncGenerator, Iterator
from contextlib import (
    AbstractAsyncContextManager,
    ExitStack,
    asynccontextmanager,
    contextmanager,
)
from pathlib import Path
from typing import BinaryIO, Final, Protocol, runtime_checkable
from uuid import uuid4

from curate_d.staging.errors import (
    StagedContentUnreadableError,
    StagedEntryMissingError,
    StagedEntrySupersededError,
    StagingCapacityExhaustedError,
    StagingCapacityNeverFitsError,
    StagingIntegrityError,
)
from curate_d.staging.keys import StagingKey, require_within_root
from curate_d.staging.objects import StagedObject, StagingDownloadStore

# Verification re-reads the snapshot in blocks so peak memory is a property of this
# constant rather than of the media being decoded.
_VERIFY_BLOCK: Final = 1024 * 1024

# A full or over-quota volume is capacity exhaustion, not unreadable content. The
# reservation bounds what this process asked for; the node's disk can still run out
# underneath it, and the two deserve the same name so a caller retries the same way.
_EXHAUSTED_ERRNOS: Final = frozenset({errno.ENOSPC, errno.EDQUOT, errno.EFBIG})


@runtime_checkable
class StagingDownloadHold(Protocol):
    """The one cache-index operation the read path is allowed to reach for.

    Narrow on purpose. A reader that could register, release or evict would be a reader
    holding write authority over the cache, and the profile split exists to make that
    unrepresentable rather than merely discouraged.
    """

    def hold_for_download(
        self, key: StagingKey
    ) -> AbstractAsyncContextManager[StagedObject | None]:
        """Hold `key`'s shared reader lock and yield the registration it protects.

        Yields None when nothing is registered, which is an ordinary cache miss. The lock
        stops eviction deleting bytes the caller is about to stream.
        """
        ...


class LocalScratchBudget:
    """The process-wide bound on concurrent local snapshot bytes.

    Counts reservations rather than measuring the volume: a snapshot is unlinked while it
    is being read, so its bytes are invisible to any `statvfs` yet still occupied until
    the last handle closes. Admission is refused immediately instead of waiting for room,
    because a decode queued behind another decode's close is a stall nobody can see.
    """

    def __init__(self, max_bytes: int) -> None:
        """Bound concurrent snapshots at `max_bytes`.

        Raises:
            ValueError: `max_bytes` is not positive, which would admit nothing at all.
        """
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes
        self._reserved = 0
        # A plain lock rather than an asyncio one: the release runs from the handle's
        # cleanup, which must never need an event loop to complete.
        self._guard = threading.Lock()

    @property
    def max_bytes(self) -> int:
        """The configured ceiling on concurrent snapshot bytes."""
        return self._max_bytes

    @property
    def reserved_bytes(self) -> int:
        """Bytes currently held by open or in-flight snapshots."""
        with self._guard:
            return self._reserved

    @contextmanager
    def reserve(self, needed_bytes: int) -> Iterator[None]:
        """Hold `needed_bytes` of local scratch for the duration of the block.

        Raises:
            ValueError: `needed_bytes` is negative.
            StagingCapacityExhaustedError: The snapshot cannot fit, either ever or
                alongside the readers currently holding scratch.
        """
        if needed_bytes < 0:
            raise ValueError("needed_bytes must be nonnegative")
        self._take(needed_bytes)
        try:
            yield
        finally:
            # Every exit path, cancellation included: an unreleased reservation is a
            # permanent loss of capacity that no later run can account for.
            with self._guard:
                self._reserved -= needed_bytes

    def _take(self, needed_bytes: int) -> None:
        """Admit `needed_bytes` against the bound, or refuse explicitly.

        Raises:
            StagingCapacityExhaustedError: The bound cannot cover this snapshot.
        """
        with self._guard:
            if needed_bytes > self._max_bytes:
                raise StagingCapacityNeverFitsError(
                    f"a {needed_bytes}-byte snapshot never fits local scratch, which is "
                    f"bounded at {self._max_bytes} bytes; this job needs a larger bound "
                    f"rather than a retry"
                )
            if self._reserved + needed_bytes > self._max_bytes:
                raise StagingCapacityExhaustedError(
                    f"a {needed_bytes}-byte snapshot does not fit local scratch: "
                    f"{self._reserved} of {self._max_bytes} bytes are held by readers "
                    f"that have not closed their handles"
                )
            self._reserved += needed_bytes


class LocalSnapshotReader:
    """Read side of S3 staging: verified seekable snapshots and nothing else.

    Constructed with a staging object store, a cache-index hold, a scratch root and a
    byte budget. There is deliberately no provider, client or credential parameter, and
    the object store protocol it takes cannot express a source fetch: the absence is the
    guarantee, so a missing object raises rather than resolving itself.
    """

    def __init__(
        self,
        store: StagingDownloadStore,
        index: StagingDownloadHold,
        *,
        scratch_root: Path,
        budget: LocalScratchBudget,
    ) -> None:
        self._store = store
        self._index = index
        self._scratch_root = scratch_root
        self._budget = budget

    @asynccontextmanager
    async def open(
        self, key: StagingKey, *, expected_sha256: str | None = None
    ) -> AsyncGenerator[BinaryIO, None]:
        """Yield a verified seekable snapshot of `key`'s registered object.

        The handle is positioned at the start of a local file whose size and digest
        already match the registration. Closing it releases the scratch reservation.

        Args:
            key: The entry to read.
            expected_sha256: The version the caller was asked to read. When set, a
                registration naming other bytes is refused before anything is
                downloaded, rather than verified and handed over as if it were that
                version.

        Raises:
            StagedEntryMissingError: Nothing is registered for `key`, or its object is
                gone from the store. Never a re-fetch: this profile holds no provider
                credential and the cache entry has to be restaged by the I/O profile.
            StagedEntrySupersededError: `key` is registered with other bytes than
                `expected_sha256`.
            StagingIntegrityError: The snapshot's size or digest disagrees with the
                registration. No handle is returned and the scratch file is reclaimed.
            StagingCapacityExhaustedError: Local scratch cannot cover the snapshot.
            StagedContentUnreadableError: The snapshot could not be written or reopened.
            StagingObjectStoreError: The shared lock, the registration read or the
                transfer failed, including `StagingAccessDeniedError` and
                `StagingTransientError`.
        """
        with ExitStack() as scope:
            yield await self._acquire(key, scope, expected_sha256)

    async def _acquire(
        self, key: StagingKey, scope: ExitStack, expected_sha256: str | None = None
    ) -> BinaryIO:
        """Reserve and stream under the shared read lock, then verify outside it.

        The hold ends with the remote stream rather than with the snapshot. It exists to
        stop eviction deleting bytes mid-transfer, and once `download` returns there is
        nothing left for eviction to race: the bytes are on local scratch under a name
        no other reader can produce, and nothing outside this process can reach them.
        Re-hashing inside the hold would pin a pooled connection and an open ledger
        transaction for the length of a SHA-256 pass over the whole object, which is how
        a handful of concurrent snapshots exhaust the pool.

        `scope` receives the reservation before the handle, so unwinding closes the handle
        first and releases scratch second — the reservation outlives every read.
        """
        with ExitStack() as cleanup:
            async with self._index.hold_for_download(key) as registration:
                if registration is None:
                    raise StagedEntryMissingError(
                        f"staging key {key} has no registered object; the decoding profile "
                        f"holds no provider credential and cannot fetch it"
                    )
                if expected_sha256 is not None and registration.sha256 != expected_sha256:
                    raise StagedEntrySupersededError(
                        f"staging key {key} now holds {registration.sha256}, not the "
                        f"{expected_sha256} this read was for"
                    )
                scope.enter_context(self._budget.reserve(registration.size_bytes))
                path = await self._stream_to_scratch(registration, cleanup)
            handle = _open_verified(path, registration, cleanup)
        scope.callback(handle.close)
        return handle

    async def _stream_to_scratch(self, obj: StagedObject, cleanup: ExitStack) -> Path:
        """Stream `obj` to a private scratch file and return the path it landed on.

        The only step that needs the cache-index hold, and therefore the only step the
        caller keeps inside it. The bytes are unverified on return: the transfer has
        finished, nothing more.

        Args:
            cleanup: Receives the snapshot's removal, so an abandoned or rejected
                transfer leaves no file behind.

        Raises:
            StagingCapacityExhaustedError: The scratch volume is full or over quota.
            StagedContentUnreadableError: The snapshot could not be written.
        """
        with _local_failures(obj):
            self._scratch_root.mkdir(parents=True, exist_ok=True)
            path = self._scratch_path(obj)
            # Runs on success and on failure alike. On success the handle is already
            # open, so unlinking only removes the name: the bytes stay reachable through
            # that descriptor and through nothing else.
            cleanup.callback(path.unlink, missing_ok=True)
            with path.open("xb") as sink:
                await self._store.download(obj, sink)
            return path

    def _scratch_path(self, obj: StagedObject) -> Path:
        """Return a private scratch path for one snapshot, proven inside the root.

        Unique per snapshot rather than per key: two readers of one key each materialize
        their own bytes, so neither can observe the other's partial file nor lose its own
        to the other's cleanup.
        """
        name = f"{obj.key}.{uuid4().hex}.snapshot"
        return require_within_root(self._scratch_root, self._scratch_root / name)


def _open_verified(path: Path, obj: StagedObject, cleanup: ExitStack) -> BinaryIO:
    """Verify the snapshot at `path` and return an anonymous read handle to it.

    Every byte it reads is already local, so it runs outside the cache-index hold.

    Args:
        cleanup: Holds the snapshot's removal. Closed here on success so the returned
            handle names an unlinked inode.

    Raises:
        StagingIntegrityError: The local bytes do not match the registration.
        StagingCapacityExhaustedError: The scratch volume is full or over quota.
        StagedContentUnreadableError: The snapshot could not be reopened or unlinked.
    """
    with _local_failures(obj):
        _require_verified(path, obj)
        # No fsync anywhere: the snapshot is disposable and is about to lose its name,
        # so durability would cost a full write-back for bytes that must never be read
        # by a second process anyway.
        handle = path.open("rb")
        try:
            # Unlink here rather than leaving it to the unwind. The unwind runs after
            # this frame has returned its handle but before `_acquire` registers the
            # close, so an unlink that raises there would abandon a descriptor no caller
            # is left holding. Closing the stack now empties it, which makes the caller's
            # `with` exit a no-op.
            cleanup.close()
        except OSError:
            handle.close()
            raise
        return handle


def _require_verified(path: Path, obj: StagedObject) -> None:
    """Refuse a snapshot whose size or digest differs from its registration.

    Re-read from local disk rather than trusting the transfer's own accounting. The bytes
    a decoder will parse are the ones that landed here, and a short write is invisible to
    a digest computed on the way past.

    Raises:
        StagingIntegrityError: The snapshot is truncated, over-long, or not the content
            the registration names. Neither is retryable against the same object.
    """
    size = path.stat().st_size
    if size != obj.size_bytes:
        raise StagingIntegrityError(
            f"local snapshot of {obj.object_key} holds {size} bytes but its registration "
            f"declares {obj.size_bytes}; no decoder handle is returned for it"
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(_VERIFY_BLOCK):
            digest.update(block)
    computed = digest.hexdigest()
    if computed != obj.sha256:
        raise StagingIntegrityError(
            f"local snapshot of {obj.object_key} hashes to {computed} but is registered "
            f"as {obj.sha256}; the cache entry is invalid rather than the read"
        )


@contextmanager
def _local_failures(obj: StagedObject) -> Iterator[None]:
    """Translate scratch filesystem failures into named staging errors.

    Raises:
        StagingCapacityExhaustedError: The volume is full, over quota, or refuses the
            file's size.
        StagedContentUnreadableError: Any other local failure. Distinct from a missing
            entry: the object is registered and present, this process simply could not
            keep a copy of it.
    """
    try:
        yield
    except (ConnectionError, TimeoutError, InterruptedError):
        # A dropped or timed-out transfer is an OSError too, and it is the object store's
        # to name. Relabelling it as unreadable local content would send whoever reads the
        # failure to the wrong volume entirely.
        raise
    except OSError as error:
        if error.errno in _EXHAUSTED_ERRNOS:
            raise StagingCapacityExhaustedError(
                f"local scratch ran out of room for a snapshot of {obj.object_key}: {error}"
            ) from error
        raise StagedContentUnreadableError(
            f"a local snapshot of {obj.object_key} could not be written or reopened: {error}"
        ) from error
