"""Publishing a source's bytes: SHA-1 in the upload's own pass, and versions replaced."""

import asyncio
import hashlib
import io
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO
from uuid import uuid4

import pytest
from structlog.testing import capture_logs

from curate_d.staging import publish as publish_module
from curate_d.staging.errors import (
    StagingCapacityExhaustedError,
    StagingIntegrityError,
    StagingObjectStoreError,
)
from curate_d.staging.keys import StagingKey
from curate_d.staging.objects import StagedObject
from curate_d.staging.occupancy import OccupancyReport
from curate_d.staging.publish import Sha1Reader, publish
from tests.stubs.staging import MemoryStaging

_KEY = StagingKey("0" * 32)


class _CountingSource(io.BytesIO):
    """Counts full passes: every read that reaches the end."""

    def __init__(self, content: bytes) -> None:
        """Serve `content`."""
        super().__init__(content)
        self.ends = 0

    def read(self, size: int | None = -1, /) -> bytes:
        """Read, counting each time the end is reached."""
        block = super().read(size)
        if not block:
            self.ends += 1
        return block


@pytest.fixture
def staging(tmp_path: Path) -> MemoryStaging:
    """An empty staging area."""
    return MemoryStaging(tmp_path)


async def test_publishing_returns_sha1_and_sha256_of_one_pass(staging: MemoryStaging) -> None:
    """Both digests describe the staged bytes, and the source is read to its end once."""
    content = b"x" * 200_000
    source = _CountingSource(content)

    published = await publish(staging.writer, _KEY, source, size_bytes=len(content))

    assert published.sha1 == hashlib.sha1(content).hexdigest()
    assert published.staged.sha256 == hashlib.sha256(content).hexdigest()
    assert source.ends == 1
    assert staging.content(_KEY) == content


async def test_a_new_version_replaces_the_registered_one(staging: MemoryStaging) -> None:
    """Restaging a key publishes the new bytes rather than losing to the old registration."""
    await publish(staging.writer, _KEY, io.BytesIO(b"old"), size_bytes=3)
    published = await publish(staging.writer, _KEY, io.BytesIO(b"newer"), size_bytes=5)

    assert staging.content(_KEY) == b"newer"
    assert published.staged.sha256 == hashlib.sha256(b"newer").hexdigest()
    assert len(staging.store.objects) == 1


async def test_publishing_takes_the_writer_lock_as_a_release_then_a_reservation(
    staging: MemoryStaging,
) -> None:
    """These two holdings are what a publish adds to the writer-lock timings."""
    await publish(staging.writer, _KEY, io.BytesIO(b"abc"), size_bytes=3)

    assert staging.index.lock_callers == ["occupancy.release", "occupancy.reserve"]


async def test_a_short_source_is_not_published(staging: MemoryStaging) -> None:
    """A source delivering fewer bytes than listed fails and registers nothing."""
    with pytest.raises(StagingIntegrityError):
        await publish(staging.writer, _KEY, io.BytesIO(b"abc"), size_bytes=10)
    assert staging.content(_KEY) is None


async def test_losing_the_key_to_another_attempt_is_an_integrity_error(
    staging: MemoryStaging, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registration that is not ours is never vouched for."""
    await publish(staging.writer, _KEY, io.BytesIO(b"theirs"), size_bytes=6)

    async def raced(key: StagingKey) -> None:
        """Another writer registers again straight after our release: theirs stays."""

    monkeypatch.setattr(staging.writer.occupancy, "release", raced)

    with pytest.raises(StagingIntegrityError, match="another attempt"):
        await publish(staging.writer, _KEY, io.BytesIO(b"ours!!"), size_bytes=6)
    assert staging.content(_KEY) == b"theirs"


def test_the_sha1_reader_counts_and_hashes_what_passes_through() -> None:
    """Partial reads accumulate into one digest."""
    reader = Sha1Reader(io.BytesIO(b"abcdef"))
    assert reader.read(2) + reader.read() == b"abcdef"
    assert reader.bytes_read == 6
    assert reader.hexdigest() == hashlib.sha1(b"abcdef").hexdigest()


_PHASES = ("upload_seconds", "registration_seconds", "commit_seconds")


def phase_events[E: Mapping[str, object]](events: list[E]) -> list[E]:
    """Return the reservation phase events among `events`."""
    return [e for e in events if e["event"] == "staging.reservation_phases"]


async def test_a_publication_reports_its_reservation_phases_once(staging: MemoryStaging) -> None:
    """One event per reservation, carrying every phase of the hold it timed."""
    with capture_logs() as events:
        await publish(staging.writer, _KEY, io.BytesIO(b"abc"), size_bytes=3)

    [event] = phase_events(events)
    assert event["log_level"] == "info"
    assert event["staging_key"] == str(_KEY)
    assert event["declared_bytes"] == 3
    assert event["outcome"] == "registered"
    assert event["error"] is None
    assert event["admission_seconds"] >= 0
    for phase in _PHASES:
        assert event[phase] >= 0
    assert (event["evicted"], event["protected"], event["orphans_deleted"]) == (0, 0, 0)


async def test_a_slow_upload_shows_in_its_phase(
    staging: MemoryStaging, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The upload is the phase a transfer timeout bounds, so it is timed on its own."""
    now = [100.0]
    monkeypatch.setattr(publish_module, "time", SimpleNamespace(monotonic=lambda: now[0]))
    upload = staging.store.upload

    async def slow_upload(
        target: StagedObject, source: BinaryIO, *, declared_max_bytes: int
    ) -> StagedObject:
        """Upload, with the phase clock advancing five seconds while it runs."""
        now[0] += 5.0
        return await upload(target, source, declared_max_bytes=declared_max_bytes)

    monkeypatch.setattr(staging.store, "upload", slow_upload)

    with capture_logs() as events:
        await publish(staging.writer, _KEY, io.BytesIO(b"abc"), size_bytes=3)

    [event] = phase_events(events)
    assert event["upload_seconds"] == 5.0
    assert event["registration_seconds"] == 0.0
    assert event["commit_seconds"] == 0.0


async def test_a_failed_upload_reports_the_phase_it_failed_in(staging: MemoryStaging) -> None:
    """A short source failed during the upload, so registration and commit never began."""
    with capture_logs() as events, pytest.raises(StagingIntegrityError):
        await publish(staging.writer, _KEY, io.BytesIO(b"abc"), size_bytes=10)

    [event] = phase_events(events)
    assert event["outcome"] == "failed"
    assert event["error"] == "StagingIntegrityError"
    assert event["upload_seconds"] >= 0
    assert event["registration_seconds"] is None
    assert event["commit_seconds"] is None


async def test_a_refused_reservation_reports_no_phases(tmp_path: Path) -> None:
    """Refusal ends the reservation before anything ran under it."""
    staging = MemoryStaging(tmp_path, max_bytes=8)

    with capture_logs() as events, pytest.raises(StagingCapacityExhaustedError):
        await publish(staging.writer, _KEY, io.BytesIO(b"x" * 16), size_bytes=16)

    [event] = phase_events(events)
    assert event["outcome"] == "refused"
    assert event["error"] == "StagingCapacityExhaustedError"
    assert event["admission_seconds"] is None
    for phase in _PHASES:
        assert event[phase] is None
    assert (event["evicted"], event["protected"], event["orphans_deleted"]) == (None,) * 3


async def test_an_admission_that_fails_to_measure_reports_no_admission(
    staging: MemoryStaging, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without an admitted reservation there is no admission time or reclamation to report."""

    async def unmeasurable() -> OccupancyReport:
        """Fail the measurement admission decides against."""
        raise StagingObjectStoreError("listing failed")

    monkeypatch.setattr(staging.writer.occupancy, "measure", unmeasurable)

    with capture_logs() as events, pytest.raises(StagingObjectStoreError):
        await publish(staging.writer, _KEY, io.BytesIO(b"abc"), size_bytes=3)

    [event] = phase_events(events)
    assert (event["outcome"], event["error"]) == ("failed", "StagingObjectStoreError")
    assert event["admission_seconds"] is None
    for phase in _PHASES:
        assert event[phase] is None
    assert (event["evicted"], event["protected"], event["orphans_deleted"]) == (None,) * 3


async def test_a_cancelled_upload_reports_its_cancellation(staging: MemoryStaging) -> None:
    """A drained worker cancels mid-upload; the hold it had taken is still reported."""
    staging.store.upload_gate = asyncio.Event()

    with capture_logs() as events:
        publishing = asyncio.create_task(
            publish(staging.writer, _KEY, io.BytesIO(b"abc"), size_bytes=3)
        )
        await staging.store.uploading.wait()
        publishing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await publishing

    [event] = phase_events(events)
    assert event["outcome"] == "cancelled"
    assert event["error"] == "CancelledError"
    assert event["upload_seconds"] >= 0
    assert event["registration_seconds"] is None


async def test_a_reclaiming_admission_reports_what_it_reclaimed(tmp_path: Path) -> None:
    """Eviction and orphan collection are the admission work that takes entry locks."""
    staging = MemoryStaging(tmp_path, max_bytes=8)
    await publish(staging.writer, StagingKey("1" * 32), io.BytesIO(b"x" * 4), size_bytes=4)
    stray = StagedObject(
        key=StagingKey("2" * 32),
        attempt_id=uuid4(),
        size_bytes=2,
        sha256="",
        created_at=datetime.now(UTC),
    )
    staging.store.objects[stray.object_key] = (stray, b"yy")

    with capture_logs() as events:
        await publish(staging.writer, _KEY, io.BytesIO(b"z" * 8), size_bytes=8)

    [event] = phase_events(events)
    assert event["outcome"] == "registered"
    assert (event["evicted"], event["orphans_deleted"]) == (1, 1)


async def test_losing_the_key_after_commit_is_reported_as_registered(
    staging: MemoryStaging, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reservation itself succeeded; the lookup that loses the race is outside it."""
    await publish(staging.writer, _KEY, io.BytesIO(b"theirs"), size_bytes=6)

    async def raced(key: StagingKey) -> None:
        """Leave the earlier registration in place, as a concurrent writer would."""

    monkeypatch.setattr(staging.writer.occupancy, "release", raced)

    with capture_logs() as events, pytest.raises(StagingIntegrityError, match="another attempt"):
        await publish(staging.writer, _KEY, io.BytesIO(b"ours!!"), size_bytes=6)

    assert [e["outcome"] for e in phase_events(events)] == ["registered"]
