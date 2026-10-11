"""Publishing a source's bytes: SHA-1 in the upload's own pass, and versions replaced."""

import hashlib
import io
from pathlib import Path

import pytest

from curate_d.staging.errors import StagingIntegrityError
from curate_d.staging.keys import StagingKey
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
