"""Verified seekable snapshots against the local RustFS fixture.

Runs the real adapter with the real reader identity, because the property under test is
what a CPU worker actually gets back: a local file whose bytes stay put after the remote
object it came from is gone. These results are evidence about RustFS only; the OVH half of
each pair remains pending until it runs against that endpoint.
"""

import hashlib
import io
import os
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import HttpUrl, SecretStr

from curate_d.config import S3AddressingStyle, Settings, StagingRole
from curate_d.staging.errors import StagedEntryMissingError
from curate_d.staging.keys import StagingKey
from curate_d.staging.object_store import S3StagingObjectStore
from curate_d.staging.objects import StagedObject
from curate_d.staging.snapshots import LocalScratchBudget, LocalSnapshotReader

pytestmark = pytest.mark.integration

_ENDPOINT = os.environ.get("CURATE_D_TEST_RUSTFS_ENDPOINT", "http://127.0.0.1:9100")
_BUCKET = os.environ.get("CURATE_D_TEST_RUSTFS_BUCKET", "curate-d-staging")
_PREFIX = os.environ.get("CURATE_D_TEST_RUSTFS_PREFIX", "content/")
_WRITER = ("curate-d-staging-writer", "writer-development-secret")
_READER = ("curate-d-staging-reader", "reader-development-secret")


def _settings(role: StagingRole, credentials: tuple[str, str]) -> Settings:
    """Configuration for one staging identity against the local RustFS fixture."""
    return Settings(
        staging_s3_endpoint_url=HttpUrl(_ENDPOINT),
        staging_s3_bucket=_BUCKET,
        staging_s3_region="us-east-1",
        staging_s3_role=role,
        staging_s3_access_key_id=SecretStr(credentials[0]),
        staging_s3_secret_access_key=SecretStr(credentials[1]),
        staging_s3_addressing_style=S3AddressingStyle.PATH,
        # The local fixture is plaintext loopback; configuration refuses to pretend
        # otherwise, so this has to be disabled deliberately rather than by default.
        staging_s3_verify_tls=False,
        staging_s3_content_prefix=_PREFIX,
    )


class DirectHold:
    """A cache index holding one registration in memory.

    The database half of the seam has its own coverage; what this suite is for is the
    bytes, so the hold here is the smallest thing that satisfies the reader's protocol.
    """

    def __init__(self) -> None:
        self.registrations: dict[StagingKey, StagedObject] = {}

    @asynccontextmanager
    async def hold_for_download(self, key: StagingKey) -> AsyncGenerator[StagedObject | None, None]:
        yield self.registrations.get(key)


@pytest.fixture
def writer_store() -> S3StagingObjectStore:
    """The staging writer identity, as worker-io holds it."""
    return S3StagingObjectStore(_settings(StagingRole.WRITER, _WRITER))


@pytest.fixture
def reader_store() -> S3StagingObjectStore:
    """The staging reader identity, as worker-cpu holds it."""
    return S3StagingObjectStore(_settings(StagingRole.READER, _READER))


@pytest.fixture
def index() -> DirectHold:
    return DirectHold()


@pytest.fixture
def reader(
    reader_store: S3StagingObjectStore, index: DirectHold, tmp_path: Path
) -> LocalSnapshotReader:
    return LocalSnapshotReader(
        reader_store,
        index,
        scratch_root=tmp_path / "scratch",
        budget=LocalScratchBudget(64 * 1024 * 1024),
    )


@pytest.fixture
async def staged(
    writer_store: S3StagingObjectStore, index: DirectHold
) -> AsyncIterator[tuple[StagingKey, bytes]]:
    """Upload one object under a unique key, register it, and delete it afterwards."""
    payload = os.urandom(256 * 1024)
    key = StagingKey(uuid4().hex)
    target = StagedObject(
        key=key,
        attempt_id=uuid4(),
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        created_at=datetime.now(tz=UTC),
    )
    registered = await writer_store.upload(
        target, io.BytesIO(payload), declared_max_bytes=len(payload)
    )
    index.registrations[key] = registered
    try:
        yield key, payload
    finally:
        # Runs on failure as well as success; a failed test must leave no billed residue.
        await writer_store.delete(registered.object_key)


async def test_a_registered_object_snapshots_to_a_seekable_local_file(
    reader: LocalSnapshotReader, staged: tuple[StagingKey, bytes]
) -> None:
    """The reader identity round trips real bytes into a file a decoder can seek."""
    key, payload = staged

    async with reader.open(key) as handle:
        handle.seek(len(payload) - 16)
        assert handle.read() == payload[-16:]
        handle.seek(0)
        assert hashlib.sha256(handle.read()).hexdigest() == hashlib.sha256(payload).hexdigest()


async def test_deleting_the_remote_object_does_not_disturb_an_open_read(
    reader: LocalSnapshotReader,
    writer_store: S3StagingObjectStore,
    index: DirectHold,
    staged: tuple[StagingKey, bytes],
) -> None:
    """Cleanup mid-decode cannot truncate a snapshot: the decoder reads its own copy."""
    key, payload = staged

    async with reader.open(key) as handle:
        first = handle.read(1024)
        await writer_store.delete(index.registrations[key].object_key)
        assert first + handle.read() == payload


async def test_a_deleted_object_fails_the_next_read_without_a_provider_fetch(
    reader: LocalSnapshotReader,
    writer_store: S3StagingObjectStore,
    index: DirectHold,
    staged: tuple[StagingKey, bytes],
    tmp_path: Path,
) -> None:
    """A stale registration is a typed failure, never a re-fetch from the source."""
    key, _ = staged
    await writer_store.delete(index.registrations[key].object_key)

    with pytest.raises(StagedEntryMissingError):
        async with reader.open(key):
            pass

    assert sorted((tmp_path / "scratch").iterdir()) == []


async def test_a_registration_naming_absent_content_is_a_typed_miss(
    reader: LocalSnapshotReader, index: DirectHold
) -> None:
    """A registration whose object was never uploaded fails rather than resolving itself."""
    key = StagingKey(uuid4().hex)
    index.registrations[key] = StagedObject(
        key=key,
        attempt_id=UUID(int=0),
        size_bytes=3,
        sha256=hashlib.sha256(b"abc").hexdigest(),
        created_at=datetime.now(tz=UTC),
    )

    with pytest.raises(StagedEntryMissingError):
        async with reader.open(key):
            pass
