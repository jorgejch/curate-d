"""Observability endpoints over the ledger and the workflow system database.

Other tests leave rows behind, so counts are compared before and after this test adds
its own; failures are looked for by identity.
"""

import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from dbos import DBOS, SetWorkflowID
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.app import create_app
from curate_d.api.backends import Backends, ledger_backends
from curate_d.api.jobs import LedgerJobs
from curate_d.classification.store import ReviewRepository, SpendRepository
from curate_d.config import Settings
from curate_d.domain import (
    ContentObject,
    DestinationConfig,
    HashAlgorithm,
    PendingFolder,
    Placement,
    PlacementState,
    Provider,
    ReviewReason,
    SourceObject,
    SourceObjectState,
    StrongFingerprint,
)
from curate_d.inventory.scans import ScanRepository
from curate_d.ledger import (
    ContentObjectRepository,
    DestinationRepository,
    PlacementRepository,
    SourceObjectRepository,
)
from curate_d.ledger.tables import (
    content_objects,
    placements,
    remote_spend,
    review_items,
    scan_folders,
    scans,
    source_objects,
)
from curate_d.runtime import dbos_app
from curate_d.runtime.queues import QueueName
from tests.integration.pipeline_harness import running_pipeline

pytestmark = pytest.mark.integration

_NOW = datetime(2099, 1, 15, 12, tzinfo=UTC)
_PERIOD = "2099-01"


@DBOS.workflow()
async def failing_for_observability(marker: str) -> None:
    """Fail with `marker` in the message."""
    raise ValueError(f"observability probe {marker}")


@pytest.fixture
def http(settings: Settings) -> Iterator[TestClient]:
    """The app over the ledger, its clock in January 2099."""

    @asynccontextmanager
    async def factory(chosen: Settings, engine: AsyncEngine) -> AsyncGenerator[Backends, None]:
        """Ledger backends with jobs reading the spend of the test's month."""
        async with ledger_backends(chosen, engine) as wired:
            workflows = dbos_app.client(chosen)
            try:
                yield replace(wired, jobs=LedgerJobs(chosen, engine, workflows, clock=lambda: _NOW))
            finally:
                workflows.destroy()

    with TestClient(create_app(settings, backends_factory=factory)) as client:
        yield client


@asynccontextmanager
async def _failed_units(ledger_engine: AsyncEngine) -> AsyncGenerator[dict[str, uuid.UUID], None]:
    """An incomplete scan, an unreadable source, a failed placement, an item in review,
    and January 2099's spend cap reached; removed afterwards."""
    tag = uuid.uuid4().hex[:8]
    account = f"jobs-{tag}"
    async with ledger_engine.begin() as connection:
        scan, _ = await ScanRepository(connection).open(
            Provider.PCLOUD, account, PendingFolder(folder_id="1", path=f"/{tag}"), now=_NOW
        )
        await ScanRepository(connection).mark_incomplete(scan.id, f"HTTP 503 {tag}")
        source = await SourceObjectRepository(connection).upsert(
            SourceObject.build(
                provider=Provider.PCLOUD,
                account=account,
                native_id="f-1",
                path=f"/{tag}/broken.jpg",
                size_bytes=1,
                modified_at=_NOW,
            ).model_copy(
                update={"state": SourceObjectState.FAILED, "failure_reason": f"unreadable {tag}"}
            )
        )
        contents = ContentObjectRepository(connection)
        placed, reviewed = [
            await contents.upsert(
                ContentObject.build(
                    fingerprint=StrongFingerprint(
                        algorithm=HashAlgorithm.SHA256, digest=uuid.uuid4().hex * 2
                    ),
                    size_bytes=1,
                    media_type="image/jpeg",
                )
            )
            for _ in range(2)
        ]
        library = DestinationConfig(
            name=f"lib-{tag}", provider=Provider.PCLOUD, account=f"acct-{tag}", root_path="/lib"
        )
        await DestinationRepository(connection).ensure(library.identity)
        placement = await PlacementRepository(connection).upsert(
            Placement.build(
                content_object_id=placed.id,
                destination_id=library.id,
                destination_name=library.name,
                locator=f"/lib-{tag}/x.jpg",
                template_version="seed-1",
            ).model_copy(
                update={"state": PlacementState.FAILED, "failure_reason": f"copy timed out {tag}"}
            )
        )
        await ReviewRepository(connection).open_item(
            reviewed.id, ReviewReason.LOW_CONFIDENCE, f"no tier was sure {tag}", (), now=_NOW
        )
        assert not await SpendRepository(connection).reserve(
            _PERIOD, Decimal("1.00"), Decimal("0.50"), now=_NOW
        )
    try:
        yield {
            "scan": scan.id,
            "source": source.id,
            "placement": placement.id,
            "placed": placed.id,
            "reviewed": reviewed.id,
        }
    finally:
        await _remove(ledger_engine, scan.id, source.id, placement.id, [placed.id, reviewed.id])


async def _remove(
    ledger_engine: AsyncEngine,
    scan_id: uuid.UUID,
    source_id: uuid.UUID,
    placement_id: uuid.UUID,
    content_ids: list[uuid.UUID],
) -> None:
    """Delete what `_failed_units` created."""
    async with ledger_engine.begin() as connection:
        await connection.execute(delete(placements).where(placements.c.id == placement_id))
        await connection.execute(
            delete(review_items).where(review_items.c.content_object_id.in_(content_ids))
        )
        await connection.execute(
            delete(content_objects).where(content_objects.c.id.in_(content_ids))
        )
        await connection.execute(delete(source_objects).where(source_objects.c.id == source_id))
        await connection.execute(delete(scan_folders).where(scan_folders.c.scan_id == scan_id))
        await connection.execute(delete(scans).where(scans.c.id == scan_id))
        await connection.execute(delete(remote_spend).where(remote_spend.c.period == _PERIOD))


@pytest.fixture
async def failed_units(ledger_engine: AsyncEngine) -> AsyncIterator[dict[str, uuid.UUID]]:
    """The failed units, removed after the test."""
    async with _failed_units(ledger_engine) as units:
        yield units


async def test_counts_by_state_account_for_each_new_unit(
    http: TestClient, ledger_engine: AsyncEngine
) -> None:
    """Each failure lands in its stage's failed count, and the cap breach is reported."""
    before = http.get("/jobs").json()
    async with _failed_units(ledger_engine):
        after = http.get("/jobs").json()

    def delta(stage: str, bucket: str) -> int:
        """How much `bucket` of `stage` grew."""
        return int(after[stage][bucket]) - int(before[stage][bucket])

    assert delta("scans", "failed") == 1
    assert delta("fingerprinting", "failed") == 1
    assert delta("placements", "failed") == 1
    assert delta("classification", "failed") == 1
    assert int(after["classification"]["by_state"]["awaiting_review"]) >= 1
    assert after["spend"]["period"] == _PERIOD
    assert after["spend"]["cap_reached"] is True
    assert after["spend"]["cap_reached_at"].startswith("2099-01-15T12:00:00")


async def test_each_failed_unit_reports_what_it_concerned_and_why(
    http: TestClient, failed_units: dict[str, uuid.UUID]
) -> None:
    """Scan, source, review and placement failures carry their identity and reason."""
    reported: list[dict[str, str]] = http.get("/jobs/failures", params={"limit": 1000}).json()

    def find(key: str, value: uuid.UUID) -> dict[str, str]:
        """The failure whose `key` is `value`."""
        [found] = [f for f in reported if f[key] == str(value)]
        return found

    scan = find("scan_id", failed_units["scan"])
    assert (scan["stage"], "HTTP 503" in scan["reason"]) == ("scan", True)
    source = find("source_object_id", failed_units["source"])
    assert source["stage"] == "fingerprinting"
    assert "unreadable" in source["reason"]
    assert source["where"].endswith("/broken.jpg")
    placement = find("placement_id", failed_units["placement"])
    assert placement["stage"] == "placement"
    assert placement["content_object_id"] == str(failed_units["placed"])
    assert placement["reason"].startswith("failed: copy timed out")
    assert placement["where"].startswith("lib-")
    assert "@pcloud:acct-" in placement["where"]
    review = find("content_object_id", failed_units["reviewed"])
    assert review["stage"] == "classification"
    assert review["reason"].startswith("low_confidence: no tier was sure")


async def test_a_failed_workflow_reports_its_error(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, http: TestClient
) -> None:
    """The workflow's exception comes back decoded, with its queue and name."""
    marker = uuid.uuid4().hex[:8]
    workflow_id = f"test-observability-{marker}"
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        with SetWorkflowID(workflow_id):
            await DBOS.enqueue_workflow_async(
                QueueName.MAINTENANCE.value, failing_for_observability, marker
            )
        await pipeline.settle()

    reported = http.get("/jobs/failures", params={"limit": 50}).json()
    [failure] = [f for f in reported if f["workflow_id"] == workflow_id]
    assert failure["stage"] == "workflow"
    assert failure["reason"].startswith("ERROR")
    assert f"observability probe {marker}" in failure["reason"]
    assert failure["where"].startswith("maintenance")
