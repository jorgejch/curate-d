"""The gated recovery sweep against the real system database.

The planner's rules and the gate's decisions are unit-tested; what can only be checked
here is that each remedy actually lands, and that each gate holds against the real locks
and registry: a redispatch resumes a row onto its own queue under its own identifier and
counts itself in the row's attributes, forking produces a linked row on the target
version, and a row of a live executor or a live, unregistered or newer version is left
exactly as it was found.

Every workflow this seeds carries a version no deployed worker runs, on a queue no worker
consumes, so the workers in the local stack cannot dequeue them mid-assertion. The sweep
refuses queues outside curate.d's own, so `_admit_test_queues` adds this module's. The sweep is
database-wide, so assertions about `failed` filter to the test's own rows. The stack's own
rows are on a version that never registered, so the sweep leaves them alone.

The fixture registers this run's versions in `lease_versions` and DBOS, oldest first, makes
`CURRENT_VERSION` DBOS's latest, and restores the previous latest on teardown.
"""

import asyncio
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from dbos import DBOS, SetEnqueueOptions, SetWorkflowID, WorkflowHandleAsync
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.config import Settings
from curate_d.runtime import dbos_app, sweep
from curate_d.runtime.dbos_app import build_dbos_config, configure
from curate_d.runtime.lease import ExecutorLease
from curate_d.runtime.profiles import WorkerProfile
from curate_d.runtime.sweep import (
    MAX_SWEEP_REDISPATCHES,
    REDISPATCH_COUNT_ATTRIBUTE,
    REDISPATCH_VERSION_ATTRIBUTE,
    DBOSRecoveryControl,
    RecoveryAction,
    SweepOutcome,
    plan_recovery,
    recovery_successor_id,
    run_recovery_sweep,
)
from curate_d.runtime.version_lease import hold_exclusive
from curate_d.worker import main as worker_main

pytestmark = pytest.mark.integration

# Unique per run: liveness is a lease on this name, and a concurrent run must not share it.
LIVE_EXECUTOR = f"sweep-live-{uuid4().hex[:8]}"
RETIRED_EXECUTOR = f"sweep-retired-{uuid4().hex[:8]}"

# Versions no deployed worker runs, registered oldest first. The suffix keeps repeat runs
# from colliding with each other.
_RUN = uuid4().hex[:8]
OLDEST_VERSION = f"9.9.7-sweep-{_RUN}"
SUPERSEDED_VERSION = f"9.9.8-sweep-{_RUN}"
CURRENT_VERSION = f"9.9.9-sweep-{_RUN}"
FUTURE_VERSION = f"9.9.10-sweep-{_RUN}"
REGISTERED = (OLDEST_VERSION, SUPERSEDED_VERSION, CURRENT_VERSION, FUTURE_VERSION)
UNREGISTERED_VERSION = f"9.9.6-sweep-unregistered-{_RUN}"
ALL_VERSIONS = [*REGISTERED, UNREGISTERED_VERSION]

RUN_QUEUE = f"sweep-run-{_RUN}"
"""A queue only this process listens to, for rows the test wants to see run."""

UNCONSUMED_QUEUE = "sweep-unconsumed"
"""A queue no process listens to, for rows the test only inspects."""


@pytest.fixture(autouse=True)
def _admit_test_queues(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the sweep fork and redispatch onto this module's queues as onto curate.d's own."""
    monkeypatch.setattr(
        sweep, "_DEQUEUED_QUEUES", sweep._DEQUEUED_QUEUES | {RUN_QUEUE, UNCONSUMED_QUEUE}
    )


@DBOS.workflow()
async def sweep_doubles(value: int) -> int:
    """Return twice `value`; recovered rows of it should complete on the target."""
    return value * 2


async def _latest_version(engine: AsyncEngine) -> str | None:
    """Return this application's latest version as DBOS ranks it, if any."""
    async with engine.connect() as connection:
        latest = await connection.scalar(
            text(
                "SELECT version_name FROM dbos.application_versions"
                " WHERE application_name = :app ORDER BY version_timestamp DESC LIMIT 1"
            ),
            {"app": dbos_app.application_name(Settings().environment)},
        )
    return None if latest is None else str(latest)


async def _register_versions(engine: AsyncEngine) -> None:
    """Register this run's versions in `lease_versions` and in DBOS, oldest first."""
    now = datetime.now(UTC)
    async with engine.begin() as connection:
        for index, version in enumerate(REGISTERED):
            registered_at = now - timedelta(hours=len(REGISTERED) - index)
            await connection.execute(
                text("INSERT INTO lease_versions (version, first_registered) VALUES (:v, :at)"),
                {"v": version, "at": registered_at},
            )
            await connection.execute(
                text(
                    "INSERT INTO dbos.application_versions"
                    " (version_id, version_name, version_timestamp, application_name)"
                    " VALUES (:id, :v, :ts, :app) ON CONFLICT DO NOTHING"
                ),
                {
                    "id": str(uuid4()),
                    "v": version,
                    "ts": int(registered_at.timestamp() * 1000),
                    "app": dbos_app.application_name(Settings().environment),
                },
            )


async def _forget_versions(engine: AsyncEngine) -> None:
    """Remove this run's versions from `lease_versions` and from DBOS."""
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM lease_versions WHERE version = ANY(:v)"), {"v": ALL_VERSIONS}
        )
        await connection.execute(
            text("DELETE FROM dbos.application_versions WHERE version_name = ANY(:v)"),
            {"v": ALL_VERSIONS},
        )


@pytest.fixture
async def dbos(settings: Settings, ledger_engine: AsyncEngine) -> AsyncIterator[None]:
    """Launch DBOS as `LIVE_EXECUTOR` on the latest version, leased as a worker is."""
    previous_latest = await _latest_version(ledger_engine)
    await _register_versions(ledger_engine)
    DBOS.destroy()
    lease = ExecutorLease.for_settings(settings, LIVE_EXECUTOR, version=CURRENT_VERSION)
    lease.acquire(timeout=10)
    config = build_dbos_config(settings, executor_id=LIVE_EXECUTOR)
    # Recovery reads the process's own version, so the instance must agree with the
    # sweep's target, exactly as a deployed worker would.
    config["application_version"] = CURRENT_VERSION
    DBOS(config=config)
    DBOS.launch()
    await DBOS.register_queue_async(RUN_QUEUE)
    await DBOS.set_latest_application_version_async(CURRENT_VERSION)
    try:
        yield
    finally:
        # Also every row on this run's versions: the sweep is database-wide, and a fork
        # left behind is stranded again by the next run.
        rows = [
            *await DBOS.list_workflows_async(name="tests.sweep.stranded"),
            *await DBOS.list_workflows_async(app_version=ALL_VERSIONS),
        ]
        if rows:
            await DBOS.delete_workflows_async([row.workflow_id for row in rows])
        if previous_latest is not None:
            await DBOS.set_latest_application_version_async(previous_latest)
        DBOS.destroy()
        lease.release()
        await _forget_versions(ledger_engine)


@asynccontextmanager
async def _latest(version: str) -> AsyncIterator[None]:
    """Make `version` DBOS's latest for the block, then `CURRENT_VERSION` again."""
    await DBOS.set_latest_application_version_async(version)
    try:
        yield
    finally:
        await DBOS.set_latest_application_version_async(CURRENT_VERSION)


async def _sweep(own_version: str = CURRENT_VERSION) -> SweepOutcome:
    """Run the sweep against the configured database, as a sweeper on `own_version` would."""
    return await run_recovery_sweep(
        Settings(), control=DBOSRecoveryControl(), own_version=own_version
    )


def _failed(outcome: SweepOutcome, *workflow_ids: str) -> dict[str, str]:
    """Return the failures among `workflow_ids`; the sweep reports database-wide."""
    return {wid: reason for wid, reason in outcome.failed if wid in workflow_ids}


async def _seed_pending(
    engine: AsyncEngine,
    *,
    workflow_id: str,
    executor_id: str | None,
    version: str | None,
    status: str = "PENDING",
    queue_name: str | None = UNCONSUMED_QUEUE,
    queue_partition_key: str | None = None,
    delay_until_epoch_ms: int | None = None,
) -> None:
    """Write a PENDING row as an abandoned worker would have left one behind."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO dbos.workflow_status"
                " (workflow_uuid, status, name, executor_id, application_version,"
                "  application_name, inputs, serialization, queue_name, queue_partition_key,"
                "  delay_until_epoch_ms)"
                " VALUES (:id, :status, :name, :executor, :version,"
                "  :app, NULL, 'py_pickle', :queue, :partition, :delay)"
            ),
            {
                "id": workflow_id,
                "name": "tests.sweep.stranded",
                "app": dbos_app.application_name(Settings().environment),
                "executor": executor_id,
                "version": version,
                "status": status,
                "queue": queue_name,
                "partition": queue_partition_key,
                "delay": delay_until_epoch_ms,
            },
        )


async def _row(engine: AsyncEngine, workflow_id: str) -> dict[str, object]:
    """Return the status columns the sweep changes, for one row."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT status, executor_id, application_version, queue_name, queue_partition_key,"
                " forked_from, was_forked_from, delay_until_epoch_ms"
                " FROM dbos.workflow_status WHERE workflow_uuid = :id"
            ),
            {"id": workflow_id},
        )
        return dict(result.mappings().one())


async def _fork_of(engine: AsyncEngine, workflow_id: str) -> dict[str, object]:
    """Return the one row forked from `workflow_id`."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                "SELECT workflow_uuid, status, application_version, queue_name,"
                " queue_partition_key,"
                " forked_from"
                " FROM dbos.workflow_status WHERE forked_from = :id"
            ),
            {"id": workflow_id},
        )
        return dict(result.mappings().one())


async def _forks(engine: AsyncEngine, workflow_id: str) -> int:
    """Count the rows forked from `workflow_id`."""
    async with engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM dbos.workflow_status WHERE forked_from = :id"),
            {"id": workflow_id},
        )
    return int(count or 0)


async def test_a_workflow_left_by_a_retired_identity_is_recovered(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """Nothing scans for an identity that no longer boots, so this row waits forever."""
    workflow_id = f"sweep-orphan-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=workflow_id,
        executor_id=RETIRED_EXECUTOR,
        version=CURRENT_VERSION,
    )

    outcome = await _sweep()

    assert workflow_id in outcome.reenqueued
    row = await _row(ledger_engine, workflow_id)
    assert row["status"] == "ENQUEUED", "the orphan was not returned to a queue"
    assert row["queue_name"] == UNCONSUMED_QUEUE, "the orphan did not keep its own queue"
    assert row["executor_id"] == RETIRED_EXECUTOR, "resume reassigned the row's executor"
    # Re-enqueueing keeps the identifier, which is what preserves the idempotency key
    # the reconciler derives from content and destination.
    assert row["forked_from"] is None
    assert row["application_version"] == CURRENT_VERSION
    status = await DBOS.get_workflow_status_async(workflow_id)
    assert status is not None
    assert status.attributes == {
        REDISPATCH_COUNT_ATTRIBUTE: 1,
        REDISPATCH_VERSION_ATTRIBUTE: CURRENT_VERSION,
    }


async def test_a_row_at_the_redispatch_cap_is_reported_and_left_pending(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """Resume resets DBOS's own recovery attempts, so the sweep's count stops the loop."""
    workflow_id = f"sweep-capped-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=workflow_id,
        executor_id=RETIRED_EXECUTOR,
        version=CURRENT_VERSION,
    )
    await DBOS.update_workflow_attributes_async(
        workflow_id,
        {
            REDISPATCH_COUNT_ATTRIBUTE: MAX_SWEEP_REDISPATCHES,
            REDISPATCH_VERSION_ATTRIBUTE: CURRENT_VERSION,
        },
    )
    before = await _row(ledger_engine, workflow_id)

    outcome = await _sweep()

    assert workflow_id not in outcome.reenqueued
    assert "the cap of" in _failed(outcome, workflow_id)[workflow_id]
    assert await _row(ledger_engine, workflow_id) == before


async def test_a_workflow_left_by_a_superseded_version_is_recovered(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """Dequeue is version-filtered, so this one needs moving forward, not re-enqueueing."""
    workflow_id = f"sweep-parked-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=workflow_id,
        executor_id=RETIRED_EXECUTOR,
        version=SUPERSEDED_VERSION,
    )

    outcome = await _sweep()

    assert workflow_id in [original for original, _ in outcome.forked]
    fork = await _fork_of(ledger_engine, workflow_id)
    assert fork["status"] == "ENQUEUED"
    assert fork["queue_name"] == UNCONSUMED_QUEUE
    # The point of the remedy: the copy runs on a version a live worker actually serves.
    assert fork["application_version"] == CURRENT_VERSION
    assert fork["workflow_uuid"] == recovery_successor_id(workflow_id)


async def test_a_retired_versions_workflow_is_forked_and_completes(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """Enqueued on a version that left, it runs to completion on the target."""
    workflow_id = f"sweep-completes-{uuid4()}"
    queue = await DBOS.retrieve_queue_async(RUN_QUEUE)
    assert queue is not None
    with SetEnqueueOptions(app_version=SUPERSEDED_VERSION), SetWorkflowID(workflow_id):
        await queue.enqueue_async(sweep_doubles, 21)
    assert (await _row(ledger_engine, workflow_id))["application_version"] == SUPERSEDED_VERSION

    outcome = await _sweep()

    successor = recovery_successor_id(workflow_id)
    assert (workflow_id, successor) in outcome.forked
    handle: WorkflowHandleAsync[int] = await DBOS.retrieve_workflow_async(successor)
    assert await asyncio.wait_for(handle.get_result(), timeout=30) == 42
    assert (await _row(ledger_engine, workflow_id))["status"] == "CANCELLED"


async def test_a_dequeue_stamps_the_executors_version_on_a_versionless_row(
    dbos: None, settings: Settings
) -> None:
    """A versionless client enqueue carries the dequeuing executor's version once dequeued."""
    # Re-check on a DBOS upgrade: the redispatch cap keys its count on this version.
    workflow_id = f"sweep-versionless-{uuid4()}"
    client = dbos_app.client(settings)
    try:
        handle: WorkflowHandleAsync[int] = await client.enqueue_async(
            {"workflow_name": "sweep_doubles", "queue_name": RUN_QUEUE, "workflow_id": workflow_id},
            21,
        )
        assert await asyncio.wait_for(handle.get_result(), timeout=30) == 42
    finally:
        client.destroy()

    [row] = await DBOS.list_workflows_async(workflow_ids=[workflow_id])
    assert row.app_version == CURRENT_VERSION


async def test_a_workflow_running_on_a_live_executor_is_never_redispatched(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """Re-dispatching it would run the same work twice, from two processes at once."""
    workflow_id = f"sweep-running-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=workflow_id,
        executor_id=LIVE_EXECUTOR,
        version=CURRENT_VERSION,
    )

    before = await _row(ledger_engine, workflow_id)
    outcome = await _sweep()

    assert workflow_id not in outcome.reenqueued
    assert all(original != workflow_id for original, _ in outcome.forked)
    assert await _row(ledger_engine, workflow_id) == before, "a running workflow was disturbed"
    assert await _forks(ledger_engine, workflow_id) == 0


async def test_the_sweep_separates_the_two_remedies_in_one_pass(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """The realistic case: a scale-down and a deployment leave both kinds behind at once."""
    orphan = f"sweep-mixed-orphan-{uuid4()}"
    parked = f"sweep-mixed-parked-{uuid4()}"
    running = f"sweep-mixed-running-{uuid4()}"
    await _seed_pending(
        ledger_engine, workflow_id=orphan, executor_id=RETIRED_EXECUTOR, version=CURRENT_VERSION
    )
    await _seed_pending(
        ledger_engine, workflow_id=parked, executor_id=LIVE_EXECUTOR, version=SUPERSEDED_VERSION
    )
    await _seed_pending(
        ledger_engine, workflow_id=running, executor_id=LIVE_EXECUTOR, version=CURRENT_VERSION
    )

    outcome = await _sweep()

    assert orphan in outcome.reenqueued
    assert parked in [original for original, _ in outcome.forked]
    assert running not in outcome.reenqueued
    assert _failed(outcome, orphan, parked, running) == {}
    assert (await _row(ledger_engine, running))["status"] == "PENDING"


async def test_a_second_sweep_does_not_redispatch_what_the_first_recovered(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """Sweeps overlap in practice; a repeat pass must not multiply the work."""
    workflow_id = f"sweep-repeat-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=workflow_id,
        executor_id=RETIRED_EXECUTOR,
        version=CURRENT_VERSION,
    )

    first = await _sweep()
    second = await _sweep()

    assert workflow_id in first.reenqueued
    # The row is healthy ENQUEUED work now, so the planner leaves it alone.
    assert workflow_id not in second.reenqueued


async def test_the_planner_agrees_with_what_the_database_reports(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """Guards the seam: the planner is only correct if the listing feeds it real fields."""
    workflow_id = f"sweep-listing-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=workflow_id,
        executor_id=RETIRED_EXECUTOR,
        version=SUPERSEDED_VERSION,
    )

    listed = await DBOSRecoveryControl().list_pending()
    pending = [w for w in listed if w.workflow_id == workflow_id]
    assert pending, "the seeded workflow was not listed as pending"
    assert pending[0].executor_id == RETIRED_EXECUTOR
    assert pending[0].application_version == SUPERSEDED_VERSION

    plan = plan_recovery(pending, current_version=CURRENT_VERSION)
    assert [entry.action for entry in plan] == [RecoveryAction.FORK]


@pytest.mark.parametrize("status", ["PENDING", "ENQUEUED"])
@pytest.mark.parametrize(
    ("queue", "partition"),
    [
        ("transfer-large", "provider-1"),
        ("transfer-small", "provider-2"),
        ("fingerprint", None),
    ],
)
async def test_fork_preserves_routing_and_retires_source(
    dbos: None,
    ledger_engine: AsyncEngine,
    status: str,
    queue: str,
    partition: str | None,
) -> None:
    """The successor keeps the original's queue and partition; the original is cancelled."""
    original = f"sweep-routing-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=original,
        executor_id=RETIRED_EXECUTOR,
        version=SUPERSEDED_VERSION,
        status=status,
        queue_name=queue,
        queue_partition_key=partition,
    )
    result = await _sweep()
    assert _failed(result, original) == {}
    fork = await _fork_of(ledger_engine, original)
    assert fork["queue_name"] == queue
    assert fork["queue_partition_key"] == partition
    assert fork["workflow_uuid"] == recovery_successor_id(original)
    assert (await _row(ledger_engine, original))["status"] == "CANCELLED"
    again = await _sweep()
    assert original not in [wid for wid, _ in again.forked]
    assert original not in again.retired


@pytest.mark.parametrize("queue", [None, "_dbos_internal_queue", "retired-queue"])
@pytest.mark.parametrize("on_target", [True, False])
async def test_a_row_without_a_curate_d_queue_is_refused_and_untouched(
    dbos: None, ledger_engine: AsyncEngine, queue: str | None, on_target: bool
) -> None:
    """Every worker dequeues the internal queue and none another: recovery could misroute it."""
    workflow_id = f"sweep-unrouted-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=workflow_id,
        executor_id=RETIRED_EXECUTOR,
        version=CURRENT_VERSION if on_target else SUPERSEDED_VERSION,
        queue_name=queue,
    )
    try:
        before = await _row(ledger_engine, workflow_id)
        outcome = await _sweep()
        assert list(_failed(outcome, workflow_id)) == [workflow_id]
        assert "refused" in _failed(outcome, workflow_id)[workflow_id]
        assert await _row(ledger_engine, workflow_id) == before
        assert await _forks(ledger_engine, workflow_id) == 0
    finally:
        await DBOS.cancel_workflow_async(workflow_id)


async def test_concurrent_sweeps_publish_one_successor(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """The version lock admits one sweep; the successor ID would arbitrate even without it."""
    original = f"sweep-concurrent-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=original,
        executor_id=RETIRED_EXECUTOR,
        version=SUPERSEDED_VERSION,
    )
    first, second = await asyncio.gather(_sweep(), _sweep())
    assert _failed(first, original) == _failed(second, original) == {}
    forked = [o for outcome in (first, second) for o, _ in outcome.forked if o == original]
    assert forked == [original]
    assert await _forks(ledger_engine, original) == 1
    assert (await _fork_of(ledger_engine, original))["workflow_uuid"] == recovery_successor_id(
        original
    )
    assert (await _row(ledger_engine, original))["status"] == "CANCELLED"


async def test_failure_before_fork_commit_leaves_original_retryable(
    dbos: None, ledger_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fork that fails leaves the original PENDING, and the next sweep forks it."""
    original = f"sweep-failure-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=original,
        executor_id=RETIRED_EXECUTOR,
        version=SUPERSEDED_VERSION,
    )
    with monkeypatch.context() as patch:
        patch.setattr(DBOS, "fork_workflow_async", AsyncMock(side_effect=RuntimeError("offline")))
        result = await _sweep()
    assert original in _failed(result, original)
    assert (await _row(ledger_engine, original))["status"] == "PENDING"
    assert await DBOS.get_workflow_status_async(recovery_successor_id(original)) is None
    retry = await _sweep()
    assert (original, recovery_successor_id(original)) in retry.forked


@pytest.mark.parametrize("retry_on", [CURRENT_VERSION, SUPERSEDED_VERSION, FUTURE_VERSION])
async def test_crash_after_fork_is_repaired_without_another_sibling(
    dbos: None,
    ledger_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    retry_on: str,
) -> None:
    """The original is retired, whichever version is the target by then, and never forked again."""
    original = f"sweep-crash-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=original,
        executor_id=RETIRED_EXECUTOR,
        version=SUPERSEDED_VERSION,
    )
    with monkeypatch.context() as patch:
        patch.setattr(DBOS, "cancel_workflow_async", AsyncMock(side_effect=RuntimeError("crash")))
        first = await _sweep()
    assert original in _failed(first, original)
    assert (await _row(ledger_engine, original))["was_forked_from"] is True
    successor = recovery_successor_id(original)
    assert (await _fork_of(ledger_engine, original))["workflow_uuid"] == successor

    # Rolled back to the original's version, or forward past the successor's.
    async with _latest(retry_on):
        result = await _sweep(retry_on)
    assert _failed(result, original, successor) == {}
    assert original in result.retired
    assert (await _row(ledger_engine, original))["status"] == "CANCELLED"
    assert await _forks(ledger_engine, original) == 1, "the root was forked a second time"
    if retry_on == SUPERSEDED_VERSION:
        # The successor's version registered after the restored target: it waits.
        assert successor in result.newer_than_target
    if retry_on == FUTURE_VERSION:
        # The successor's version is live in this process, which holds it shared.
        assert await _forks(ledger_engine, successor) == 0
        assert result.version_live >= 1


@pytest.mark.parametrize("version", [CURRENT_VERSION, None])
async def test_unclaimed_healthy_queue_is_untouched(
    dbos: None, ledger_engine: AsyncEngine, version: str | None
) -> None:
    """ENQUEUED work on the target, or on no version, is not stranded."""
    original = f"sweep-healthy-queued-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=original,
        executor_id=None,
        version=version,
        status="ENQUEUED",
    )
    before = await _row(ledger_engine, original)
    result = await _sweep()
    assert _failed(result, original) == {}
    assert original not in result.reenqueued
    assert original not in [wid for wid, _ in result.forked]
    assert await _row(ledger_engine, original) == before


async def test_delayed_retired_version_waits_until_due(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """Forking a DELAYED row would drop its delay; it is recovered once ENQUEUED."""
    original = f"sweep-delayed-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=original,
        executor_id=None,
        version=SUPERSEDED_VERSION,
        status="DELAYED",
        delay_until_epoch_ms=4_000_000_000_000,
    )
    before = await _row(ledger_engine, original)
    first = await _sweep()
    assert original not in [wid for wid, _ in first.forked]
    assert await _row(ledger_engine, original) == before
    # Model the scheduler's normal DELAYED -> ENQUEUED transition when the deadline arrives.
    async with ledger_engine.begin() as connection:
        await connection.execute(
            text("UPDATE dbos.workflow_status SET status='ENQUEUED' WHERE workflow_uuid=:id"),
            {"id": original},
        )
    second = await _sweep()
    assert (original, recovery_successor_id(original)) in second.forked


async def test_the_sweep_skips_an_executor_whose_lease_is_held(
    dbos: None, ledger_engine: AsyncEngine, settings: Settings
) -> None:
    """A held lease means a live process owns the row, whatever the caller believes."""
    executor = f"sweep-leased-{uuid4().hex[:8]}"
    workflow_id = f"sweep-leased-{uuid4()}"
    await _seed_pending(
        ledger_engine, workflow_id=workflow_id, executor_id=executor, version=CURRENT_VERSION
    )
    holder = ExecutorLease.for_settings(settings, executor)
    holder.acquire(timeout=10)
    try:
        before = await _row(ledger_engine, workflow_id)
        outcome = await _sweep()
        assert workflow_id not in outcome.reenqueued
        assert outcome.left_running >= 1
        assert await _row(ledger_engine, workflow_id) == before
    finally:
        holder.release()


async def test_the_sweep_reenqueues_under_a_free_lease_and_holds_it_meanwhile(
    dbos: None, ledger_engine: AsyncEngine, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The executor cannot start and recover the same rows while the sweep works on them."""
    executor = f"sweep-dead-{uuid4().hex[:8]}"
    workflow_id = f"sweep-dead-{uuid4()}"
    await _seed_pending(
        ledger_engine, workflow_id=workflow_id, executor_id=executor, version=CURRENT_VERSION
    )
    restarting = ExecutorLease.for_settings(settings, executor)
    claimed_mid_sweep: list[bool] = []
    reenqueue = DBOSRecoveryControl.reenqueue

    async def observe(control: DBOSRecoveryControl, resumed: str, *, queue_name: str) -> None:
        """Try to lease the dead executor while its row is resumed, then resume it."""
        if resumed == workflow_id:
            claimed_mid_sweep.append(await asyncio.to_thread(restarting.try_acquire))
        await reenqueue(control, resumed, queue_name=queue_name)

    monkeypatch.setattr(DBOSRecoveryControl, "reenqueue", observe)
    try:
        outcome = await _sweep()
        assert workflow_id in outcome.reenqueued
        assert claimed_mid_sweep == [False], "the executor could lease itself mid-sweep"
        assert (await _row(ledger_engine, workflow_id))["status"] == "ENQUEUED"
        assert await asyncio.to_thread(restarting.try_acquire), "the sweep kept the lease"
    finally:
        restarting.release()


# The version gates.


async def test_a_version_held_by_another_session_is_left(
    dbos: None, ledger_engine: AsyncEngine, settings: Settings
) -> None:
    """A live worker of the source version holds it shared: nothing of it is forked."""
    workflow_id = f"sweep-live-version-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=workflow_id,
        executor_id=RETIRED_EXECUTOR,
        version=SUPERSEDED_VERSION,
    )
    worker = ExecutorLease.for_settings(
        settings, f"sweep-v1-{uuid4().hex[:8]}", version=SUPERSEDED_VERSION
    )
    worker.acquire(timeout=10)
    try:
        before = await _row(ledger_engine, workflow_id)
        outcome = await _sweep()
        assert outcome.version_live >= 1
        assert await _row(ledger_engine, workflow_id) == before
        assert await _forks(ledger_engine, workflow_id) == 0
    finally:
        worker.release()


async def test_a_version_being_swept_elsewhere_is_left(
    dbos: None, ledger_engine: AsyncEngine, settings: Settings
) -> None:
    """Another sweep's exclusive hold leaves this sweep nothing to do for that version."""
    workflow_id = f"sweep-swept-elsewhere-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=workflow_id,
        executor_id=RETIRED_EXECUTOR,
        version=SUPERSEDED_VERSION,
    )
    async with hold_exclusive(settings, SUPERSEDED_VERSION) as hold:
        assert hold.held
        outcome = await _sweep()
    assert outcome.version_live >= 1
    assert await _forks(ledger_engine, workflow_id) == 0


async def test_only_a_sweeper_on_the_target_forks(dbos: None, ledger_engine: AsyncEngine) -> None:
    """With FUTURE latest, a SUPERSEDED rows' fork waits for a sweeper on FUTURE."""
    workflow_id = f"sweep-off-target-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=workflow_id,
        executor_id=RETIRED_EXECUTOR,
        version=OLDEST_VERSION,
    )
    async with _latest(FUTURE_VERSION):
        off_target = await _sweep(OLDEST_VERSION)
        assert workflow_id in off_target.skipped
        assert await _forks(ledger_engine, workflow_id) == 0

        on_target = await _sweep(FUTURE_VERSION)
    assert (workflow_id, recovery_successor_id(workflow_id)) in on_target.forked
    assert (await _fork_of(ledger_engine, workflow_id))["application_version"] == FUTURE_VERSION


async def test_a_version_newer_than_a_restored_target_is_left(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
    """After a rollback the replaced build's rows wait for the roll-forward."""
    workflow_id = f"sweep-newer-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=workflow_id,
        executor_id=RETIRED_EXECUTOR,
        version=FUTURE_VERSION,
    )
    async with _latest(SUPERSEDED_VERSION):
        outcome = await _sweep(SUPERSEDED_VERSION)
    assert workflow_id in outcome.newer_than_target
    assert (await _row(ledger_engine, workflow_id))["status"] == "PENDING"
    assert await _forks(ledger_engine, workflow_id) == 0


async def test_an_unregistered_versions_row_is_left_with_its_lock_free(
    dbos: None, ledger_engine: AsyncEngine, settings: Settings
) -> None:
    """A build that predates the lease holds no lock; its silence proves nothing."""
    workflow_id = f"sweep-unregistered-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=workflow_id,
        executor_id=RETIRED_EXECUTOR,
        version=UNREGISTERED_VERSION,
    )
    async with hold_exclusive(settings, UNREGISTERED_VERSION) as hold:
        assert hold.held, "the unregistered version's lock is not free"
    outcome = await _sweep()
    assert workflow_id in outcome.unregistered
    assert (await _row(ledger_engine, workflow_id))["status"] == "PENDING"


async def test_a_running_worker_keeps_its_version_from_the_sweep(
    settings: Settings, ledger_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker started by `worker.run` holds its version, so a newer sweep leaves its rows."""
    worker_version = dbos_app.APPLICATION_VERSION
    target = f"9.9.11-sweep-worker-{_RUN}"
    workflow_id = f"sweep-worker-held-{uuid4()}"
    previous_latest = await _latest_version(ledger_engine)
    worker_version_registered = worker_version in await _registered_versions(ledger_engine)
    monkeypatch.setattr(worker_main, "apply_schedules", lambda profile, settings: None)
    monkeypatch.setattr(worker_main, "_install_shutdown_handler", lambda stop: None)
    monkeypatch.setattr(worker_main, "resolve_executor_id", lambda: LIVE_EXECUTOR)

    DBOS.destroy()
    # Configured here, before `run` creates its datasource: a destroyed instance still in
    # DBOS's registry counts as launched, so a datasource created first would be refused.
    # A deployed worker is a fresh process and configures inside `run`.
    configure(settings, executor_id=LIVE_EXECUTOR)
    monkeypatch.setattr(worker_main, "configure", lambda settings, executor_id: None)
    stop = threading.Event()
    failures: list[BaseException] = []

    def serve() -> None:
        try:
            worker_main.run(settings, WorkerProfile.IO, stop=stop)
        except BaseException as error:  # Reported to the test thread below.
            failures.append(error)

    thread = threading.Thread(target=serve, name="sweep-test-worker")
    thread.start()
    try:
        for _ in range(300):
            if failures or worker_version in await _registered_versions(ledger_engine):
                break
            await asyncio.sleep(0.1)
        assert failures == []
        await _wait_for_launch()
        async with ledger_engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO lease_versions (version) VALUES (:v)"), {"v": target}
            )
        await DBOS.set_latest_application_version_async(worker_version)
        await _register_newer(ledger_engine, target)
        await _seed_pending(
            ledger_engine,
            workflow_id=workflow_id,
            executor_id=RETIRED_EXECUTOR,
            version=worker_version,
        )

        outcome = await run_recovery_sweep(
            settings, control=DBOSRecoveryControl(), own_version=target
        )

        assert outcome.version_live >= 1
        assert (await _row(ledger_engine, workflow_id))["status"] == "PENDING"
        assert await _forks(ledger_engine, workflow_id) == 0
    finally:
        async with ledger_engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM dbos.workflow_status WHERE workflow_uuid = :id"),
                {"id": workflow_id},
            )
            await connection.execute(
                text("DELETE FROM lease_versions WHERE version = ANY(:v)"),
                {"v": [target] if worker_version_registered else [target, worker_version]},
            )
            await connection.execute(
                text("DELETE FROM dbos.application_versions WHERE version_name = :v"),
                {"v": target},
            )
        if previous_latest is not None:
            await DBOS.set_latest_application_version_async(previous_latest)
        stop.set()
        await asyncio.to_thread(thread.join, 60)
        assert not thread.is_alive(), "the worker did not stop"


async def _registered_versions(engine: AsyncEngine) -> set[str]:
    """Return every version in `lease_versions`."""
    async with engine.connect() as connection:
        rows = await connection.execute(text("SELECT version FROM lease_versions"))
        return {row[0] for row in rows}


async def _wait_for_launch() -> None:
    """Wait until the worker thread's DBOS instance has launched."""
    for _ in range(300):
        try:
            await DBOS.get_latest_application_version_async()
            return
        except Exception:  # Not launched yet; DBOS raises until it is.
            await asyncio.sleep(0.1)
    raise AssertionError("the worker never launched DBOS")


async def _register_newer(engine: AsyncEngine, version: str) -> None:
    """Register `version` in DBOS as newer than every other, as a newer build's first worker."""
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO dbos.application_versions"
                " (version_id, version_name, version_timestamp, application_name)"
                " SELECT :id, :v, max(version_timestamp) + 1, :app FROM dbos.application_versions"
            ),
            {
                "id": str(uuid4()),
                "v": version,
                "app": dbos_app.application_name(Settings().environment),
            },
        )
