"""Verified seekable snapshots, against a substituted object store and cache index.

The fakes stop exactly where the real collaborators do: a store that hands out bytes and
refuses every other operation, and an index that records whether its read lock was held
when the download ran. Real S3 behaviour is the integration suite's job.
"""

import asyncio
import errno
import hashlib
import inspect
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, cast
from uuid import uuid4

import pytest

from curate_d.staging import snapshots
from curate_d.staging.cache_index import PostgresStagingCacheIndex
from curate_d.staging.errors import (
    StagedContentUnreadableError,
    StagedEntryMissingError,
    StagingCapacityExhaustedError,
    StagingCapacityNeverFitsError,
    StagingIntegrityError,
)
from curate_d.staging.keys import StagingKey
from curate_d.staging.objects import (
    StagedObject,
    StagedObjectRef,
    StagingDownloadStore,
    StagingObjectStore,
)
from curate_d.staging.snapshots import (
    LocalScratchBudget,
    LocalSnapshotReader,
    StagingDownloadHold,
)
from tests.unit.staging.conftest import key_for

_WHEN = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

# Small enough that a handful of bytes arrives in several writes, so a failure injected
# mid-download leaves a genuinely partial file behind.
_CHUNK = 4

# SHA-256 of the empty string, and of content nothing in these tests stages.
_EMPTY_DIGEST = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_WRONG_DIGEST = "f" * 64

if TYPE_CHECKING:

    def _the_cache_index_is_a_hold(index: PostgresStagingCacheIndex) -> StagingDownloadHold:
        """Check at type-check time that the real index satisfies the reader's protocol."""
        return index


class FakeIndex:
    """A cache index that registers objects in memory and lends its read lock."""

    def __init__(self) -> None:
        self.registrations: dict[StagingKey, StagedObject] = {}
        self.holding = False
        self.holds: list[StagingKey] = []

    def hold_for_download(
        self, key: StagingKey
    ) -> AbstractAsyncContextManager[StagedObject | None]:
        return self._hold(key)

    @asynccontextmanager
    async def _hold(self, key: StagingKey) -> AsyncGenerator[StagedObject | None, None]:
        self.holds.append(key)
        self.holding = True
        try:
            yield self.registrations.get(key)
        finally:
            self.holding = False


class FakeObjectStore:
    """Staging object storage as the read path uses it: downloads, and nothing else.

    Every other protocol method raises, so a test fails loudly if the read path ever
    reaches for write authority instead of surfacing a cache miss.
    """

    def __init__(self, index: FakeIndex) -> None:
        self._index = index
        self.payloads: dict[str, bytes] = {}
        self.downloads: list[str] = []
        self.held_during_download: list[bool] = []
        self.failure: BaseException | None = None
        self.partial_bytes = 0
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()

    async def download(self, obj: StagedObject, destination: BinaryIO) -> None:
        self.downloads.append(obj.object_key)
        self.held_during_download.append(self._index.holding)
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        payload = self.payloads.get(obj.object_key, b"")
        if self.failure is not None:
            destination.write(payload[: self.partial_bytes])
            raise self.failure
        for start in range(0, len(payload), _CHUNK):
            destination.write(payload[start : start + _CHUNK])

    async def upload(
        self, target: StagedObject, source: BinaryIO, *, declared_max_bytes: int
    ) -> StagedObject:
        raise AssertionError("the read path must never upload")

    async def delete(self, object_key: str) -> None:
        raise AssertionError("the read path must never delete")

    def list_objects(self) -> AsyncIterator[StagedObjectRef]:
        raise AssertionError("the read path must never list the bucket")

    async def abort_incomplete_uploads(self, older_than: datetime) -> int:
        raise AssertionError("the read path must never abort uploads")

    @asynccontextmanager
    async def batch(self) -> AsyncGenerator[None, None]:
        """Share no client; this fake opens none."""
        yield


@pytest.fixture
def index() -> FakeIndex:
    return FakeIndex()


@pytest.fixture
def store(index: FakeIndex) -> FakeObjectStore:
    return FakeObjectStore(index)


@pytest.fixture
def scratch(tmp_path: Path) -> Path:
    return tmp_path / "scratch"


@pytest.fixture
def budget() -> LocalScratchBudget:
    return LocalScratchBudget(1024)


@pytest.fixture
def reader(
    store: FakeObjectStore, index: FakeIndex, scratch: Path, budget: LocalScratchBudget
) -> LocalSnapshotReader:
    return LocalSnapshotReader(store, index, scratch_root=scratch, budget=budget)


def stage(
    index: FakeIndex,
    store: FakeObjectStore,
    payload: bytes,
    *,
    seed: int = 1,
    size_bytes: int | None = None,
    sha256: str | None = None,
) -> StagingKey:
    """Register one object and give the store its bytes; return its staging key.

    `size_bytes` and `sha256` default to the payload's own, so a test that overrides
    either is deliberately describing a registration the bytes no longer match.
    """
    key = key_for(seed)
    obj = StagedObject(
        key=key,
        attempt_id=uuid4(),
        size_bytes=len(payload) if size_bytes is None else size_bytes,
        sha256=hashlib.sha256(payload).hexdigest() if sha256 is None else sha256,
        created_at=_WHEN,
    )
    index.registrations[key] = obj
    store.payloads[obj.object_key] = payload
    return key


def scratch_files(scratch: Path) -> list[Path]:
    """Everything left on the scratch volume, named so a leak reads clearly."""
    return sorted(scratch.iterdir()) if scratch.is_dir() else []


async def test_a_snapshot_is_seekable_and_matches_its_registration(
    reader: LocalSnapshotReader, index: FakeIndex, store: FakeObjectStore
) -> None:
    """The decoder gets a local file it can seek within, holding the registered bytes."""
    key = stage(index, store, b"staged media bytes")

    async with reader.open(key) as handle:
        assert handle.seekable()
        handle.seek(7)
        assert handle.read() == b"media bytes"
        handle.seek(0)
        assert handle.read() == b"staged media bytes"


async def test_the_snapshot_has_no_name_while_the_decoder_reads_it(
    reader: LocalSnapshotReader, index: FakeIndex, store: FakeObjectStore, scratch: Path
) -> None:
    """Unlinked before the handle is yielded, so nothing on disk can truncate the read."""
    key = stage(index, store, b"content that outlives its name")

    async with reader.open(key) as handle:
        assert scratch_files(scratch) == []
        assert handle.read() == b"content that outlives its name"


async def test_closing_the_handle_releases_scratch_and_capacity(
    reader: LocalSnapshotReader,
    index: FakeIndex,
    store: FakeObjectStore,
    budget: LocalScratchBudget,
) -> None:
    """Both the handle and its reservation are gone once the block exits."""
    key = stage(index, store, b"payload")

    async with reader.open(key) as handle:
        assert budget.reserved_bytes == 7

    assert handle.closed
    assert budget.reserved_bytes == 0


async def test_a_zero_length_object_snapshots(
    reader: LocalSnapshotReader, index: FakeIndex, store: FakeObjectStore
) -> None:
    """Empty content verifies and opens like any other; the spec requires it work."""
    key = stage(index, store, b"", sha256=_EMPTY_DIGEST)

    async with reader.open(key) as handle:
        assert handle.read() == b""


async def test_the_read_lock_is_held_through_the_download_and_released_before_decoding(
    reader: LocalSnapshotReader, index: FakeIndex, store: FakeObjectStore
) -> None:
    """Eviction cannot slip in mid-download, and a decode holds no database transaction."""
    key = stage(index, store, b"locked while streaming")

    async with reader.open(key):
        assert store.held_during_download == [True]
        assert index.holding is False


async def test_the_local_verify_pass_runs_outside_the_read_lock(
    reader: LocalSnapshotReader,
    index: FakeIndex,
    store: FakeObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-hashing local bytes must not pin a ledger transaction or a pooled connection."""
    key = stage(index, store, b"verified after the stream")
    held_during_verify: list[bool] = []
    verify = snapshots._require_verified

    def record(path: Path, obj: StagedObject) -> None:
        held_during_verify.append(index.holding)
        verify(path, obj)

    monkeypatch.setattr(snapshots, "_require_verified", record)

    async with reader.open(key) as handle:
        assert handle.read() == b"verified after the stream"
    assert store.held_during_download == [True]
    assert held_during_verify == [False]


async def test_a_size_mismatch_returns_no_handle(
    reader: LocalSnapshotReader,
    index: FakeIndex,
    store: FakeObjectStore,
    scratch: Path,
    budget: LocalScratchBudget,
) -> None:
    """A truncated transfer is caught locally and its scratch file reclaimed."""
    key = stage(index, store, b"short", size_bytes=99)

    with pytest.raises(StagingIntegrityError, match="holds 5 bytes"):
        async with reader.open(key):
            pass

    assert scratch_files(scratch) == []
    assert budget.reserved_bytes == 0


async def test_a_checksum_mismatch_returns_no_handle(
    reader: LocalSnapshotReader,
    index: FakeIndex,
    store: FakeObjectStore,
    scratch: Path,
    budget: LocalScratchBudget,
) -> None:
    """Right length, wrong content: no decoder ever sees the bytes."""
    key = stage(index, store, b"substituted content", sha256=_WRONG_DIGEST)

    with pytest.raises(StagingIntegrityError, match="is registered as"):
        async with reader.open(key):
            pass

    assert scratch_files(scratch) == []
    assert budget.reserved_bytes == 0


async def test_an_unregistered_key_raises_without_touching_the_store(
    reader: LocalSnapshotReader, store: FakeObjectStore, budget: LocalScratchBudget
) -> None:
    """A cache miss is explicit; there is no provider to fall back to."""
    with pytest.raises(StagedEntryMissingError, match="holds no provider credential"):
        async with reader.open(key_for(9)):
            pass

    assert store.downloads == []
    assert budget.reserved_bytes == 0


async def test_a_missing_remote_object_raises_without_a_second_attempt(
    reader: LocalSnapshotReader,
    index: FakeIndex,
    store: FakeObjectStore,
    scratch: Path,
    budget: LocalScratchBudget,
) -> None:
    """The registration is stale: the read fails once rather than re-fetching anything."""
    key = stage(index, store, b"gone")
    store.failure = StagedEntryMissingError("staged object was deleted")

    with pytest.raises(StagedEntryMissingError, match="was deleted"):
        async with reader.open(key):
            pass

    assert len(store.downloads) == 1
    assert scratch_files(scratch) == []
    assert budget.reserved_bytes == 0


async def test_an_abrupt_failure_mid_download_reclaims_the_partial_file(
    reader: LocalSnapshotReader,
    index: FakeIndex,
    store: FakeObjectStore,
    scratch: Path,
    budget: LocalScratchBudget,
) -> None:
    """A connection dropped part way leaves neither a handle nor a partial snapshot."""
    key = stage(index, store, b"first half second half")
    store.failure = ConnectionResetError("peer closed the connection")
    store.partial_bytes = 10

    with pytest.raises(ConnectionResetError):
        async with reader.open(key):
            pass

    assert scratch_files(scratch) == []
    assert budget.reserved_bytes == 0


async def test_cancellation_mid_download_reclaims_the_partial_file(
    reader: LocalSnapshotReader,
    index: FakeIndex,
    store: FakeObjectStore,
    scratch: Path,
    budget: LocalScratchBudget,
) -> None:
    """Interruption is not an error path the cleanup may skip: the pod may be stopping."""
    key = stage(index, store, b"interrupted content")
    store.failure = asyncio.CancelledError()
    store.partial_bytes = 6

    with pytest.raises(asyncio.CancelledError):
        async with reader.open(key):
            pass

    assert scratch_files(scratch) == []
    assert budget.reserved_bytes == 0


async def test_a_decoder_exception_releases_the_snapshot_and_its_capacity(
    reader: LocalSnapshotReader,
    index: FakeIndex,
    store: FakeObjectStore,
    scratch: Path,
    budget: LocalScratchBudget,
) -> None:
    """A parser that raises mid-read still gives its scratch back."""
    key = stage(index, store, b"media a decoder chokes on")

    with pytest.raises(ValueError, match="unsupported"):
        async with reader.open(key) as handle:
            handle.read(4)
            raise ValueError("unsupported media")

    assert handle.closed
    assert scratch_files(scratch) == []
    assert budget.reserved_bytes == 0


async def test_a_full_scratch_volume_is_capacity_exhaustion(
    reader: LocalSnapshotReader, index: FakeIndex, store: FakeObjectStore, scratch: Path
) -> None:
    """The node's disk can run out under a reservation the budget did admit."""
    key = stage(index, store, b"too much")
    store.failure = OSError(errno.ENOSPC, "No space left on device")

    with pytest.raises(StagingCapacityExhaustedError, match="ran out of room"):
        async with reader.open(key):
            pass

    assert scratch_files(scratch) == []


async def test_another_local_failure_is_unreadable_content(
    reader: LocalSnapshotReader, index: FakeIndex, store: FakeObjectStore
) -> None:
    """A scratch volume that refuses the write is distinct from a missing entry."""
    key = stage(index, store, b"denied")
    store.failure = OSError(errno.EACCES, "Permission denied")

    with pytest.raises(StagedContentUnreadableError, match="could not be written"):
        async with reader.open(key):
            pass


async def test_a_failed_cleanup_unlink_does_not_leak_the_read_handle(
    reader: LocalSnapshotReader,
    index: FakeIndex,
    store: FakeObjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Close the snapshot descriptor when the unlink that was to hide it fails."""
    key = stage(index, store, b"undeletable")
    opened: list[BinaryIO] = []
    real_open = Path.open

    def track(path: Path, mode: str = "r") -> BinaryIO:
        """Record every binary read handle the reader opens under scratch."""
        handle = cast("BinaryIO", real_open(path, mode))
        if mode == "rb":
            opened.append(handle)
        return handle

    def refuse(path: Path, missing_ok: bool = False) -> None:
        """Fail the way a scratch mount that will not release a name does."""
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(Path, "open", track)
    monkeypatch.setattr(Path, "unlink", refuse)

    with pytest.raises(StagedContentUnreadableError, match="could not be written"):
        async with reader.open(key):
            pass

    assert opened
    assert all(handle.closed for handle in opened)


async def test_a_second_reader_is_refused_while_the_first_holds_its_handle(
    store: FakeObjectStore, index: FakeIndex, scratch: Path
) -> None:
    """The reservation lasts until close, so two open snapshots cannot exceed the bound."""
    first = stage(index, store, b"aaaaaaaa", seed=1)
    second = stage(index, store, b"bbbbbbbb", seed=2)
    budget = LocalScratchBudget(8)
    reader = LocalSnapshotReader(store, index, scratch_root=scratch, budget=budget)

    async with reader.open(first) as handle:
        with pytest.raises(StagingCapacityExhaustedError, match="have not closed"):
            async with reader.open(second):
                pass
        assert handle.read() == b"aaaaaaaa"

    # The refusal cost nothing: the same snapshot fits once the first handle is closed.
    async with reader.open(second) as handle:
        assert handle.read() == b"bbbbbbbb"


async def test_a_second_reader_is_refused_while_the_first_is_still_downloading(
    store: FakeObjectStore, index: FakeIndex, scratch: Path
) -> None:
    """The bound covers in-flight snapshots too, not just ones already materialized."""
    first = stage(index, store, b"aaaaaaaa", seed=1)
    second = stage(index, store, b"bbbbbbbb", seed=2)
    budget = LocalScratchBudget(8)
    reader = LocalSnapshotReader(store, index, scratch_root=scratch, budget=budget)
    store.gate = asyncio.Event()

    async def read_first() -> bytes:
        async with reader.open(first) as handle:
            return handle.read()

    streaming = asyncio.create_task(read_first())
    await store.started.wait()
    try:
        with pytest.raises(StagingCapacityExhaustedError, match="have not closed") as refused:
            async with reader.open(second):
                pass
        assert not isinstance(refused.value, StagingCapacityNeverFitsError)
    finally:
        store.gate.set()

    assert await streaming == b"aaaaaaaa"
    assert budget.reserved_bytes == 0


async def test_an_object_larger_than_the_bound_never_fits(
    store: FakeObjectStore, index: FakeIndex, scratch: Path
) -> None:
    """Refused before any transfer: a retry cannot make a bound grow."""
    key = stage(index, store, b"far too large for this pod")
    reader = LocalSnapshotReader(store, index, scratch_root=scratch, budget=LocalScratchBudget(4))

    with pytest.raises(StagingCapacityNeverFitsError, match="never fits"):
        async with reader.open(key):
            pass

    assert store.downloads == []


def test_the_budget_refuses_a_ceiling_that_admits_nothing() -> None:
    """A zero or negative bound is configuration nobody meant, not an empty cache."""
    with pytest.raises(ValueError, match="max_bytes must be positive"):
        LocalScratchBudget(0)


def test_the_budget_refuses_a_negative_reservation() -> None:
    """Negative bytes would silently hand capacity back that was never taken."""
    budget = LocalScratchBudget(16)

    with pytest.raises(ValueError, match="needed_bytes must be nonnegative"), budget.reserve(-1):
        pass


def test_the_budget_reports_its_ceiling_and_what_is_held() -> None:
    """Accounting is readable so an operator can see why a snapshot was refused."""
    budget = LocalScratchBudget(16)

    with budget.reserve(6), budget.reserve(4):
        assert budget.max_bytes == 16
        assert budget.reserved_bytes == 10
    assert budget.reserved_bytes == 0


def test_the_read_path_takes_no_provider_credential() -> None:
    """Require download-only collaborators and no provider credential parameter."""
    signature = inspect.signature(LocalSnapshotReader.__init__)
    parameters = set(signature.parameters) - {"self"}

    assert parameters == {"store", "index", "scratch_root", "budget"}
    # The reader protocol cannot express a source fetch or a staging mutation.
    assert signature.parameters["store"].annotation is StagingDownloadStore
    assert not any(
        hasattr(StagingDownloadStore, name) for name in ("upload", "delete", "list_objects")
    )
    assert signature.parameters["index"].annotation is StagingDownloadHold


def test_the_staging_fakes_satisfy_the_real_contracts(
    store: FakeObjectStore, index: FakeIndex
) -> None:
    """The fakes stand in for the protocols the reader is written against."""
    assert isinstance(store, StagingObjectStore)
    assert isinstance(index, StagingDownloadHold)


def test_the_cache_index_satisfies_the_download_hold_protocol() -> None:
    """The reader's narrow hold protocol is the one the Postgres index already provides."""
    assert issubclass(PostgresStagingCacheIndex, StagingDownloadHold)
