"""In-memory staging, shared by a writer and a reader backend.

The store and index are fakes; `StagingOccupancy` and `LocalSnapshotReader` are the real
classes over them, so admission, publication and verified snapshots behave as they do in
production while the bytes live in a dict. The store verifies size exactly and computes
SHA-256 while reading, as the S3 store does.
"""

import asyncio
import hashlib
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from curate_d.staging.errors import StagedEntryMissingError, StagingIntegrityError
from curate_d.staging.factories import StagingReaderBackend, StagingWriterBackend
from curate_d.staging.keys import StagingKey
from curate_d.staging.objects import StagedObject, StagedObjectRef, StagingObjectStore
from curate_d.staging.occupancy import StagingOccupancy
from curate_d.staging.snapshots import LocalScratchBudget, LocalSnapshotReader

_BLOCK = 64 * 1024


class MemoryStore:
    """Staged bytes by object key.

    An `upload_gate` makes every upload wait for the event before it reads, with
    `uploading` set once it does, so a test can cancel an upload in flight.
    """

    def __init__(self) -> None:
        """Start empty."""
        self.objects: dict[str, tuple[StagedObject, bytes]] = {}
        self.uploads = 0
        self.upload_gate: asyncio.Event | None = None
        self.uploading = asyncio.Event()

    async def upload(
        self, target: StagedObject, source: BinaryIO, *, declared_max_bytes: int
    ) -> StagedObject:
        """Read the source to its end, enforcing the declared size, and keep the bytes."""
        self.uploading.set()
        if self.upload_gate is not None:
            await self.upload_gate.wait()
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        while block := source.read(_BLOCK):
            digest.update(block)
            chunks.append(block)
        content = b"".join(chunks)
        if len(content) != target.size_bytes or len(content) > declared_max_bytes:
            raise StagingIntegrityError(
                f"expected {target.size_bytes} bytes, source delivered {len(content)}"
            )
        staged = replace(target, sha256=digest.hexdigest(), created_at=datetime.now(UTC))
        self.objects[staged.object_key] = (staged, content)
        self.uploads += 1
        return staged

    async def download(self, obj: StagedObject, destination: BinaryIO) -> None:
        """Write the registered object's bytes."""
        entry = self.objects.get(obj.object_key)
        if entry is None:
            raise StagedEntryMissingError(f"{obj.object_key} is gone")
        destination.write(entry[1])

    async def delete(self, object_key: str) -> None:
        """Drop an object; absence is success."""
        self.objects.pop(object_key, None)

    async def list_objects(self) -> AsyncIterator[StagedObjectRef]:
        """Every object."""
        for staged, content in list(self.objects.values()):
            yield StagedObjectRef(
                object_key=staged.object_key,
                size_bytes=len(content),
                last_modified=staged.created_at,
            )

    async def abort_incomplete_uploads(self, older_than: datetime) -> int:
        """Nothing is ever left incomplete here."""
        return 0

    @asynccontextmanager
    async def batch(self) -> AsyncGenerator[None, None]:
        """No connection to share."""
        yield


class MemoryIndex:
    """Registrations by key, with the writer lock and holds the real index offers."""

    def __init__(self) -> None:
        """Start empty."""
        self.registrations: dict[StagingKey, StagedObject] = {}
        self.lock_callers: list[str] = []

    @asynccontextmanager
    async def writer_lock(self, caller: str) -> AsyncGenerator[object, None]:
        """A lock nothing contends for, recording who took it."""
        self.lock_callers.append(caller)
        yield self

    async def register(self, obj: StagedObject) -> None:
        """First writer wins, as in PostgreSQL."""
        self.registrations.setdefault(obj.key, obj)

    async def lookup(self, key: StagingKey) -> StagedObject | None:
        """The current registration."""
        return self.registrations.get(key)

    async def release(self, key: StagingKey) -> None:
        """Unregister."""
        self.registrations.pop(key, None)

    async def total_registered_bytes(self) -> int:
        """Sum of registered sizes."""
        return sum(obj.size_bytes for obj in self.registrations.values())

    async def evict(self, key: StagingKey) -> StagedObject | None:
        """Unregister, returning what was published."""
        return self.registrations.pop(key, None)

    async def reconcile(self, store: StagingObjectStore) -> int:
        """Delete unregistered objects."""
        registered = {obj.object_key for obj in self.registrations.values()}
        doomed = [ref.object_key async for ref in store.list_objects()]
        for object_key in doomed:
            if object_key not in registered:
                await store.delete(object_key)
        return len([k for k in doomed if k not in registered])

    def hold_for_download(
        self, key: StagingKey
    ) -> AbstractAsyncContextManager[StagedObject | None]:
        """Lend the registration for a download."""
        return self._hold(key)

    @asynccontextmanager
    async def _hold(self, key: StagingKey) -> AsyncGenerator[StagedObject | None, None]:
        """Yield the registration."""
        yield self.registrations.get(key)


class MemoryStaging:
    """One staging area, seen from both profiles."""

    def __init__(self, scratch_root: Path, *, max_bytes: int = 10 * 1024 * 1024) -> None:
        """Build both sides over shared memory; snapshots land under `scratch_root`."""
        self.store = MemoryStore()
        self.index = MemoryIndex()
        self.writer = StagingWriterBackend(
            store=self.store,
            index=self.index,
            occupancy=StagingOccupancy(
                self.store, self.index, max_bytes=max_bytes, max_age=timedelta(hours=1)
            ),
            upload_max_age=timedelta(hours=1),
        )
        budget = LocalScratchBudget(max_bytes)
        self.reader = StagingReaderBackend(
            reader=LocalSnapshotReader(
                self.store, self.index, scratch_root=scratch_root, budget=budget
            ),
            budget=budget,
        )

    def content(self, key: StagingKey) -> bytes | None:
        """The bytes currently registered under `key`."""
        registration = self.index.registrations.get(key)
        if registration is None:
            return None
        return self.store.objects[registration.object_key][1]
