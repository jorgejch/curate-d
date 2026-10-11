"""Scans under DBOS against the ledger and a stub pCloud.

This process dequeues only the scan queue, so the ingests a scan hands off stay enqueued
and the scan's own provider traffic can be counted on its own.
"""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from dbos import SetWorkflowID
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.config import Settings
from curate_d.domain import HashAlgorithm, Provider, ScanStatus, SourceObjectState
from curate_d.inventory.decisions import FileAction
from curate_d.inventory.scans import ScanRepository
from curate_d.runtime.queues import QueueName
from curate_d.workflows.scan import ScanOutcome, ScanReport, scan_root
from tests.integration.pipeline_harness import Pipeline, running_pipeline
from tests.stubs.pcloud import Failure

pytestmark = pytest.mark.integration


@pytest.fixture
async def pipeline(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> AsyncIterator[Pipeline]:
    """A pipeline dequeuing scans only."""
    async with running_pipeline(
        settings, ledger_engine, tmp_path, listen=(QueueName.SCAN.value,)
    ) as running:
        yield running


def _tree(pipeline: Pipeline) -> None:
    """A nested library: files at three depths and an empty folder."""
    tag = pipeline.account.encode()
    pipeline.pcloud.file("/Library/readme.txt", b"readme " + tag)
    pipeline.pcloud.file("/Library/Photos/2025/a.jpg", b"\xff\xd8\xff a " + tag)
    pipeline.pcloud.file("/Library/Photos/2025/b.jpg", b"\xff\xd8\xff b " + tag)
    pipeline.pcloud.file("/Library/Photos/2026/c.jpg", b"\xff\xd8\xff c " + tag)
    pipeline.pcloud.file("/Library/Docs/tax.pdf", b"%PDF " + tag)
    pipeline.pcloud.folder("/Library/Empty")


async def _scan(pipeline: Pipeline, root: str = "/Library") -> ScanReport:
    """Run one scan of `root` to its end under a fresh workflow id."""
    cycle = uuid4().hex[:8]
    with SetWorkflowID(f"test-scan-{pipeline.account}-{cycle}"):
        return await scan_root(Provider.PCLOUD.value, pipeline.account, root, cycle)


async def test_a_scan_records_every_file_with_digests_without_reading_content(
    pipeline: Pipeline,
) -> None:
    """Every file in the nested tree becomes a source object carrying reported digests."""
    _tree(pipeline)

    report = await _scan(pipeline)

    assert report.outcome is ScanOutcome.COMPLETE
    sources = await pipeline.sources()
    assert [s.path for s in sources] == [
        "/Library/Docs/tax.pdf",
        "/Library/Photos/2025/a.jpg",
        "/Library/Photos/2025/b.jpg",
        "/Library/Photos/2026/c.jpg",
        "/Library/readme.txt",
    ]
    for source in sources:
        assert source.native_id
        assert source.version_marker is not None
        assert source.reported(HashAlgorithm.SHA1) is not None
        assert source.reported(HashAlgorithm.SHA256) is not None
        assert source.state is SourceObjectState.DISCOVERED
    assert pipeline.pcloud.requests["content"] == 0
    assert pipeline.pcloud.requests["getfilelink"] == 0


async def test_new_files_become_downstream_work(pipeline: Pipeline) -> None:
    """Each new file has an ingest enqueued for its version."""
    _tree(pipeline)
    await _scan(pipeline)

    sources = await pipeline.sources()
    ingests = await pipeline.workflows("ingest:")
    enqueued = {workflow_id.split(":")[1] for workflow_id, _ in ingests}
    assert {str(s.id) for s in sources} <= enqueued


async def test_an_unchanged_library_is_skipped_without_reads_or_work(pipeline: Pipeline) -> None:
    """A second scan reads no digests, no content, and creates no work."""
    _tree(pipeline)
    await _scan(pipeline)
    checksums = pipeline.pcloud.requests["checksumfile"]
    ingests = await pipeline.workflows("ingest:")

    report = await _scan(pipeline)

    assert report.actions == {FileAction.UNCHANGED: 5}
    assert pipeline.pcloud.requests["checksumfile"] == checksums
    assert pipeline.pcloud.requests["content"] == 0
    assert await pipeline.workflows("ingest:") == ingests


async def test_a_modified_file_is_rerecorded_and_produces_work(pipeline: Pipeline) -> None:
    """A new marker updates the source object and enqueues an ingest of the new version."""
    _tree(pipeline)
    await _scan(pipeline)
    before = {s.path: s for s in await pipeline.sources()}
    stub = next(f for f in pipeline.pcloud.files.values() if f.name == "tax.pdf")
    pipeline.pcloud.modify(stub, b"%PDF amended " + pipeline.account.encode())

    report = await _scan(pipeline)

    assert report.actions.get(FileAction.CHANGED) == 1
    after = {s.path: s for s in await pipeline.sources()}
    changed = after["/Library/Docs/tax.pdf"]
    assert changed.id == before["/Library/Docs/tax.pdf"].id
    assert changed.version_marker != before["/Library/Docs/tax.pdf"].version_marker
    assert changed.reported_digests != before["/Library/Docs/tax.pdf"].reported_digests
    ingests = [wid for wid, _ in await pipeline.workflows(f"ingest:{changed.id}:")]
    assert len(ingests) == 2


async def test_a_renamed_file_updates_its_existing_source_object(pipeline: Pipeline) -> None:
    """Same provider id at a new path: one row, new path, no new work."""
    _tree(pipeline)
    await _scan(pipeline)
    stub = next(f for f in pipeline.pcloud.files.values() if f.name == "readme.txt")
    original = next(s for s in await pipeline.sources() if s.path == "/Library/readme.txt")
    pipeline.pcloud.rename(stub, "README.md")

    report = await _scan(pipeline)

    assert report.actions.get(FileAction.RENAMED) == 1
    renamed = next(s for s in await pipeline.sources() if s.native_id == original.native_id)
    assert renamed.id == original.id
    assert renamed.path == "/Library/README.md"
    assert len(await pipeline.sources()) == 5
    assert len(await pipeline.workflows(f"ingest:{original.id}:")) == 1


async def test_an_interrupted_scan_resumes_without_relisting_confirmed_folders(
    pipeline: Pipeline, ledger_engine: AsyncEngine
) -> None:
    """A provider failure leaves the scan incomplete; the next scan lists only the rest."""
    _tree(pipeline)
    photos = pipeline.pcloud.folder_id_at("/Library/Photos")
    assert photos is not None
    pipeline.pcloud.fail("listfolder", str(photos), Failure(times=100, status=503))

    first = await _scan(pipeline)

    assert first.outcome is ScanOutcome.INCOMPLETE
    assert first.reason is not None and "HTTP 503" in first.reason
    recorded_before = {s.path for s in await pipeline.sources()}
    assert "/Library/readme.txt" in recorded_before
    async with ledger_engine.connect() as connection:
        scan = await ScanRepository(connection).get(first.scan_id)  # type: ignore[arg-type]
    assert scan is not None
    assert scan.status is ScanStatus.INCOMPLETE
    assert scan.failure_reason is not None

    pipeline.pcloud.failures.clear()
    listed_before = pipeline.pcloud.requests["listfolder"]
    second = await _scan(pipeline)

    assert second.outcome is ScanOutcome.COMPLETE
    assert second.scan_id == first.scan_id
    assert len(await pipeline.sources()) == 5
    # The first run confirmed Library, Docs and Empty; the second lists only Photos and
    # its two subfolders, plus the one call that resolves the root path.
    assert first.folders_listed == 3
    assert second.folders_listed == 3
    assert pipeline.pcloud.requests["listfolder"] - listed_before == 1 + 3


async def test_a_folder_deleted_mid_scan_is_dropped_and_later_scans_progress(
    pipeline: Pipeline, ledger_engine: AsyncEngine
) -> None:
    """A pending folder that no longer exists does not hold the frontier back forever."""
    _tree(pipeline)
    first_year = pipeline.pcloud.folder_id_at("/Library/Photos/2025")
    assert first_year is not None
    pipeline.pcloud.fail("listfolder", str(first_year), Failure(times=100, status=503))
    first = await _scan(pipeline)
    assert first.outcome is ScanOutcome.INCOMPLETE

    pipeline.pcloud.failures.clear()
    pipeline.pcloud.remove_folder("/Library/Photos/2025")
    pipeline.pcloud.file("/Library/later.txt", b"later " + pipeline.account.encode())
    resumed = await _scan(pipeline)

    assert resumed.outcome is ScanOutcome.COMPLETE
    assert resumed.scan_id == first.scan_id
    async with ledger_engine.connect() as connection:
        scan = await ScanRepository(connection).get(first.scan_id)  # type: ignore[arg-type]
    assert scan is not None
    assert scan.status is ScanStatus.COMPLETE

    fresh = await _scan(pipeline)
    assert fresh.outcome is ScanOutcome.COMPLETE
    assert fresh.scan_id != first.scan_id
    assert "/Library/later.txt" in {s.path for s in await pipeline.sources()}


async def test_a_long_scan_hands_over_to_continuations(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> None:
    """With two folders per run, the rest of the frontier is scanned by continuations."""
    async with running_pipeline(
        settings,
        ledger_engine,
        tmp_path,
        listen=(QueueName.SCAN.value,),
        scan_folders_per_run=2,
    ) as pipeline:
        _tree(pipeline)
        report = await _scan(pipeline)
        assert report.outcome is ScanOutcome.CONTINUED
        await pipeline.settle()
        assert len(await pipeline.sources()) == 5
        continuations = await pipeline.workflows(f"scan:pcloud:{pipeline.account}:")
        assert continuations
        assert all(status == "SUCCESS" for _, status in continuations)


async def test_a_rate_limited_scan_backs_off_and_completes(pipeline: Pipeline) -> None:
    """Two 429s on a folder are waited out; the scan still finishes."""
    _tree(pipeline)
    docs = pipeline.pcloud.folder_id_at("/Library/Docs")
    assert docs is not None
    pipeline.pcloud.fail(
        "listfolder", str(docs), Failure(times=2, status=429, headers={"retry-after": "0"})
    )

    report = await _scan(pipeline)

    assert report.outcome is ScanOutcome.COMPLETE
    assert len(await pipeline.sources()) == 5
