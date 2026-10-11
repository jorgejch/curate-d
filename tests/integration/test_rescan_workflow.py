"""Full rescans under DBOS: re-reading content whatever the version marker says."""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from dbos import SetWorkflowID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.config import Settings
from curate_d.domain import AuditEntryType, Provider, SourceObjectState
from curate_d.inventory.rescan import RescanVerdict
from curate_d.ledger.mapping import to_audit_entry
from curate_d.ledger.tables import audit_entries
from curate_d.workflows.rescan import RescanOutcome, RescanReport, full_rescan
from curate_d.workflows.scan import scan_root
from tests.integration.pipeline_harness import Pipeline, running_pipeline
from tests.stubs.pcloud import Failure

pytestmark = pytest.mark.integration


@pytest.fixture
async def pipeline(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path
) -> AsyncIterator[Pipeline]:
    """A pipeline running every queue, with a library already scanned and ingested."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as running:
        tag = running.account.encode()
        for name in ("a", "b", "c", "d"):
            running.pcloud.file(f"/lib/{name}.txt", f"{name} ".encode() + tag)
        cycle = uuid4().hex[:8]
        with SetWorkflowID(f"test-scan-{running.account}-{cycle}"):
            await scan_root(Provider.PCLOUD.value, running.account, "/", cycle)
        await running.settle()
        yield running


async def _rescan(pipeline: Pipeline) -> RescanReport:
    """One full-rescan run under a fresh cycle."""
    cycle = uuid4().hex[:8]
    with SetWorkflowID(f"test-rescan-{pipeline.account}-{cycle}"):
        return await full_rescan(Provider.PCLOUD.value, pipeline.account, cycle)


async def test_unchanged_files_are_confirmed_and_stamped(pipeline: Pipeline) -> None:
    """Every file is re-read, matches, gets a verification time, and creates no work."""
    ingests = await pipeline.workflows("ingest:")
    reads = pipeline.pcloud.requests["content"]

    report = await _rescan(pipeline)

    assert report.outcome is RescanOutcome.COMPLETE
    assert report.verdicts == {RescanVerdict.CONFIRMED: 4}
    assert pipeline.pcloud.requests["content"] - reads == 4
    assert all(s.last_verified_at is not None for s in await pipeline.sources())
    assert await pipeline.workflows("ingest:") == ingests


async def test_a_change_behind_an_unchanged_marker_is_detected_and_recorded(
    pipeline: Pipeline, ledger_engine: AsyncEngine
) -> None:
    """The discrepancy is audited, the source re-ingested, and the new content recorded."""
    before = {s.path: s for s in await pipeline.sources()}
    stub = next(f for f in pipeline.pcloud.files.values() if f.name == "b.txt")
    pipeline.pcloud.change_silently(stub, b"B changed " + pipeline.account.encode())

    report = await _rescan(pipeline)
    await pipeline.settle()

    assert report.verdicts == {RescanVerdict.CONFIRMED: 3, RescanVerdict.DISCREPANCY: 1}
    after = {s.path: s for s in await pipeline.sources()}
    changed = after["/lib/b.txt"]
    assert changed.version_marker == before["/lib/b.txt"].version_marker
    assert changed.state is SourceObjectState.FINGERPRINTED
    assert changed.content_object_id != before["/lib/b.txt"].content_object_id
    async with ledger_engine.connect() as connection:
        rows = await connection.execute(
            select(audit_entries).where(
                audit_entries.c.source_object_id == changed.id,
                audit_entries.c.entry_type == AuditEntryType.SOURCE_DISCREPANCY_DETECTED.value,
            )
        )
        [entry] = [to_audit_entry(row) for row in rows.mappings()]
    assert entry.details["version_marker"] == changed.version_marker
    assert entry.details["recorded_fingerprint"] != entry.details["observed_fingerprint"]


async def test_an_interrupted_rescan_resumes_rather_than_restarting(pipeline: Pipeline) -> None:
    """Files re-read before the failure keep their stamps and are not read again."""
    third = next(f for f in pipeline.pcloud.files.values() if f.name == "c.txt")
    pipeline.pcloud.fail("getfilelink", str(third.file_id), Failure(times=100, status=503))
    reads = pipeline.pcloud.requests["content"]

    first = await _rescan(pipeline)

    assert first.outcome is RescanOutcome.INCOMPLETE
    assert pipeline.pcloud.requests["content"] - reads == 2
    stamped = {s.path for s in await pipeline.sources() if s.last_verified_at is not None}
    assert stamped == {"/lib/a.txt", "/lib/b.txt"}

    pipeline.pcloud.failures.clear()
    reads = pipeline.pcloud.requests["content"]
    second = await _rescan(pipeline)

    assert second.outcome is RescanOutcome.COMPLETE
    assert second.verdicts == {RescanVerdict.CONFIRMED: 2}
    assert pipeline.pcloud.requests["content"] - reads == 2


async def test_a_file_the_provider_no_longer_serves_is_marked_failed(
    pipeline: Pipeline,
) -> None:
    """A vanished file is recorded as failed and the pass moves on past it."""
    gone = next(f for f in pipeline.pcloud.files.values() if f.name == "a.txt")
    pipeline.pcloud.fail(
        "getfilelink", str(gone.file_id), Failure(times=100, body={"result": 2009, "error": "x"})
    )

    report = await _rescan(pipeline)

    assert report.outcome is RescanOutcome.COMPLETE
    assert report.verdicts == {RescanVerdict.CONFIRMED: 3}
    failed = next(s for s in await pipeline.sources() if s.path == "/lib/a.txt")
    assert failed.state is SourceObjectState.FAILED
    assert failed.last_verified_at is not None
