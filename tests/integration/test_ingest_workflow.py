"""Ingest under DBOS: from a scanned file to a fingerprinted content object.

Most tests run every queue in this process and let the scan's hand-offs run to the end.
The ones that need a moment between two stages run the scan queue only and invoke the
next workflow directly.
"""

import hashlib
import io
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from dbos import SetWorkflowID
from PIL import Image, ImageDraw
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.config import Settings
from curate_d.domain import (
    AuditEntryType,
    HashAlgorithm,
    Provider,
    SourceObject,
    SourceObjectState,
)
from curate_d.ingest.pipeline import StagedRef, observed_marker
from curate_d.ledger.mapping import to_audit_entry
from curate_d.ledger.tables import audit_entries
from curate_d.runtime.queues import QueueName
from curate_d.staging.keys import derive_staging_key
from curate_d.workflows.ingest import (
    FingerprintOutcome,
    IngestOutcome,
    fingerprint_source,
    ingest_source,
)
from curate_d.workflows.scan import scan_root
from tests.integration.pipeline_harness import Pipeline, running_pipeline
from tests.stubs.pcloud import Failure

pytestmark = pytest.mark.integration


def _png(seed: int) -> bytes:
    """A small, distinct picture."""
    image = Image.new("RGB", (96, 64), (seed * 40 % 255, 30, 60))
    ImageDraw.Draw(image).ellipse((seed * 7 % 50, 5, 60, 50), fill=(220, 180, 20))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def _scan(pipeline: Pipeline, root: str = "/") -> None:
    """Scan `root` under a fresh workflow id."""
    cycle = uuid4().hex[:8]
    with SetWorkflowID(f"test-scan-{pipeline.account}-{cycle}"):
        await scan_root(Provider.PCLOUD.value, pipeline.account, root, cycle)


async def _audit(engine: AsyncEngine, source: SourceObject) -> list[AuditEntryType]:
    """Audit entry types recorded against a source."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            select(audit_entries)
            .where(audit_entries.c.source_object_id == source.id)
            .order_by(audit_entries.c.id)
        )
        return [to_audit_entry(row).entry_type for row in rows.mappings()]


@pytest.fixture
async def pipeline(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> AsyncIterator[Pipeline]:
    """A pipeline running every queue."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as running:
        yield running


@pytest.fixture
async def scan_only(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> AsyncIterator[Pipeline]:
    """A pipeline that dequeues scans and nothing else."""
    async with running_pipeline(
        settings, ledger_engine, tmp_path, listen=(QueueName.SCAN.value,)
    ) as running:
        yield running


async def test_scanned_files_end_as_fingerprinted_content(pipeline: Pipeline) -> None:
    """Each file's content object carries the staged SHA-256, a sniffed type, and for
    images a perceptual fingerprint."""
    tag = pipeline.account.encode()
    photo = _png(int(pipeline.account) % 7)
    pipeline.pcloud.file("/p/photo.png", photo)
    pipeline.pcloud.file("/d/notes.txt", b"notes " + tag)

    await _scan(pipeline)
    await pipeline.settle()

    sources = {s.path: s for s in await pipeline.sources()}
    assert all(s.state is SourceObjectState.FINGERPRINTED for s in sources.values())
    contents = {c.id: c for c in await pipeline.contents()}
    image = contents[sources["/p/photo.png"].content_object_id]  # type: ignore[index]
    text = contents[sources["/d/notes.txt"].content_object_id]  # type: ignore[index]
    assert image.fingerprint.algorithm is HashAlgorithm.SHA256
    assert image.fingerprint.digest == hashlib.sha256(photo).hexdigest()
    assert image.media_type == "image/png"
    assert image.perceptual_fingerprint is not None
    assert text.media_type == "text/plain"
    assert text.perceptual_fingerprint is None


async def test_identical_content_at_two_paths_is_one_content_object(pipeline: Pipeline) -> None:
    """Two sources, one set of bytes, one content object."""
    same = b"duplicate " + pipeline.account.encode()
    pipeline.pcloud.file("/a/one.txt", same)
    pipeline.pcloud.file("/b/two.txt", same)

    await _scan(pipeline)
    await pipeline.settle()

    sources = await pipeline.sources()
    assert len(sources) == 2
    assert sources[0].content_object_id == sources[1].content_object_id
    assert len(await pipeline.contents()) == 1


async def test_consumed_staged_versions_leave_staging(pipeline: Pipeline) -> None:
    """Once fingerprinting and classification have read them, no staged bytes remain.

    Two of the three files share content, so classification reads one copy and the
    other is released by its fingerprinting alone.
    """
    tag = pipeline.account.encode()
    pipeline.pcloud.file("/a/one.txt", b"shared " + tag)
    pipeline.pcloud.file("/b/two.txt", b"shared " + tag)
    pipeline.pcloud.file("/c/own.txt", b"own " + tag)

    await _scan(pipeline)
    await pipeline.settle()

    sources = await pipeline.sources()
    assert all(s.state is SourceObjectState.FINGERPRINTED for s in sources)
    assert pipeline.staging.index.registrations == {}
    assert pipeline.staging.store.objects == {}
    for source in sources:
        released = await pipeline.workflows(f"release-staged:{derive_staging_key(source.id)}:")
        assert [status for _, status in released] == ["SUCCESS"], source.path


async def test_the_copy_classification_reads_waits_for_classification(
    scan_only: Pipeline,
) -> None:
    """A second source of the same content is released at once; the first copy is not."""
    same = b"twice " + scan_only.account.encode()
    scan_only.pcloud.file("/a/one.txt", same)
    scan_only.pcloud.file("/b/two.txt", same)
    await _scan(scan_only)
    first, second = await scan_only.sources()

    for source in (first, second):
        observed = observed_marker(source)
        with SetWorkflowID(f"test-ingest-{uuid4()}"):
            assert await ingest_source(source.id, observed) is IngestOutcome.STAGED
        key = derive_staging_key(source.id)
        registration = scan_only.staging.index.registrations[key]
        with SetWorkflowID(f"test-fingerprint-{uuid4()}"):
            outcome = await fingerprint_source(
                source.id,
                observed,
                StagedRef(
                    key=key,
                    sha256=registration.sha256,
                    sha1=hashlib.sha1(same).hexdigest(),
                    size_bytes=registration.size_bytes,
                    attempt_id=registration.attempt_id,
                ),
            )
        assert outcome is FingerprintOutcome.FINGERPRINTED

    first_key, second_key = derive_staging_key(first.id), derive_staging_key(second.id)
    assert await scan_only.workflows(f"release-staged:{first_key}:") == []
    assert first_key in scan_only.staging.index.registrations
    second_attempt = scan_only.staging.index.registrations[second_key].attempt_id
    assert await scan_only.workflows(f"release-staged:{second_key}:") == [
        (f"release-staged:{second_key}:{second_attempt.hex}", "ENQUEUED")
    ]


async def test_an_unreadable_file_fails_alone_without_content(
    pipeline: Pipeline, ledger_engine: AsyncEngine
) -> None:
    """A file the provider cannot deliver is marked failed; the rest are fingerprinted."""
    tag = pipeline.account.encode()
    gone = pipeline.pcloud.file("/gone.txt", b"gone " + tag)
    pipeline.pcloud.file("/fine.txt", b"fine " + tag)
    pipeline.pcloud.fail(
        "getfilelink",
        str(gone.file_id),
        Failure(times=100, body={"result": 2009, "error": "File not found."}),
    )

    await _scan(pipeline)
    await pipeline.settle()

    sources = {s.path: s for s in await pipeline.sources()}
    failed = sources["/gone.txt"]
    assert failed.state is SourceObjectState.FAILED
    assert failed.content_object_id is None
    assert failed.failure_reason is not None and "2009" in failed.failure_reason
    assert sources["/fine.txt"].state is SourceObjectState.FINGERPRINTED
    assert AuditEntryType.SOURCE_READ_FAILED in await _audit(ledger_engine, failed)


async def test_a_file_changed_after_listing_is_rejected_as_a_source_change(
    scan_only: Pipeline, ledger_engine: AsyncEngine
) -> None:
    """Same size, new bytes: the SHA-1 no longer matches the listing."""
    stub = scan_only.pcloud.file("/doc.txt", b"version one " + scan_only.account.encode())
    await _scan(scan_only)
    [source] = await scan_only.sources()
    scan_only.pcloud.change_silently(stub, b"version two " + scan_only.account.encode())

    with SetWorkflowID(f"test-ingest-{uuid4()}"):
        outcome = await ingest_source(source.id, observed_marker(source))

    assert outcome is IngestOutcome.SOURCE_CHANGED
    [failed] = await scan_only.sources()
    assert failed.state is SourceObjectState.FAILED
    assert failed.content_object_id is None
    assert failed.failure_reason is not None and "sha1 reported" in failed.failure_reason
    assert AuditEntryType.SOURCE_DISCREPANCY_DETECTED in await _audit(ledger_engine, failed)
    assert scan_only.staging.index.registrations == {}


async def test_an_entry_evicted_before_fingerprinting_is_restaged(scan_only: Pipeline) -> None:
    """The CPU side cannot fetch; it asks the I/O side to stage the version again."""
    scan_only.pcloud.file("/doc.txt", b"evicted " + scan_only.account.encode())
    await _scan(scan_only)
    [source] = await scan_only.sources()
    observed = observed_marker(source)
    with SetWorkflowID(f"test-ingest-{uuid4()}"):
        assert await ingest_source(source.id, observed) is IngestOutcome.STAGED
    assert len(await scan_only.workflows(f"fingerprint:{source.id}:")) == 1
    key = next(iter(scan_only.staging.index.registrations))
    registration = scan_only.staging.index.registrations[key]
    scan_only.staging.index.registrations.clear()

    with SetWorkflowID(f"test-fingerprint-{uuid4()}"):
        outcome = await fingerprint_source(
            source.id,
            observed,
            StagedRef(
                key=key,
                sha256=registration.sha256,
                sha1="0" * 40,
                size_bytes=registration.size_bytes,
            ),
        )

    assert outcome is FingerprintOutcome.RESTAGED
    restage = await scan_only.workflows(f"ingest:{source.id}:{observed}#1")
    assert len(restage) == 1


async def test_staging_that_stays_full_defers_the_file_with_a_reason(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Waits durably, then gives up explicitly rather than hanging."""
    async with running_pipeline(settings, ledger_engine, tmp_path, staging_bytes=8) as pipeline:
        pipeline.pcloud.file("/big.bin", b"far more than eight bytes " + pipeline.account.encode())
        await _scan(pipeline)
        await pipeline.settle()
        [source] = await pipeline.sources()
        assert source.state is SourceObjectState.FAILED
        assert source.failure_reason is not None
        assert "capacity" in source.failure_reason


async def test_a_snapshot_failing_verification_marks_the_source_unreadable(
    scan_only: Pipeline, ledger_engine: AsyncEngine
) -> None:
    """Bytes that no longer match their registration are refused, not fingerprinted."""
    scan_only.pcloud.file("/doc.txt", b"intact " + scan_only.account.encode())
    await _scan(scan_only)
    [source] = await scan_only.sources()
    observed = observed_marker(source)
    with SetWorkflowID(f"test-ingest-{uuid4()}"):
        assert await ingest_source(source.id, observed) is IngestOutcome.STAGED
    key, registration = next(iter(scan_only.staging.index.registrations.items()))
    staged, content = scan_only.staging.store.objects[registration.object_key]
    scan_only.staging.store.objects[registration.object_key] = (staged, b"X" + content[1:])

    with SetWorkflowID(f"test-fingerprint-{uuid4()}"):
        outcome = await fingerprint_source(
            source.id,
            observed,
            StagedRef(
                key=key,
                sha256=registration.sha256,
                sha1="0" * 40,
                size_bytes=registration.size_bytes,
                attempt_id=registration.attempt_id,
            ),
        )

    assert outcome is FingerprintOutcome.UNREADABLE
    [failed] = await scan_only.sources()
    assert failed.state is SourceObjectState.FAILED
    assert failed.content_object_id is None
    assert AuditEntryType.SOURCE_READ_FAILED in await _audit(ledger_engine, failed)
    # Nothing can use the bytes, so they are handed to the I/O profile to release.
    assert await scan_only.workflows(f"release-staged:{key}:") == [
        (f"release-staged:{key}:{registration.attempt_id.hex}", "ENQUEUED")
    ]
