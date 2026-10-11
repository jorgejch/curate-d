"""Publishing one source object's bytes into staging.

The writer-side operation the rest of the pipeline calls. Admission, upload and
registration happen inside one `occupancy.reserve()`, which holds the writer lock: the
cache index requires every upload to happen inside a reservation, or a sweep on another
pod may collect the object before its row commits.

SHA-1 is computed in the same read pass in which the object store computes SHA-256. pCloud
reports SHA-1 in both regions (SHA-256 only in the EU), so the SHA-1 is what binds the
staged bytes to the file the scan listed, whichever region the account is in.

A registration is immutable, so staging a new version of a file first releases the old
one. Otherwise the new upload would lose the registration race to its own predecessor and
readers would go on receiving the old bytes under a key that now means the new ones.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO
from uuid import uuid4

from curate_d.staging.errors import StagingIntegrityError
from curate_d.staging.factories import StagingWriterBackend
from curate_d.staging.keys import StagingKey
from curate_d.staging.objects import StagedObject


class Sha1Reader:
    """Wraps a binary source, hashing with SHA-1 whatever is read through it."""

    def __init__(self, source: BinaryIO) -> None:
        """Wrap `source`, which must be read only through this object from now on."""
        self._source = source
        self._digest = hashlib.sha1()
        self.bytes_read = 0

    def read(self, size: int = -1, /) -> bytes:
        """Read from the source and fold what was read into the digest."""
        block = self._source.read(size)
        self._digest.update(block)
        self.bytes_read += len(block)
        return block

    def hexdigest(self) -> str:
        """SHA-1 of everything read so far."""
        return self._digest.hexdigest()


@dataclass(frozen=True)
class StagedContent:
    """A published staging object together with the SHA-1 of the same bytes."""

    staged: StagedObject
    sha1: str


async def publish(
    backend: StagingWriterBackend, key: StagingKey, source: BinaryIO, *, size_bytes: int
) -> StagedContent:
    """Stage `size_bytes` from `source` under `key`, replacing any earlier version.

    Raises:
        StagingCapacityExhaustedError: staging cannot admit the object.
        StagingIntegrityError: the source did not deliver `size_bytes`, what landed does
            not match what was sent, or a concurrent writer published this key first.
        StagingObjectStoreError: the upload, registration or release failed.
    """
    await backend.occupancy.release(key)
    reader = Sha1Reader(source)
    target = StagedObject(
        key=key,
        attempt_id=uuid4(),
        size_bytes=size_bytes,
        sha256="",
        created_at=datetime.now(UTC),
    )
    async with backend.occupancy.reserve(size_bytes):
        staged = await backend.store.upload(
            target,
            reader,  # type: ignore[arg-type]  # only read() is used, which Sha1Reader has
            declared_max_bytes=size_bytes,
        )
        await backend.index.register(staged)
    # Looked up after the reservation ends, when the registration has committed; the
    # lookup reads on its own connection and would not see it any earlier.
    winner = await backend.index.lookup(key)
    if winner is None or winner.attempt_id != staged.attempt_id:
        # Another writer won the key between our release and our registration: another
        # version of the same file is being ingested. Its bytes are not ours to vouch for.
        raise StagingIntegrityError(
            f"staging key {key} was published by another attempt while this one uploaded; "
            f"the ingest that owns the current version supersedes this one"
        )
    return StagedContent(staged=staged, sha1=reader.hexdigest())
