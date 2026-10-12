"""Staging a listed file and inspecting what was staged, against stubs of both sides."""

import hashlib
import io
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from pydantic import SecretStr

from curate_d.domain import (
    HashAlgorithm,
    Provider,
    SourceObject,
    SourceObjectState,
    StrongFingerprint,
)
from curate_d.ingest.pipeline import (
    StagedRef,
    StageOutcome,
    StageResult,
    inspect_staged,
    is_current,
    observed_marker,
    stage_source,
)
from curate_d.providers.errors import ProviderRequestError
from curate_d.providers.pcloud.files import PCloudFiles
from curate_d.staging.errors import (
    StagedEntryMissingError,
    StagedEntrySupersededError,
    StagingCapacityExhaustedError,
)
from curate_d.staging.snapshots import LocalScratchBudget
from tests.stubs.pcloud import Failure, StubFile, StubPCloud
from tests.stubs.staging import MemoryStaging


def _png() -> bytes:
    """A small picture."""
    image = Image.new("RGB", (64, 48), (10, 20, 30))
    ImageDraw.Draw(image).ellipse((8, 8, 40, 40), fill=(200, 100, 50))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _source(stub: StubFile, *, listed: bytes | None = None) -> SourceObject:
    """The source object a scan would have recorded for `stub`, listing `listed`."""
    content = listed if listed is not None else stub.content
    return SourceObject.build(
        provider=Provider.PCLOUD,
        account="4242",
        native_id=str(stub.file_id),
        path=f"/{stub.name}",
        size_bytes=len(content),
        modified_at=stub.modified,
        version_marker=str(stub.marker),
        reported_digests=(
            StrongFingerprint(
                algorithm=HashAlgorithm.SHA1, digest=hashlib.sha1(content).hexdigest()
            ),
            StrongFingerprint(
                algorithm=HashAlgorithm.SHA256, digest=hashlib.sha256(content).hexdigest()
            ),
        ),
    )


async def _direct[T](work: Coroutine[None, None, T]) -> T:
    """Run staging work on the current loop, as a single-loop harness does."""
    return await work


@pytest.fixture
def pcloud() -> StubPCloud:
    """An EU account."""
    return StubPCloud()


@pytest.fixture
def staging(tmp_path: Path) -> MemoryStaging:
    """Empty staging."""
    return MemoryStaging(tmp_path / "snapshots")


@pytest.fixture
async def files(pcloud: StubPCloud) -> AsyncIterator[PCloudFiles]:
    """A client for the stub account."""
    async with pcloud.client() as http:
        yield PCloudFiles(
            http, api_host=pcloud.host, account="4242", access_token=SecretStr(pcloud.access_token)
        )


async def _stage(
    files: PCloudFiles,
    staging: MemoryStaging,
    source: SourceObject,
    tmp_path: Path,
    *,
    budget: LocalScratchBudget | None = None,
) -> StageResult:
    """Stage `source` through the pipeline function."""
    return await stage_source(
        files,
        staging.writer,
        source,
        scratch_root=tmp_path / "fetch",
        budget=budget or LocalScratchBudget(10 * 1024 * 1024),
        publish_on_staging_loop=_direct,
    )


async def test_a_listed_file_is_staged_with_both_digests(
    files: PCloudFiles, pcloud: StubPCloud, staging: MemoryStaging, tmp_path: Path
) -> None:
    """The staged ref carries the SHA-256 and SHA-1 of exactly the listed bytes."""
    stub = pcloud.file("/doc.txt", b"hello world")
    result = await _stage(files, staging, _source(stub), tmp_path)

    assert result.outcome is StageOutcome.STAGED
    assert result.staged is not None
    assert result.staged.sha256 == hashlib.sha256(b"hello world").hexdigest()
    assert result.staged.sha1 == hashlib.sha1(b"hello world").hexdigest()
    assert staging.content(result.staged.key) == b"hello world"  # type: ignore[arg-type]
    assert list((tmp_path / "fetch").iterdir()) == []


async def test_bytes_whose_sha1_differs_from_the_listing_are_a_source_change(
    files: PCloudFiles, pcloud: StubPCloud, staging: MemoryStaging, tmp_path: Path
) -> None:
    """Same size, different content: rejected with both SHA-1 values, nothing left staged."""
    stub = pcloud.file("/doc.txt", b"version one")
    source = _source(stub)
    pcloud.modify(stub, b"version two")

    result = await _stage(files, staging, source, tmp_path)

    assert result.outcome is StageOutcome.SOURCE_CHANGED
    assert result.reason is not None
    assert hashlib.sha1(b"version one").hexdigest() in result.reason
    assert hashlib.sha1(b"version two").hexdigest() in result.reason
    assert staging.index.registrations == {}


async def test_a_file_that_grew_is_rejected_before_anything_is_uploaded(
    files: PCloudFiles, pcloud: StubPCloud, staging: MemoryStaging, tmp_path: Path
) -> None:
    """A size change is caught on scratch, and the fetch stops one byte past the listing."""
    stub = pcloud.file("/big.bin", b"a" * 10)
    source = _source(stub)
    pcloud.modify(stub, b"a" * 10_000)

    result = await _stage(files, staging, source, tmp_path)

    assert result.outcome is StageOutcome.SOURCE_CHANGED
    assert result.reason is not None
    assert "size listed 10, staged 11" in result.reason
    assert staging.store.uploads == 0


async def test_scratch_that_cannot_hold_the_file_is_an_explicit_capacity_error(
    files: PCloudFiles, pcloud: StubPCloud, staging: MemoryStaging, tmp_path: Path
) -> None:
    """The fetch refuses rather than filling the disk."""
    stub = pcloud.file("/big.bin", b"b" * 100)
    with pytest.raises(StagingCapacityExhaustedError):
        await _stage(files, staging, _source(stub), tmp_path, budget=LocalScratchBudget(50))
    assert pcloud.requests["getfilelink"] == 0


async def test_a_file_the_provider_cannot_deliver_raises_its_error(
    files: PCloudFiles, pcloud: StubPCloud, staging: MemoryStaging, tmp_path: Path
) -> None:
    """A provider refusal surfaces for the workflow to record as unreadable."""
    stub = pcloud.file("/gone.txt", b"x")
    pcloud.fail(
        "getfilelink", str(stub.file_id), Failure(times=1, body={"result": 2009, "error": "gone"})
    )
    with pytest.raises(ProviderRequestError):
        await _stage(files, staging, _source(stub), tmp_path)
    assert staging.index.registrations == {}


async def test_an_image_snapshot_is_sniffed_and_perceptually_fingerprinted(
    files: PCloudFiles, pcloud: StubPCloud, staging: MemoryStaging, tmp_path: Path
) -> None:
    """The CPU side reads a verified snapshot of exactly the staged version."""
    stub = pcloud.file("/photo.png", _png())
    result = await _stage(files, staging, _source(stub), tmp_path)
    assert result.staged is not None

    inspection = await inspect_staged(staging.reader.reader, result.staged)

    assert inspection.media_type == "image/png"
    assert inspection.perceptual is not None


async def test_a_non_image_has_no_perceptual_fingerprint(
    files: PCloudFiles, pcloud: StubPCloud, staging: MemoryStaging, tmp_path: Path
) -> None:
    """A PDF is sniffed and left without a perceptual fingerprint."""
    stub = pcloud.file("/doc.pdf", b"%PDF-1.7 body")
    result = await _stage(files, staging, _source(stub), tmp_path)
    assert result.staged is not None

    inspection = await inspect_staged(staging.reader.reader, result.staged)

    assert inspection.media_type == "application/pdf"
    assert inspection.perceptual is None


async def test_an_undecodable_image_keeps_its_type_without_a_perceptual_fingerprint(
    files: PCloudFiles, pcloud: StubPCloud, staging: MemoryStaging, tmp_path: Path
) -> None:
    """A truncated JPEG is noted rather than failing the strong fingerprint."""
    stub = pcloud.file("/broken.jpg", b"\xff\xd8\xff\xe0 truncated")
    result = await _stage(files, staging, _source(stub), tmp_path)
    assert result.staged is not None

    inspection = await inspect_staged(staging.reader.reader, result.staged)

    assert inspection.media_type == "image/jpeg"
    assert inspection.perceptual is None
    assert inspection.note is not None


async def test_an_image_over_the_decode_budget_keeps_its_type_with_a_note(
    files: PCloudFiles, pcloud: StubPCloud, staging: MemoryStaging, tmp_path: Path
) -> None:
    """Refused before decoding: no perceptual fingerprint, and the note says why."""
    stub = pcloud.file("/photo.png", _png())
    result = await _stage(files, staging, _source(stub), tmp_path)
    assert result.staged is not None

    inspection = await inspect_staged(staging.reader.reader, result.staged, max_pixels=10)

    assert inspection.media_type == "image/png"
    assert inspection.perceptual is None
    assert inspection.note is not None
    assert "decode budget is 10 pixels" in inspection.note


async def test_a_superseded_version_is_not_read_as_if_it_were_current(
    files: PCloudFiles, pcloud: StubPCloud, staging: MemoryStaging, tmp_path: Path
) -> None:
    """A snapshot is only handed over for the version it was asked for."""
    stub = pcloud.file("/doc.txt", b"one")
    first = await _stage(files, staging, _source(stub), tmp_path)
    pcloud.modify(stub, b"two")
    await _stage(files, staging, _source(stub), tmp_path)
    assert first.staged is not None

    with pytest.raises(StagedEntrySupersededError):
        await inspect_staged(staging.reader.reader, first.staged)


async def test_an_evicted_entry_is_missing_not_refetched(staging: MemoryStaging) -> None:
    """The CPU side has no provider to fall back on."""
    ref = StagedRef(key="a" * 32, sha256="0" * 64, sha1="0" * 40, size_bytes=1)
    with pytest.raises(StagedEntryMissingError):
        await inspect_staged(staging.reader.reader, ref)


def test_the_observed_marker_prefers_the_version_marker() -> None:
    """A marker identifies the version; without one, size and time do."""
    when = datetime(2026, 1, 1, tzinfo=UTC)
    with_marker = SourceObject.build(
        provider=Provider.PCLOUD,
        account="a",
        native_id="1",
        path="/x",
        size_bytes=3,
        modified_at=when,
        version_marker="77",
    )
    without = with_marker.model_copy(update={"version_marker": None})
    assert observed_marker(with_marker) == "v77"
    assert observed_marker(without) == f"s3-t{int(when.timestamp())}"
    assert is_current(with_marker, "v77")
    assert not is_current(with_marker, "v78")
    failed = with_marker.model_copy(
        update={"state": SourceObjectState.FAILED, "failure_reason": "x"}
    )
    assert not is_current(failed, "v77")
    assert not is_current(None, "v77")
