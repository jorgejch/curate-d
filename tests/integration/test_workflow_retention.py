"""Workflow history retention against the real database.

The boundary arithmetic is unit-tested. What can only be checked here is that the prune
reaches the right rows: that an aged completed record is gone, that a recent one and an
in-flight one survive, and — the property the whole distinction rests on — that the audit
trail is untouched by a prune that deletes workflow history around it.
"""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from dbos import DBOS, ScheduleInput, WorkflowHandleAsync
from dbos._utils import GlobalParams
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.config import Settings
from curate_d.runtime.datasource import create_ledger_datasource
from curate_d.runtime.dbos_app import build_dbos_config
from curate_d.runtime.retention import (
    prune_datasource_checkpoints,
    prune_workflow_history,
    retention_window,
)
from curate_d.workflows import retention
from tests.integration.pipeline_harness import SETTLE_TIMEOUT_SECONDS, running_pipeline

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
RETENTION_DAYS = 30


def _epoch_ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


@pytest.fixture(autouse=True)
async def _remove_seeds(ledger_engine: AsyncEngine) -> AsyncIterator[None]:
    """Delete the rows the test seeded.

    A seeded PENDING row belongs to an executor that never runs, so a later pipeline's
    recovery sweep would hand it to its own process, which cannot run it and waits on it.
    """
    yield
    async with ledger_engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM dbos.workflow_status WHERE name = 'tests.retention.sample'")
        )


@pytest.fixture
async def dbos(settings: Settings) -> AsyncIterator[None]:
    DBOS.destroy()
    config = build_dbos_config(settings, executor_id="retention-test")
    DBOS(config=config)
    DBOS.launch()
    yield
    DBOS.destroy()


@pytest.fixture
async def dbos_with_datasource(settings: Settings) -> AsyncIterator[None]:
    """Launch DBOS with the ledger datasource created, so its tracking table exists."""
    DBOS.destroy()
    DBOS(config=build_dbos_config(settings, executor_id="retention-test"))
    datasource = await create_ledger_datasource(settings)
    DBOS.launch()
    yield
    DBOS.destroy()
    await datasource.engine.dispose()


async def _seed_workflow(
    engine: AsyncEngine,
    *,
    workflow_id: str,
    status: str,
    created_at: datetime,
    completed_at: datetime | None = None,
) -> None:
    """Insert a workflow_status row as DBOS would leave it.

    A terminal seed needs `completed_at`: the collector keys on it, and DBOS sets it on
    every terminal transition.
    """
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO dbos.workflow_status"
                " (workflow_uuid, status, name, executor_id, application_name,"
                "  created_at, updated_at, completed_at, serialization)"
                " VALUES (:id, :status, 'tests.retention.sample', 'retention-test',"
                "  :app, :created, :created, :completed, 'py_pickle')"
            ),
            {
                "id": workflow_id,
                "app": GlobalParams.app_name,
                "status": status,
                "created": _epoch_ms(created_at),
                "completed": None if completed_at is None else _epoch_ms(completed_at),
            },
        )


async def _exists(engine: AsyncEngine, workflow_id: str) -> bool:
    async with engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM dbos.workflow_status WHERE workflow_uuid = :id"),
            {"id": workflow_id},
        )
    return bool(count)


async def _seed_checkpoint(engine: AsyncEngine, *, workflow_id: str, created_at: datetime) -> None:
    """Insert a datasource_outputs row as a completed datasource transaction leaves it."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO dbos.datasource_outputs"
                " (workflow_id, step_id, output, serialization, created_at)"
                " VALUES (:id, 1, NULL, 'py_pickle', :created)"
            ),
            {"id": workflow_id, "created": _epoch_ms(created_at)},
        )


async def _has_checkpoint(engine: AsyncEngine, workflow_id: str) -> bool:
    async with engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM dbos.datasource_outputs WHERE workflow_id = :id"),
            {"id": workflow_id},
        )
    return bool(count)


async def _seed_audit_entry(engine: AsyncEngine, *, occurred_at: datetime) -> int:
    """An audit entry as old as the workflow records being pruned around it."""
    async with engine.begin() as connection:
        entry_id = await connection.scalar(
            text(
                "INSERT INTO audit_entries"
                " (occurred_at, entry_type, decided_by, details)"
                " VALUES (:occurred, 'placement_registered', 'tests.retention',"
                '  \'{"note": "older than any retained workflow"}\'::jsonb)'
                " RETURNING id"
            ),
            {"occurred": occurred_at},
        )
    assert entry_id is not None
    return int(entry_id)


@pytest.fixture
def settings_with_retention(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("CURATE_D_WORKFLOW_RETENTION_DAYS", str(RETENTION_DAYS))
    return Settings()


async def test_a_completed_record_older_than_the_period_is_removed(
    dbos: None, ledger_engine: AsyncEngine, settings_with_retention: Settings
) -> None:
    workflow_id = f"retention-old-{uuid4()}"
    await _seed_workflow(
        ledger_engine,
        workflow_id=workflow_id,
        status="SUCCESS",
        created_at=NOW - timedelta(days=RETENTION_DAYS + 1),
        completed_at=NOW - timedelta(days=RETENTION_DAYS + 1),
    )

    await prune_workflow_history(settings_with_retention, now=NOW)

    assert not await _exists(ledger_engine, workflow_id)


async def test_a_recent_completed_record_survives(
    dbos: None, ledger_engine: AsyncEngine, settings_with_retention: Settings
) -> None:
    workflow_id = f"retention-recent-{uuid4()}"
    await _seed_workflow(
        ledger_engine,
        workflow_id=workflow_id,
        status="SUCCESS",
        created_at=NOW - timedelta(days=RETENTION_DAYS - 1),
        completed_at=NOW - timedelta(days=RETENTION_DAYS - 1),
    )

    await prune_workflow_history(settings_with_retention, now=NOW)

    assert await _exists(ledger_engine, workflow_id)


async def test_every_audit_entry_survives_a_prune(
    dbos: None, ledger_engine: AsyncEngine, settings_with_retention: Settings
) -> None:
    """The distinction the design rests on: history is exhaust, the audit trail is not."""
    entry_id = await _seed_audit_entry(
        ledger_engine, occurred_at=NOW - timedelta(days=RETENTION_DAYS * 10)
    )
    workflow_id = f"retention-alongside-{uuid4()}"
    await _seed_workflow(
        ledger_engine,
        workflow_id=workflow_id,
        status="SUCCESS",
        created_at=NOW - timedelta(days=RETENTION_DAYS + 1),
        completed_at=NOW - timedelta(days=RETENTION_DAYS + 1),
    )

    async with ledger_engine.connect() as connection:
        before = await connection.scalar(text("SELECT count(*) FROM audit_entries"))

    await prune_workflow_history(settings_with_retention, now=NOW)

    async with ledger_engine.connect() as connection:
        after = await connection.scalar(text("SELECT count(*) FROM audit_entries"))
        survived = await connection.scalar(
            text("SELECT count(*) FROM audit_entries WHERE id = :id"), {"id": entry_id}
        )

    assert not await _exists(ledger_engine, workflow_id), "the prune did not run"
    assert after == before, "pruning workflow history removed audit entries"
    assert survived == 1


async def test_an_in_flight_record_is_never_pruned_however_old(
    dbos: None, ledger_engine: AsyncEngine, settings_with_retention: Settings
) -> None:
    """Otherwise a long deployment would delete the work the recovery sweep exists to find."""
    workflow_id = f"retention-pending-{uuid4()}"
    await _seed_workflow(
        ledger_engine,
        workflow_id=workflow_id,
        status="PENDING",
        created_at=NOW - timedelta(days=RETENTION_DAYS * 5),
    )

    await prune_workflow_history(settings_with_retention, now=NOW)

    assert await _exists(ledger_engine, workflow_id)


async def test_a_shorter_configured_period_prunes_more(
    dbos: None, ledger_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the period reaches the deletion, not just the returned window."""
    workflow_id = f"retention-configured-{uuid4()}"
    await _seed_workflow(
        ledger_engine,
        workflow_id=workflow_id,
        status="SUCCESS",
        created_at=NOW - timedelta(days=10),
        completed_at=NOW - timedelta(days=10),
    )

    monkeypatch.setenv("CURATE_D_WORKFLOW_RETENTION_DAYS", "30")
    await prune_workflow_history(Settings(), now=NOW)
    assert await _exists(ledger_engine, workflow_id), "a 30-day period should have kept it"

    monkeypatch.setenv("CURATE_D_WORKFLOW_RETENTION_DAYS", "5")
    await prune_workflow_history(Settings(), now=NOW)
    assert not await _exists(ledger_engine, workflow_id), "a 5-day period should have pruned it"


async def test_the_period_runs_from_completion_not_creation(
    dbos: None, ledger_engine: AsyncEngine, settings_with_retention: Settings
) -> None:
    """A long-running workflow is kept for the full period after it finishes."""
    workflow_id = f"retention-long-running-{uuid4()}"
    await _seed_workflow(
        ledger_engine,
        workflow_id=workflow_id,
        status="SUCCESS",
        created_at=NOW - timedelta(days=RETENTION_DAYS * 5),
        completed_at=NOW - timedelta(days=RETENTION_DAYS - 1),
    )

    await prune_workflow_history(settings_with_retention, now=NOW)

    assert await _exists(ledger_engine, workflow_id)


async def test_the_applied_window_is_reported_back(
    dbos: None, settings_with_retention: Settings
) -> None:
    """A caller must be able to log the boundary actually used, not recompute and hope."""
    window = await prune_workflow_history(settings_with_retention, now=NOW)
    assert window == retention_window(settings_with_retention, now=NOW)
    assert window.retention_days == RETENTION_DAYS


async def test_the_checkpoints_of_a_pruned_workflow_are_removed(
    dbos_with_datasource: None, ledger_engine: AsyncEngine, settings_with_retention: Settings
) -> None:
    """DBOS's collector leaves them behind; nothing else in DBOS 3.1 ever would."""
    workflow_id = f"retention-checkpoint-old-{uuid4()}"
    aged = NOW - timedelta(days=RETENTION_DAYS + 1)
    await _seed_workflow(
        ledger_engine, workflow_id=workflow_id, status="SUCCESS", created_at=aged, completed_at=aged
    )
    await _seed_checkpoint(ledger_engine, workflow_id=workflow_id, created_at=aged)

    await prune_workflow_history(settings_with_retention, now=NOW)

    assert not await _exists(ledger_engine, workflow_id), "the collector did not run"
    assert not await _has_checkpoint(ledger_engine, workflow_id)


async def test_the_checkpoints_of_an_in_flight_workflow_survive_however_old(
    dbos_with_datasource: None, ledger_engine: AsyncEngine, settings_with_retention: Settings
) -> None:
    """Recovery replays them; deleting one would repeat its ledger write."""
    workflow_id = f"retention-checkpoint-pending-{uuid4()}"
    aged = NOW - timedelta(days=RETENTION_DAYS * 5)
    await _seed_workflow(ledger_engine, workflow_id=workflow_id, status="PENDING", created_at=aged)
    await _seed_checkpoint(ledger_engine, workflow_id=workflow_id, created_at=aged)

    await prune_workflow_history(settings_with_retention, now=NOW)

    assert await _has_checkpoint(ledger_engine, workflow_id)


async def test_a_checkpoint_newer_than_the_cutoff_survives_without_its_workflow(
    dbos_with_datasource: None, ledger_engine: AsyncEngine, settings_with_retention: Settings
) -> None:
    """A collected deterministic ID enqueued again writes fresh checkpoints; they must stay."""
    workflow_id = f"retention-checkpoint-recent-{uuid4()}"
    await _seed_checkpoint(
        ledger_engine, workflow_id=workflow_id, created_at=NOW - timedelta(days=1)
    )

    await prune_workflow_history(settings_with_retention, now=NOW)

    assert await _has_checkpoint(ledger_engine, workflow_id)


async def test_the_checkpoint_walk_passes_kept_workflows_rather_than_stalling_on_them(
    dbos_with_datasource: None, ledger_engine: AsyncEngine, settings: Settings
) -> None:
    """A batch of workflows that all still exist must not end the walk before an orphan."""
    run = uuid4().hex
    kept, orphaned = f"retention-walk-{run}-0", f"retention-walk-{run}-1"
    aged = NOW - timedelta(days=RETENTION_DAYS * 5)
    await _seed_workflow(ledger_engine, workflow_id=kept, status="PENDING", created_at=aged)
    await _seed_checkpoint(ledger_engine, workflow_id=kept, created_at=aged)
    await _seed_checkpoint(ledger_engine, workflow_id=orphaned, created_at=aged)

    deleted = prune_datasource_checkpoints(
        settings, retention_window(settings, now=NOW).cutoff_epoch_ms, batch_size=1
    )

    assert deleted >= 1
    assert await _has_checkpoint(ledger_engine, kept)
    assert not await _has_checkpoint(ledger_engine, orphaned)


async def test_a_scheduled_tick_prunes_what_aged_past_the_period_before_it(
    ledger_engine: AsyncEngine, settings_with_retention: Settings, tmp_path: Path
) -> None:
    """The persisted schedule runs the prune, measured from the tick rather than the clock."""
    run = uuid4().hex
    aged, recent = f"retention-tick-old-{run}", f"retention-tick-recent-{run}"
    active = f"retention-tick-active-{run}"
    for workflow_id, days in ((aged, RETENTION_DAYS + 1), (recent, RETENTION_DAYS - 1)):
        moment = NOW - timedelta(days=days)
        await _seed_workflow(
            ledger_engine,
            workflow_id=workflow_id,
            status="SUCCESS",
            created_at=moment,
            completed_at=moment,
        )
    long_ago = NOW - timedelta(days=RETENTION_DAYS * 5)
    await _seed_workflow(ledger_engine, workflow_id=active, status="PENDING", created_at=long_ago)
    entry = ScheduleInput(
        schedule_name=f"test-prune-{run}",
        workflow_fn=retention.prune_history,
        schedule=f"{NOW.minute} {NOW.hour} * * *",
        context=None,
        queue_name=retention.schedule(settings_with_retention)["queue_name"],
    )

    async with running_pipeline(
        settings_with_retention, ledger_engine, tmp_path, listen=("maintenance",)
    ):
        # Seeded here: launching the datasource is what creates dbos.datasource_outputs.
        for workflow_id in (aged, active):
            await _seed_checkpoint(ledger_engine, workflow_id=workflow_id, created_at=long_ago)
        # DBOS stamps a backfilled tick with the latest application version, and only a
        # worker on that version dequeues it; test_recovery_sweep registers a newer one
        # whenever it runs earlier in the session.
        await DBOS.set_latest_application_version_async(DBOS.application_version)
        await DBOS.apply_schedules_async([entry])
        try:
            ticks = await asyncio.to_thread(
                DBOS.backfill_schedule,
                entry["schedule_name"],
                NOW - timedelta(days=1),
                NOW + timedelta(seconds=1),
            )
            assert len(ticks) == 1
            handle: WorkflowHandleAsync[None] = await DBOS.retrieve_workflow_async(
                ticks[0].get_workflow_id()
            )
            await asyncio.wait_for(handle.get_result(), SETTLE_TIMEOUT_SECONDS)
        finally:
            await DBOS.delete_schedule_async(entry["schedule_name"])

    assert not await _exists(ledger_engine, aged)
    assert await _exists(ledger_engine, recent)
    assert not await _has_checkpoint(ledger_engine, aged)
    assert await _has_checkpoint(ledger_engine, active)
