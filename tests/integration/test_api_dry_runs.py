"""Dry runs through the API: started by a client, run by the workers, exported in bulk.

The API enqueues through the workflow system database like any other client; the test
process runs the workers, so the run executes here and its export is read back.
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from dbos import DBOS, SetWorkflowID, WorkflowHandleAsync
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.api.app import create_app
from curate_d.classification.store import ReviewRepository
from curate_d.config import Settings
from curate_d.domain import Provider, RejectedValue, ReviewReason, TaxonomyField
from curate_d.ledger.tables import dry_runs, review_items
from curate_d.runtime import dbos_app
from curate_d.workflows.scan import scan_root
from tests.integration.pipeline_harness import running_pipeline
from tests.stubs import media

pytestmark = pytest.mark.integration

_RECEIPTS = RejectedValue(field=TaxonomyField.SUBCATEGORY, value="Receipts", category="Documents")


@pytest.fixture
def http(settings: Settings) -> Iterator[TestClient]:
    """The app over the ledger and the workflow system database."""
    with TestClient(create_app(settings)) as client:
        yield client


async def test_a_started_run_exports_facts_locators_and_unresolved_items(
    settings: Settings, ledger_engine: AsyncEngine, tmp_path: Path, http: TestClient
) -> None:
    """A proposal, a render failure with its reason, and refused taxonomy values."""
    async with running_pipeline(settings, ledger_engine, tmp_path) as pipeline:
        camera = ("Canon", f"EOS {pipeline.account}")
        pipeline.pcloud.file(
            "/Camera/IMG_0001.JPG",
            media.photo(
                camera=camera,
                taken=datetime(2024, 5, 1, 9, 30),  # noqa: DTZ001 - EXIF wall-clock time
                offset="+02:00",
            ),
        )
        pipeline.pcloud.file("/Camera/IMG_0002.JPG", media.photo(camera=camera, seed=2))
        pipeline.pcloud.file("/Camera/IMG_0003.JPG", media.photo(camera=camera, seed=3))
        cycle = uuid.uuid4().hex[:8]
        with SetWorkflowID(f"test-scan-{pipeline.account}-{cycle}"):
            await scan_root(Provider.PCLOUD.value, pipeline.account, "/", cycle)
        await pipeline.settle()
        by_path = {s.path: s.content_object_id for s in await pipeline.sources()}
        refused = by_path["/Camera/IMG_0003.JPG"]
        assert refused is not None
        async with ledger_engine.begin() as connection:
            await ReviewRepository(connection).open_item(
                refused,
                ReviewReason.REJECTED_VALUE,
                "subcategory outside",
                (_RECEIPTS,),
                now=datetime.now(UTC),
            )
        destination = {
            "name": f"dry-{uuid.uuid4().hex[:8]}",
            "provider": "pcloud",
            "account": pipeline.account,
            "root_path": "/Organized",
        }

        started = http.post("/dry-runs", json={"destinations": [destination]})
        assert started.status_code == 202
        run_id = started.json()["run_id"]
        try:
            handle: WorkflowHandleAsync[int] = await DBOS.retrieve_workflow_async(
                started.json()["workflow_id"]
            )
            assert await handle.get_result() >= 3
            status = http.get(f"/dry-runs/{run_id}").json()
            exported = http.get(f"/dry-runs/{run_id}/export").json()
        finally:
            async with ledger_engine.begin() as connection:
                await connection.execute(delete(dry_runs).where(dry_runs.c.id == uuid.UUID(run_id)))
                await connection.execute(
                    delete(review_items).where(review_items.c.content_object_id == refused)
                )

    assert status["state"] == "finished"
    ours = {
        item["paths"][0]: item
        for item in exported["items"]
        if item["content_object_id"] in {str(c) for c in by_path.values()}
    }
    proposed = ours["/Camera/IMG_0001.JPG"]
    assert proposed["outcome"] == "proposed"
    assert proposed["facts"]["category"] == "Photos"
    assert proposed["locator"] == "/Organized/Photos/2024/2024-05/IMG_0001.JPG"
    failed = ours["/Camera/IMG_0002.JPG"]
    assert failed["outcome"] == "render_failed"
    assert "captured_at" in failed["reason"]
    unresolved = ours["/Camera/IMG_0003.JPG"]
    assert unresolved["outcome"] == "unclassified"
    assert unresolved["rejected_values"] == [_RECEIPTS.model_dump(mode="json")]
    assert exported["counts"]["proposed"] >= 1


async def test_an_unknown_run_is_reported_as_such(http: TestClient) -> None:
    """Neither recorded nor enqueued: 404."""
    missing = uuid.uuid4()
    for path in (f"/dry-runs/{missing}", f"/dry-runs/{missing}/export"):
        response = http.get(path)
        assert response.status_code == 404
        assert response.json()["detail"] == f"no dry run {missing}"


async def test_a_cancelled_run_reports_it_so_callers_stop_polling(
    settings: Settings, http: TestClient
) -> None:
    """Started with no worker to run it, then cancelled: cancelled, not queued forever."""
    started = http.post("/dry-runs", json={"destinations": []})
    run_id, workflow_id = started.json()["run_id"], started.json()["workflow_id"]
    assert http.get(f"/dry-runs/{run_id}").json()["state"] == "queued"

    workflows = dbos_app.client(settings)
    try:
        await workflows.cancel_workflow_async(workflow_id)
    finally:
        workflows.destroy()

    status = http.get(f"/dry-runs/{run_id}").json()
    assert (status["state"], status["detail"]) == ("cancelled", "cancelled")
