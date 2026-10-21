"""Publishing one source object's bytes into staging.

The writer-side operation the rest of the pipeline calls. Admission, upload and
registration happen inside one `occupancy.reserve()`: admission records a reservation in a
short writer-lock holding, the upload runs under that reservation with no lock held, and
the reservation's `register()` consumes it and writes the row in a second holding. Every
upload has to happen inside a reservation, or a sweep on another pod may collect the
object before its row commits.

SHA-1 is computed in the same read pass in which the object store computes SHA-256. pCloud
reports SHA-1 in both regions (SHA-256 only in the EU), so the SHA-1 is what binds the
staged bytes to the file the scan listed, whichever region the account is in.

A registration is immutable, so staging a new version of a file first releases the old
one. Otherwise the new upload would lose the registration race to its own predecessor and
readers would go on receiving the old bytes under a key that now means the new ones.

Each reservation logs one `staging.reservation_phases` event splitting its lifetime into
admission, upload, registration and commit (see `docs/staging.md`).
"""

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import BinaryIO, Literal
from uuid import uuid4

from curate_d.observability import get_logger
from curate_d.staging.errors import StagingCapacityExhaustedError, StagingIntegrityError
from curate_d.staging.factories import StagingWriterBackend
from curate_d.staging.keys import StagingKey
from curate_d.staging.objects import StagedObject
from curate_d.staging.occupancy import Reservation

type ReservationOutcome = Literal["registered", "refused", "cancelled", "failed"]

logger = get_logger(__name__)


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


@dataclass
class _ReservationPhases:
    """Monotonic marks across one reservation, reported as `staging.reservation_phases`.

    A mark is None until its phase ends. A phase that a failure or a cancellation cut
    short runs to the moment the reservation ended, rollback included, so the phases of
    any reservation that was admitted add up to its lifetime after the wait for the
    admission holding. Registration runs from the upload's end to the row's insert, the
    wait for the registration holding included; commit from the insert to that holding's
    end. The insert's mark is the reservation's own, taken inside the holding.
    """

    key: StagingKey
    declared_bytes: int
    reservation: Reservation | None = None
    entered: float | None = None
    uploaded: float | None = None
    committed: float | None = None

    def admit(self, reservation: Reservation) -> None:
        """Record the reservation admission yielded and start the upload phase."""
        self.reservation = reservation
        self.entered = time.monotonic()

    def report(self, outcome: ReservationOutcome, error: BaseException | None = None) -> None:
        """Log the reservation's phases and how it ended."""
        ended = time.monotonic()

        def span(start: float | None, end: float | None) -> float | None:
            """Return a phase's duration, None when it never started."""
            return None if start is None else (ended if end is None else end) - start

        admission = None if self.reservation is None else self.reservation.admission
        registered = None if self.reservation is None else self.reservation.registered_at
        reclaimed = None if admission is None else admission.reclaimed
        logger.info(
            "staging.reservation_phases",
            staging_key=str(self.key),
            declared_bytes=self.declared_bytes,
            outcome=outcome,
            error=None if error is None else type(error).__name__,
            admission_seconds=None if admission is None else admission.admission_seconds,
            upload_seconds=span(self.entered, self.uploaded),
            registration_seconds=span(self.uploaded, registered),
            commit_seconds=span(registered, self.committed),
            evicted=None if reclaimed is None else len(reclaimed.evicted),
            protected=None if reclaimed is None else len(reclaimed.protected),
            orphans_deleted=None if reclaimed is None else reclaimed.orphans_deleted,
        )


def _outcome_of(error: BaseException, *, admitted: bool) -> ReservationOutcome:
    """Classify how a reservation that raised `error` ended."""
    if isinstance(error, asyncio.CancelledError):
        return "cancelled"
    if isinstance(error, StagingCapacityExhaustedError) and not admitted:
        return "refused"
    return "failed"


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
        StagingReservationLostError: the upload outlived its reservation, so nothing
            was published; a later attempt re-uploads under a fresh one.
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
    phases = _ReservationPhases(key=key, declared_bytes=size_bytes)
    try:
        async with backend.occupancy.reserve(key, target.attempt_id, size_bytes) as reservation:
            phases.admit(reservation)
            staged = await backend.store.upload(
                target,
                reader,  # type: ignore[arg-type]  # only read() is used, which Sha1Reader has
                declared_max_bytes=size_bytes,
            )
            phases.uploaded = time.monotonic()
            await reservation.register(staged)
        phases.committed = time.monotonic()
    except BaseException as error:
        phases.report(_outcome_of(error, admitted=phases.entered is not None), error)
        raise
    phases.report("registered")
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
