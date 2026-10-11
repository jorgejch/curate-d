"""Workflow status views.

DBOS's operational tooling is thin; these views are the compensation, and a view that
silently stops matching DBOS's schema after an upgrade is worse than no view at all.
"""

import asyncio
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
from dbos import DBOS, SetEnqueueOptions, SetWorkflowID, WorkflowHandleAsync
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.config import Settings
from curate_d.runtime.dbos_app import configure

pytestmark = pytest.mark.integration


@pytest.fixture
async def dbos(settings: Settings) -> AsyncIterator[None]:
    """Launch a DBOS instance against the test settings.

    Yields:
        Nothing; DBOS is destroyed again once the test completes.
    """
    DBOS.destroy()
    configure(settings, executor_id="views-test")
    DBOS.launch()
    yield
    DBOS.destroy()


async def test_seeded_workflows_are_counted_by_queue_and_state(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """workflow_status_by_queue counts completed workflows per queue and status."""
    queue_name = f"views-probe-{uuid4().hex[:8]}"
    queue = await DBOS.register_queue_async(
        queue_name, worker_concurrency=4, on_conflict="never_update"
    )

    @DBOS.workflow()
    async def succeed(index: int) -> int:
        """Return `index` unchanged."""
        return index

    for index in range(3):
        with SetWorkflowID(f"{queue_name}-{index}"):
            handle = await DBOS.enqueue_workflow_async(queue.name, succeed, index)
            await asyncio.wait_for(handle.get_result(), timeout=30)

    async with ledger_engine.connect() as connection:
        count = await connection.scalar(
            text(
                "SELECT workflow_count FROM workflow_status_by_queue"
                " WHERE queue_name = :queue AND status = 'SUCCESS'"
            ),
            {"queue": queue_name},
        )
    assert count == 3


async def test_the_view_reports_a_backlog_per_partition(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """Aggregating the partition away would hide which provider is actually stalled."""
    queue_name = f"views-partition-{uuid4().hex[:8]}"
    queue = await DBOS.register_queue_async(
        queue_name, partition_concurrency=2, on_conflict="never_update"
    )

    @DBOS.workflow()
    async def succeed(provider: str) -> str:
        """Return `provider` unchanged."""
        return provider

    with SetEnqueueOptions(queue_partition_key="pcloud"), SetWorkflowID(f"{queue_name}-a"):
        handle = await DBOS.enqueue_workflow_async(queue.name, succeed, "pcloud")
        await asyncio.wait_for(handle.get_result(), timeout=30)

    async with ledger_engine.connect() as connection:
        partition = await connection.scalar(
            text("SELECT partition_key FROM workflow_status_by_queue WHERE queue_name = :queue"),
            {"queue": queue_name},
        )
    assert partition == "pcloud"


async def test_a_failed_workflow_appears_with_its_reason(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """failed_workflows carries the serialized error payload for a failed workflow."""

    @DBOS.workflow()
    async def fail() -> None:
        """Always raise, to seed a failed workflow row."""
        raise RuntimeError("provider returned 503")

    workflow_id = f"views-failure-{uuid4()}"
    with pytest.raises(RuntimeError), SetWorkflowID(workflow_id):
        await fail()

    async with ledger_engine.connect() as connection:
        row = (
            (
                await connection.execute(
                    text(
                        "SELECT workflow_name, error_payload FROM failed_workflows"
                        " WHERE workflow_id = :id"
                    ),
                    {"id": workflow_id},
                )
            )
            .mappings()
            .one()
        )

    assert row["error_payload"] is not None
    # The payload is DBOS's serialized exception, not a message. The control plane reads
    # the reason back through DBOS; the view exists to find the workflow, not to render it.
    handle: WorkflowHandleAsync[None] = await DBOS.retrieve_workflow_async(workflow_id)
    status = await handle.get_status()
    assert status.status == "ERROR"


async def test_a_completed_workflow_is_not_reported_as_stalled(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """stalled_workflows excludes a workflow that has already succeeded."""

    @DBOS.workflow()
    async def succeed() -> str:
        """Return immediately, to seed a completed workflow row."""
        return "done"

    workflow_id = f"views-done-{uuid4()}"
    with SetWorkflowID(workflow_id):
        assert await succeed() == "done"

    async with ledger_engine.connect() as connection:
        stalled = await connection.scalar(
            text("SELECT count(*) FROM stalled_workflows WHERE workflow_id = :id"),
            {"id": workflow_id},
        )
    assert stalled == 0


async def test_the_stalled_view_names_the_application_version(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """A workflow parked on a retired version needs a worker on that version to resume.

    A plain count by state cannot show that, which is why the column is here.
    """
    async with ledger_engine.connect() as connection:
        columns = {
            row[0]
            for row in await connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns"
                    " WHERE table_name = 'stalled_workflows'"
                )
            )
        }
    assert {"application_version", "executor_id", "recovery_attempts"} <= columns
