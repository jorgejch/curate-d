"""Staging and snapshot inspection through the real backends on the local RustFS engine.

The writer and reader identities are the ones the two worker profiles hold, and the cache
index is the real PostgreSQL one. Evidence about RustFS only: the OVH half of every
staging behavior is the content-staging suite's to establish.
"""

import hashlib
import io
import os
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image
from pydantic import HttpUrl, SecretStr

from curate_d.config import S3AddressingStyle, Settings, StagingRole
from curate_d.domain import HashAlgorithm, Provider, SourceObject, StrongFingerprint
from curate_d.ingest.pipeline import StagedRef, StageOutcome, inspect_staged, stage_source
from curate_d.providers.pcloud.files import PCloudFiles
from curate_d.staging.errors import StagedEntryMissingError, StagedEntrySupersededError
from curate_d.staging.factories import (
    StagingReaderBackend,
    StagingWriterBackend,
    build_reader_backend,
    build_writer_backend,
)
from curate_d.staging.keys import StagingKey
from curate_d.staging.snapshots import LocalScratchBudget
from tests.integration.rustfs_staging import provisioned_location
from tests.stubs.pcloud import StubFile, StubPCloud

pytestmark = pytest.mark.integration

_ENDPOINT = os.environ.get("CURATE_D_TEST_RUSTFS_ENDPOINT", "http://127.0.0.1:9100")
_WRITER = ("curate-d-staging-writer", "writer-development-secret")
_READER = ("curate-d-staging-reader", "reader-development-secret")


def _settings(
    base: Settings, role: StagingRole, credentials: tuple[str, str], scratch: Path
) -> Settings:
    """One staging identity against the local RustFS fixture."""
    bucket, prefix = provisioned_location()
    return base.model_copy(
        update={
            "staging_s3_endpoint_url": HttpUrl(_ENDPOINT),
            "staging_s3_bucket": bucket,
            "staging_s3_content_prefix": prefix,
            "staging_s3_region": "us-east-1",
            "staging_s3_role": role,
            "staging_s3_access_key_id": SecretStr(credentials[0]),
            "staging_s3_secret_access_key": SecretStr(credentials[1]),
            "staging_s3_addressing_style": S3AddressingStyle.PATH,
            "staging_s3_verify_tls": False,
            "staging_local_scratch_root": scratch,
        }
    )


async def _direct[T](work: Coroutine[None, None, T]) -> T:
    """Run staging work on the test's loop, which owns these backends."""
    return await work


@pytest.fixture
async def writer(settings: Settings, tmp_path: Path) -> AsyncIterator[StagingWriterBackend]:
    """The I/O profile's staging writer."""
    backend = build_writer_backend(_settings(settings, StagingRole.WRITER, _WRITER, tmp_path / "w"))
    try:
        yield backend
    finally:
        await backend.aclose()


@pytest.fixture
async def reader(settings: Settings, tmp_path: Path) -> AsyncIterator[StagingReaderBackend]:
    """The CPU profile's staging reader."""
    backend = build_reader_backend(_settings(settings, StagingRole.READER, _READER, tmp_path / "r"))
    try:
        yield backend
    finally:
        await backend.aclose()


@pytest.fixture
async def files() -> AsyncIterator[tuple[StubPCloud, PCloudFiles]]:
    """A stub pCloud account and a client for it."""
    pcloud = StubPCloud()
    async with pcloud.client() as http:
        yield (
            pcloud,
            PCloudFiles(
                http, api_host=pcloud.host, account="1", access_token=SecretStr(pcloud.access_token)
            ),
        )


def _source(stub: StubFile) -> SourceObject:
    """What a scan recorded for `stub`, with a unique identity per run."""
    return SourceObject.build(
        provider=Provider.PCLOUD,
        account=f"rustfs-{os.urandom(4).hex()}",
        native_id=str(stub.file_id),
        path=f"/{stub.name}",
        size_bytes=len(stub.content),
        modified_at=datetime.now(UTC),
        version_marker=str(stub.marker),
        reported_digests=(
            StrongFingerprint(
                algorithm=HashAlgorithm.SHA1, digest=hashlib.sha1(stub.content).hexdigest()
            ),
        ),
    )


def _jpeg() -> bytes:
    """A small JPEG."""
    buffer = io.BytesIO()
    Image.new("RGB", (80, 60), (120, 30, 200)).save(buffer, format="JPEG")
    return buffer.getvalue()


async def _stage(
    files: PCloudFiles, writer: StagingWriterBackend, source: SourceObject, scratch: Path
) -> StagedRef:
    """Stage a source and return its ref."""
    result = await stage_source(
        files,
        writer,
        source,
        scratch_root=scratch,
        budget=LocalScratchBudget(64 * 1024 * 1024),
        publish_on_staging_loop=_direct,
    )
    assert result.outcome is StageOutcome.STAGED
    assert result.staged is not None
    return result.staged


async def test_a_staged_image_is_inspected_from_a_verified_snapshot(
    writer: StagingWriterBackend,
    reader: StagingReaderBackend,
    files: tuple[StubPCloud, PCloudFiles],
    tmp_path: Path,
) -> None:
    """Writer publishes through RustFS with both digests; the reader snapshots and decodes."""
    pcloud, client = files
    content = _jpeg()
    source = _source(pcloud.file("/photo.jpg", content))
    staged = await _stage(client, writer, source, tmp_path / "fetch")
    try:
        assert staged.sha256 == hashlib.sha256(content).hexdigest()
        assert staged.sha1 == hashlib.sha1(content).hexdigest()
        inspection = await inspect_staged(reader.reader, staged)
        assert inspection.media_type == "image/jpeg"
        assert inspection.perceptual is not None
    finally:
        await writer.occupancy.release(StagingKey(staged.key))


async def test_a_released_entry_is_an_explicit_miss(
    writer: StagingWriterBackend,
    reader: StagingReaderBackend,
    files: tuple[StubPCloud, PCloudFiles],
    tmp_path: Path,
) -> None:
    """The reader raises rather than fetching: it holds no provider credential."""
    pcloud, client = files
    staged = await _stage(client, writer, _source(pcloud.file("/a.txt", b"a")), tmp_path / "f")
    await writer.occupancy.release(StagingKey(staged.key))

    with pytest.raises(StagedEntryMissingError):
        await inspect_staged(reader.reader, staged)


async def test_a_replaced_version_is_refused_as_superseded(
    writer: StagingWriterBackend,
    reader: StagingReaderBackend,
    files: tuple[StubPCloud, PCloudFiles],
    tmp_path: Path,
) -> None:
    """Restaging a file under the same key makes the old ref unreadable, not wrong."""
    pcloud, client = files
    stub = pcloud.file("/doc.txt", b"one")
    source = _source(stub)
    first = await _stage(client, writer, source, tmp_path / "f1")
    pcloud.modify(stub, b"two")
    second = await _stage(
        client,
        writer,
        source.model_copy(
            update={
                "reported_digests": (
                    StrongFingerprint(
                        algorithm=HashAlgorithm.SHA1, digest=hashlib.sha1(b"two").hexdigest()
                    ),
                )
            }
        ),
        tmp_path / "f2",
    )
    try:
        assert first.key == second.key
        with pytest.raises(StagedEntrySupersededError):
            await inspect_staged(reader.reader, first)
        assert (await inspect_staged(reader.reader, second)).media_type == "text/plain"
    finally:
        await writer.occupancy.release(StagingKey(second.key))
