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
from sqlalchemy import select, text
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


async def _operations(engine: AsyncEngine, workflow_id: str) -> list[str]:
    """Return the names of the steps a workflow recorded, in order."""
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT function_name FROM dbos.operation_outputs "
                "WHERE workflow_uuid = :id ORDER BY function_id"
            ),
            {"id": workflow_id},
        )
        return [row[0] for row in rows]


async def _ingest_too_large(
    pipeline: Pipeline, engine: AsyncEngine, content: bytes, expected_bound: str
) -> None:
    """Ingest one file the bounds refuse, and check it failed at once, unread."""
    pipeline.pcloud.file("/big.bin", content)
    await _scan(pipeline)
    [source] = await pipeline.sources()
    reads = pipeline.pcloud.requests["getfilelink"] + pipeline.pcloud.requests["content"]
    workflow_id = f"test-ingest-{uuid4()}"

    with SetWorkflowID(workflow_id):
        outcome = await ingest_source(source.id, observed_marker(source))

    assert outcome is IngestOutcome.TOO_LARGE
    assert not any("sleep" in name for name in await _operations(engine, workflow_id))
    [failed] = await pipeline.sources()
    assert failed.state is SourceObjectState.FAILED
    assert failed.content_object_id is None
    assert failed.failure_reason is not None
    assert f"file is {len(content)} bytes" in failed.failure_reason
    assert expected_bound in failed.failure_reason
    assert AuditEntryType.SOURCE_READ_FAILED in await _audit(engine, failed)
    assert pipeline.pcloud.requests["getfilelink"] + pipeline.pcloud.requests["content"] == reads
    assert pipeline.staging.index.registrations == {}


async def test_a_file_larger_than_staging_fails_at_once_without_a_provider_read(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A file staging can never hold is refused before any read, with no backoff."""
    async with running_pipeline(
        settings, ledger_engine, tmp_path, listen=(QueueName.SCAN.value,), staging_max_bytes=16
    ) as pipeline:
        await _ingest_too_large(
            pipeline,
            ledger_engine,
            b"far more than sixteen bytes " + pipeline.account.encode(),
            "the bound is 16 bytes (staging_max_bytes)",
        )


async def test_a_file_over_the_per_file_ceiling_fails_at_once(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    """`ingest_max_bytes` refuses a file that staging could hold."""
    async with running_pipeline(
        settings, ledger_engine, tmp_path, listen=(QueueName.SCAN.value,), ingest_max_bytes=12
    ) as pipeline:
        await _ingest_too_large(
            pipeline,
            ledger_engine,
            b"more than twelve bytes " + pipeline.account.encode(),
            "the bound is 12 bytes (ingest_max_bytes)",
        )


async def test_content_staging_reports_will_never_fit_fails_without_waiting(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    """A never-fit refusal from the publish path ends the ingest at once, unretried."""
    async with running_pipeline(
        settings, ledger_engine, tmp_path, listen=(QueueName.SCAN.value,), staging_bytes=8
    ) as pipeline:
        pipeline.pcloud.file("/big.bin", b"far more than eight bytes " + pipeline.account.encode())
        await _scan(pipeline)
        [source] = await pipeline.sources()
        workflow_id = f"test-ingest-{uuid4()}"

        with SetWorkflowID(workflow_id):
            outcome = await ingest_source(source.id, observed_marker(source))

        assert outcome is IngestOutcome.TOO_LARGE
        assert not any("sleep" in name for name in await _operations(ledger_engine, workflow_id))
        [failed] = await pipeline.sources()
        assert failed.state is SourceObjectState.FAILED
        assert failed.failure_reason is not None
        assert "will never fit" in failed.failure_reason
        assert AuditEntryType.SOURCE_READ_FAILED in await _audit(ledger_engine, failed)


async def test_staging_that_stays_full_defers_the_file_with_a_reason(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    """Staging full of entries in use: waits durably, then gives up explicitly."""
    first = b"first file, in use " + b"x" * 40
    second = b"second file, waits " + b"y" * 40
    async with running_pipeline(
        settings,
        ledger_engine,
        tmp_path,
        listen=(QueueName.SCAN.value,),
        staging_bytes=len(first) + len(second) - 1,
    ) as pipeline:
        pipeline.pcloud.file("/a/first.bin", first)
        pipeline.pcloud.file("/b/second.bin", second)
        await _scan(pipeline)
        sources = {s.path: s for s in await pipeline.sources()}
        with SetWorkflowID(f"test-ingest-{uuid4()}"):
            staged = await ingest_source(
                sources["/a/first.bin"].id, observed_marker(sources["/a/first.bin"])
            )
        assert staged is IngestOutcome.STAGED

        async def in_use(key: object) -> None:
            """Refuse every eviction, as for an entry being downloaded."""
            return None

        pipeline.staging.index.evict = in_use  # type: ignore[method-assign]
        workflow_id = f"test-ingest-{uuid4()}"
        with SetWorkflowID(workflow_id):
            outcome = await ingest_source(
                sources["/b/second.bin"].id, observed_marker(sources["/b/second.bin"])
            )

        assert outcome is IngestOutcome.DEFERRED
        assert any("sleep" in name for name in await _operations(ledger_engine, workflow_id))
        waiting = {s.path: s for s in await pipeline.sources()}["/b/second.bin"]
        assert waiting.state is SourceObjectState.FAILED
        assert waiting.failure_reason is not None
        assert "capacity" in waiting.failure_reason


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
            ),
        )

    assert outcome is FingerprintOutcome.UNREADABLE
    [failed] = await scan_only.sources()
    assert failed.state is SourceObjectState.FAILED
    assert failed.content_object_id is None
    assert AuditEntryType.SOURCE_READ_FAILED in await _audit(ledger_engine, failed)
