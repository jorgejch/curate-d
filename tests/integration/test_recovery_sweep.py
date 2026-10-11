"""The recovery sweep against the real system database.

The planner's rules are unit-tested; what can only be checked here is that each remedy
actually lands: that re-enqueueing returns a row to a queue under its own identifier,
that forking produces a row on the current version linked to the original, and that a
row a live executor holds is left exactly as it was found.

Every workflow this seeds carries a version no deployed worker runs, or none on a queue
no worker consumes, so the workers in the local stack cannot dequeue them mid-assertion
and the test observes only what the sweep did.
"""

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from dbos import DBOS
from dbos._utils import GlobalParams
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from curate_d.config import Settings
from curate_d.runtime import sweep
from curate_d.runtime.dbos_app import build_dbos_config
from curate_d.runtime.lease import ExecutorLease
from curate_d.runtime.sweep import (
    PendingWorkflow,
    RecoveryAction,
    SweepOutcome,
    plan_recovery,
    recovery_successor_id,
    run_recovery_sweep,
)

pytestmark = pytest.mark.integration

# Unique per run: liveness is a lease on this name, and a concurrent run must not share it.
LIVE_EXECUTOR = f"sweep-live-{uuid4().hex[:8]}"
RETIRED_EXECUTOR = f"sweep-retired-{uuid4().hex[:8]}"

# A version no deployed worker runs, so the local stack's workers cannot dequeue what
# this test seeds. The suffix keeps repeat runs from colliding with each other.
CURRENT_VERSION = f"9.9.9-sweep-{uuid4().hex[:8]}"
SUPERSEDED_VERSION = f"9.9.8-sweep-{uuid4().hex[:8]}"
FUTURE_VERSION = f"9.9.10-sweep-{uuid4().hex[:8]}"


@pytest.fixture
async def dbos(settings: Settings) -> AsyncIterator[None]:
    """Launch DBOS as `LIVE_EXECUTOR`, holding its lease the way a worker does."""
    DBOS.destroy()
    lease = ExecutorLease.for_settings(settings, LIVE_EXECUTOR)
    lease.acquire(timeout=10)
    config = build_dbos_config(settings, executor_id=LIVE_EXECUTOR)
    # Recovery reads the process's own version, so the instance must agree with what the
    # sweep is told is current, exactly as a deployed worker would.
    config["application_version"] = CURRENT_VERSION
    DBOS(config=config)
    DBOS.launch()
    try:
        yield
    finally:
        # Also every row on this run's versions: the sweep is database-wide and forks rows
        # other suites stranded, and a fork left behind is stranded again by the next run.
        rows = [
            *await DBOS.list_workflows_async(name="tests.sweep.stranded"),
            *await DBOS.list_workflows_async(
                app_version=[CURRENT_VERSION, SUPERSEDED_VERSION, FUTURE_VERSION]
            ),
        ]
        if rows:
            await DBOS.delete_workflows_async([row.workflow_id for row in rows])
        DBOS.destroy()
        lease.release()


async def _sweep(current_version: str = CURRENT_VERSION) -> SweepOutcome:
    """Run the sweep against the configured database, as a deployment would."""
    return await run_recovery_sweep(Settings(), current_version=current_version)


async def _seed_pending(
    engine: AsyncEngine,
    *,
    workflow_id: str,
    executor_id: str | None,
    version: str | None,
    status: str = "PENDING",
    queue_name: str | None = "sweep-unconsumed",
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
                "app": GlobalParams.app_name,
                "executor": executor_id,
                "version": version,
                "status": status,
                "queue": queue_name,
                "partition": queue_partition_key,
                "delay": delay_until_epoch_ms,
            },
        )


async def _row(engine: AsyncEngine, workflow_id: str) -> dict[str, object]:
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
    assert row["queue_name"] is not None
    # Re-enqueueing keeps the identifier, which is what preserves the idempotency key
    # the reconciler derives from content and destination.
    assert row["forked_from"] is None
    assert row["application_version"] == CURRENT_VERSION


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

    # Membership, not equality: the sweep is database-wide and other suites leave
    # pending rows behind, which it is correct for it to pick up as well.
    assert workflow_id in [original for original, _ in outcome.forked]

    fork = await _fork_of(ledger_engine, workflow_id)
    assert fork["status"] == "ENQUEUED"
    assert fork["queue_name"] is not None
    # The point of the remedy: the copy runs on a version a live worker actually serves.
    assert fork["application_version"] == CURRENT_VERSION
    assert fork["workflow_uuid"] != workflow_id, "a fork must not reuse the original id"


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

    async with ledger_engine.connect() as connection:
        forks = await connection.scalar(
            text("SELECT count(*) FROM dbos.workflow_status WHERE forked_from = :id"),
            {"id": workflow_id},
        )
    assert forks == 0


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
    assert [wid for wid, _ in outcome.failed if wid in {orphan, parked, running}] == []
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

    from curate_d.runtime.sweep import list_pending_workflows

    pending = [w for w in await list_pending_workflows() if w.workflow_id == workflow_id]
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
        (None, None),
    ],
)
async def test_fork_preserves_routing_and_retires_source(
    dbos: None,
    ledger_engine: AsyncEngine,
    status: str,
    queue: str | None,
    partition: str | None,
) -> None:
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
    assert result.failed == ()
    fork = await _fork_of(ledger_engine, original)
    assert fork["queue_name"] == (queue or "_dbos_internal_queue")
    assert fork["queue_partition_key"] == partition
    assert fork["workflow_uuid"] == recovery_successor_id(original)
    assert (await _row(ledger_engine, original))["status"] == "CANCELLED"
    assert (await _sweep()).recovered_count == 0


async def test_concurrent_sweeps_publish_one_successor(
    dbos: None, ledger_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = f"sweep-concurrent-{uuid4()}"
    await _seed_pending(
        ledger_engine,
        workflow_id=original,
        executor_id=RETIRED_EXECUTOR,
        version=SUPERSEDED_VERSION,
    )
    barrier = asyncio.Barrier(2)
    start_step = sweep._fork_start_step

    async def overlap(workflow_id: str) -> int:
        value = await start_step(workflow_id)
        await barrier.wait()
        return value

    monkeypatch.setattr(sweep, "_fork_start_step", overlap)
    first, second = await asyncio.gather(*(_sweep() for _ in range(2)))
    assert first.failed == second.failed == ()
    assert len(first.forked) + len(second.forked) == 1
    assert len(first.retired) + len(second.retired) == 1
    assert (await _fork_of(ledger_engine, original))["workflow_uuid"] == recovery_successor_id(
        original
    )
    assert (await _row(ledger_engine, original))["status"] == "CANCELLED"


async def test_failure_before_fork_commit_leaves_original_retryable(
    dbos: None, ledger_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    # Membership: the sweep is database-wide, and with fork or cancel patched to fail, so
    # does every stranded row other suites left behind.
    assert original in {wid for wid, _ in result.failed}
    assert (await _row(ledger_engine, original))["status"] == "PENDING"
    assert await DBOS.get_workflow_status_async(recovery_successor_id(original)) is None
    retry = await _sweep()
    assert retry.forked == ((original, recovery_successor_id(original)),)


@pytest.mark.parametrize("retry_version", [CURRENT_VERSION, SUPERSEDED_VERSION, FUTURE_VERSION])
async def test_crash_after_fork_is_repaired_without_another_sibling(
    dbos: None,
    ledger_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    retry_version: str,
) -> None:
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
    assert original in {wid for wid, _ in first.failed}
    assert (await _row(ledger_engine, original))["was_forked_from"] is True
    successor = recovery_successor_id(original)
    assert (await _fork_of(ledger_engine, original))["workflow_uuid"] == successor
    result = await _sweep(retry_version)
    assert result.failed == ()
    assert original in result.retired
    assert (await _row(ledger_engine, original))["status"] == "CANCELLED"
    # A new deployment/rollback can move the leaf, but cannot fork the root again.
    assert (await _fork_of(ledger_engine, original))["workflow_uuid"] == successor
    if retry_version != CURRENT_VERSION:
        assert result.forked == ((successor, recovery_successor_id(successor)),)


@pytest.mark.parametrize("version", [CURRENT_VERSION, None])
async def test_unclaimed_healthy_queue_is_untouched(
    dbos: None, ledger_engine: AsyncEngine, version: str | None
) -> None:
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
    assert result.failed == ()
    assert result.reenqueued == ()
    assert result.forked == ()
    assert result.left_running == 0
    assert await _row(ledger_engine, original) == before


async def test_delayed_retired_version_waits_until_due(
    dbos: None, ledger_engine: AsyncEngine
) -> None:
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
    assert first.recovered_count == 0
    assert await _row(ledger_engine, original) == before
    # Model the scheduler's normal DELAYED -> ENQUEUED transition when the deadline arrives.
    async with ledger_engine.begin() as connection:
        await connection.execute(
            text("UPDATE dbos.workflow_status SET status='ENQUEUED' WHERE workflow_uuid=:id"),
            {"id": original},
        )
    second = await _sweep()
    assert second.forked == ((original, recovery_successor_id(original)),)


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
    reenqueue = sweep._reenqueue

    async def observe(workflow: PendingWorkflow, executor_id: str) -> bool:
        if executor_id == executor:
            claimed_mid_sweep.append(await asyncio.to_thread(restarting.try_acquire))
        return await reenqueue(workflow, executor_id)

    monkeypatch.setattr(sweep, "_reenqueue", observe)
    try:
        outcome = await _sweep()
        assert workflow_id in outcome.reenqueued
        assert claimed_mid_sweep == [False], "the executor could lease itself mid-sweep"
        assert (await _row(ledger_engine, workflow_id))["status"] == "ENQUEUED"
        assert await asyncio.to_thread(restarting.try_acquire), "the sweep kept the lease"
    finally:
        restarting.release()
