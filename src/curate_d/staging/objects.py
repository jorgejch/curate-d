"""Typed contracts for S3-backed staging.

The contracts separate storage from coordination. `StagingObjectStore` is the remote
bytes and nothing else; `StagingCacheIndex` is the PostgreSQL record of which upload
attempt currently owns a key. Neither knows about the other, because the ordering
between them is the correctness property: an object is uploaded and verified before it
is registered, and a registration commits only for bytes that already exist remotely.

`StagingDownloadStore` exposes only verified reads to CPU snapshot readers.

The protocols exist so the coordination logic can be tested against fakes without an
object store, and so the SDK's exception and response surface stops at one adapter
rather than spreading into the cache logic.
"""

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import BinaryIO, Protocol, runtime_checkable
from uuid import UUID

from curate_d.staging.keys import StagingKey


@dataclass(frozen=True)
class StagedObject:
    """One completed upload attempt, as the object store holds it.

    The attempt identifier is part of the remote key rather than a column alone. Two
    writers racing one staging key therefore write two distinct objects, and the loser's
    bytes are an orphan to reconcile rather than an overwrite of the winner.
    """

    key: StagingKey
    attempt_id: UUID
    size_bytes: int
    sha256: str
    created_at: datetime

    @property
    def object_key(self) -> str:
        """The remote key for this attempt, relative to the configured content prefix."""
        return f"{self.key}/{self.attempt_id.hex}"


@dataclass(frozen=True)
class StagedObjectRef:
    """A remote object identified well enough to fetch or delete it.

    Carries no digest: used for orphans and reconciliation candidates, which by
    definition have no registration to compare against.
    """

    object_key: str
    size_bytes: int
    last_modified: datetime


@runtime_checkable
class StagingDownloadStore(Protocol):
    """Expose verified downloads without mutation or listing authority."""

    async def download(self, obj: StagedObject, destination: BinaryIO) -> None:
        """Stream and verify the registered object into the destination."""
        ...


@runtime_checkable
class StagingObjectStore(Protocol):
    """Remote object operations staging depends on.

    Deliberately narrow. There is no create-bucket or put-policy method, because runtime
    workers must not hold that authority and an interface that cannot express the call
    cannot accidentally make it.
    """

    async def upload(
        self, target: StagedObject, source: BinaryIO, *, declared_max_bytes: int
    ) -> StagedObject:
        """Upload `source` under `target`'s attempt key and verify what landed.

        Args:
            target: Key, attempt identifier, and expected metadata for this attempt.
            source: Bytes to upload. Read once, streamed, never held whole in memory.
            declared_max_bytes: Upper bound admission already reserved. A stream that
                exceeds it fails rather than silently consuming unreserved capacity.

        Returns:
            The object as verified after upload, with the computed size and digest.

        Raises:
            StagingObjectStoreError: The upload or its verification did not complete.
            StagingIntegrityError: What landed remotely does not match what was sent.
        """
        ...

    async def download(self, obj: StagedObject, destination: BinaryIO) -> None:
        """Stream `obj` into `destination`, verifying size and digest before returning.

        Raises:
            StagedEntryMissingError: The object is absent; its registration is stale.
            StagingAccessDeniedError: The identity may not read it.
            StagingIntegrityError: The retrieved bytes do not match the registration,
                including while they are still arriving: an object that outgrows its
                registration is refused before it can fill the caller's scratch.
            OSError: `destination` could not be written. Passed through untranslated,
                because only the caller knows what a failed write to its own scratch
                means; implementations must not relabel it as a store failure.
        """
        ...

    async def delete(self, object_key: str) -> None:
        """Delete one object, treating an already-absent object as success.

        Raises:
            StagingObjectStoreError: Deletion failed for any reason other than absence.
        """
        ...

    def list_objects(self) -> AsyncIterator[StagedObjectRef]:
        """Yield every completed object under the content prefix, paginating.

        Occupancy accounting consumes this, so a truncated listing would understate usage
        and admit work that does not fit. Pagination is the implementation's problem, not
        the caller's.
        """
        ...

    async def abort_incomplete_uploads(self, older_than: datetime) -> int:
        """Abort multipart uploads started before `older_than`, returning the count.

        The explicit half of residual cleanup. The backend-specific lifecycle or server
        backstop is the other half, and neither is assumed to cover the other.
        """
        ...

    def batch(self) -> AbstractAsyncContextManager[None]:
        """Scope the metadata operations inside it so they may share one connection.

        An efficiency scope and nothing else: reclamation issues thousands of serial
        deletes under the global writer lock, and a connection per delete is a connect, a
        handshake and a credential setup each time. Callers get the same results either
        way, so an implementation with nothing to share may make this a no-op.

        Transfers are deliberately excluded: `upload` and `download` are free to open
        their own connection inside a batch, so none outlives the transfer that needed it.
        """
        ...


@runtime_checkable
class StagingCacheIndex(Protocol):
    """The registered-winner record and the locks that keep it consistent.

    Backed by PostgreSQL rather than by the object store's own listing: a listing cannot
    express "this attempt is the published one" atomically with the readers that depend
    on it, which is the whole coordination problem.
    """

    async def register(self, obj: StagedObject) -> None:
        """Publish `obj` as the winner for its key, if no winner is registered yet.

        Called only after a verified upload. Registering a key that already has a winner
        leaves the existing registration untouched, so a late writer cannot displace it.

        Raises:
            StagingObjectStoreError: The registration could not be committed.
        """
        ...

    async def lookup(self, key: StagingKey) -> StagedObject | None:
        """The currently published object for `key`, or None if nothing is registered."""
        ...

    async def release(self, key: StagingKey) -> None:
        """Drop `key`'s registration so its object becomes eligible for deletion.

        Does not delete remotely: the object becomes an orphan that reconciliation
        collects while holding the writer lock, so a reader mid-download is never
        racing a delete.
        """
        ...

    async def total_registered_bytes(self) -> int:
        """Bytes across every registered winner, for admission accounting."""
        ...
