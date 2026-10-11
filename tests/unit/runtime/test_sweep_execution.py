"""Failure boundaries around DBOS's atomic fork, retirement, and lease-based liveness."""

import inspect
from sqlite3 import IntegrityError as SQLiteIntegrityError
from unittest.mock import AsyncMock

import pytest
from dbos import DBOS
from dbos._sys_db import WorkflowStatus
from sqlalchemy.exc import IntegrityError

from curate_d.config import Settings
from curate_d.runtime import sweep
from curate_d.runtime.dbos_app import APPLICATION_NAME
from curate_d.runtime.lease import lease_key
from curate_d.runtime.sweep import PendingWorkflow, recovery_successor_id
from tests.unit.runtime.fake_lease import FakeLockServer

ORIGINAL = "original"
SUCCESSOR = recovery_successor_id(ORIGINAL)
WORKFLOW = PendingWorkflow(ORIGINAL, "retired", "v1")


def _source(*, forked: bool = False) -> WorkflowStatus:
    status = WorkflowStatus()
    status.workflow_id = ORIGINAL
    status.status = "PENDING"
    status.executor_id = "retired"
    status.app_version = "v1"
    status.queue_name = "transfer-large"
    status.queue_partition_key = "provider-1"
    status.was_forked_from = forked
    status.forked_from = None
    return status


def _successor() -> WorkflowStatus:
    status = _source()
    status.workflow_id = SUCCESSOR
    status.forked_from = ORIGINAL
    return status


async def test_listing_preserves_status_routing_and_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(forked=True)
    source.status = "ENQUEUED"
    listing = AsyncMock(return_value=[source])
    monkeypatch.setattr(DBOS, "list_workflows_async", listing)
    assert await sweep.list_pending_workflows() == (
        PendingWorkflow(
            ORIGINAL,
            "retired",
            "v1",
            status="ENQUEUED",
            queue_name="transfer-large",
            queue_partition_key="provider-1",
            was_forked_from=True,
        ),
    )
    listing.assert_awaited_once_with(
        status=["PENDING", "ENQUEUED"],
        load_input=False,
        load_output=False,
    )


async def test_unrelated_id_collision_never_cancels_source(monkeypatch: pytest.MonkeyPatch) -> None:
    collision = _source()
    monkeypatch.setattr(DBOS, "get_workflow_status_async", AsyncMock(return_value=collision))
    cancel = AsyncMock()
    monkeypatch.setattr(DBOS, "cancel_workflow_async", cancel)
    with pytest.raises(ValueError, match="unrelated workflow"):
        await sweep._recover_version(WORKFLOW, "v2")
    cancel.assert_not_awaited()


async def test_source_recheck_repairs_a_concurrent_committed_fork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DBOS,
        "get_workflow_status_async",
        AsyncMock(side_effect=[None, _source(forked=True), _successor()]),
    )
    cancel = AsyncMock()
    fork = AsyncMock()
    monkeypatch.setattr(DBOS, "cancel_workflow_async", cancel)
    monkeypatch.setattr(DBOS, "fork_workflow_async", fork)
    assert await sweep._recover_version(WORKFLOW, "v3") == (SUCCESSOR, False)
    fork.assert_not_awaited()
    cancel.assert_awaited_once_with(ORIGINAL)


async def test_an_existing_manual_fork_requires_lineage_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DBOS,
        "get_workflow_status_async",
        AsyncMock(side_effect=[None, _source(forked=True), None]),
    )
    cancel = AsyncMock()
    monkeypatch.setattr(DBOS, "cancel_workflow_async", cancel)
    with pytest.raises(ValueError, match="inspect its existing lineage"):
        await sweep._recover_version(WORKFLOW, "v2")
    cancel.assert_not_awaited()


async def test_a_cancelled_snapshot_is_not_forked_back_to_life(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source()
    source.status = "CANCELLED"
    monkeypatch.setattr(
        DBOS,
        "get_workflow_status_async",
        AsyncMock(side_effect=[None, source, None]),
    )
    fork = AsyncMock()
    monkeypatch.setattr(DBOS, "fork_workflow_async", fork)
    with pytest.raises(ValueError, match="no longer eligible"):
        await sweep._recover_version(WORKFLOW, "v2")
    fork.assert_not_awaited()


async def test_only_a_workflow_primary_key_conflict_can_be_repaired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DBOS,
        "get_workflow_status_async",
        AsyncMock(side_effect=[None, _source()]),
    )
    monkeypatch.setattr(sweep, "_fork_start_step", AsyncMock(return_value=1))
    error = IntegrityError("insert", {}, SQLiteIntegrityError("some other constraint failed"))
    monkeypatch.setattr(DBOS, "fork_workflow_async", AsyncMock(side_effect=error))
    cancel = AsyncMock()
    monkeypatch.setattr(DBOS, "cancel_workflow_async", cancel)
    with pytest.raises(IntegrityError):
        await sweep._recover_version(WORKFLOW, "v2")
    cancel.assert_not_awaited()


async def test_an_unconfirmed_duplicate_insert_does_not_cancel_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DBOS,
        "get_workflow_status_async",
        AsyncMock(side_effect=[None, _source(), None]),
    )
    monkeypatch.setattr(sweep, "_fork_start_step", AsyncMock(return_value=1))
    error = IntegrityError(
        "insert",
        {},
        SQLiteIntegrityError("UNIQUE constraint failed: workflow_status.workflow_uuid"),
    )
    monkeypatch.setattr(DBOS, "fork_workflow_async", AsyncMock(side_effect=error))
    cancel = AsyncMock()
    monkeypatch.setattr(DBOS, "cancel_workflow_async", cancel)
    with pytest.raises(IntegrityError):
        await sweep._recover_version(WORKFLOW, "v2")
    cancel.assert_not_awaited()


async def test_fork_resumes_after_last_checkpoint_with_original_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DBOS,
        "get_workflow_status_async",
        AsyncMock(side_effect=[None, _source(), _successor()]),
    )
    monkeypatch.setattr(
        DBOS,
        "list_workflow_steps_async",
        AsyncMock(return_value=[{"function_id": 1}, {"function_id": 4}]),
    )
    fork = AsyncMock()
    cancel = AsyncMock()
    monkeypatch.setattr(DBOS, "fork_workflow_async", fork)
    monkeypatch.setattr(DBOS, "cancel_workflow_async", cancel)
    assert await sweep._recover_version(WORKFLOW, "v2") == (SUCCESSOR, True)
    fork.assert_awaited_once_with(
        ORIGINAL,
        5,
        application_version="v2",
        queue_name="transfer-large",
        queue_partition_key="provider-1",
    )
    cancel.assert_awaited_once_with(ORIGINAL)


class _Reenqueues:
    """Record re-enqueues and whether the sweep held the owner's lease at the time."""

    def __init__(self, locks: FakeLockServer, *, fail: bool = False) -> None:
        self.locks = locks
        self.fail = fail
        self.calls: list[tuple[str, str, bool]] = []

    async def __call__(self, workflow: PendingWorkflow, executor_id: str) -> bool:
        held = lease_key(APPLICATION_NAME, executor_id) in self.locks.holders
        self.calls.append((workflow.workflow_id, executor_id, held))
        if self.fail:
            raise RuntimeError("database gone")
        return True


@pytest.fixture
def locks() -> FakeLockServer:
    """Return the lock server both the sweep and the simulated executors lease from."""
    return FakeLockServer()


def _sweep_over(
    monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer, *pending: PendingWorkflow
) -> _Reenqueues:
    monkeypatch.setattr(sweep, "list_pending_workflows", AsyncMock(return_value=pending))
    reenqueues = _Reenqueues(locks)
    monkeypatch.setattr(sweep, "_reenqueue", reenqueues)
    return reenqueues


async def _run(locks: FakeLockServer) -> sweep.SweepOutcome:
    return await sweep.run_recovery_sweep(
        Settings(), current_version="v1", lease_factory=locks.lease
    )


async def test_an_executor_holding_its_lease_is_never_touched(
    monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
) -> None:
    """Re-dispatching its rows would run the same placement twice from two processes."""
    assert locks.lease("worker-0").try_acquire()
    reenqueues = _sweep_over(monkeypatch, locks, PendingWorkflow("running", "worker-0", "v1"))
    outcome = await _run(locks)
    assert reenqueues.calls == []
    assert outcome.left_running == 1
    assert outcome.reenqueued == ()
    assert outcome.failed == ()


async def test_a_dead_executor_is_recovered_under_its_lease_then_released(
    monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
) -> None:
    """Held throughout, so the executor cannot start and recover the same rows mid-sweep."""
    reenqueues = _sweep_over(
        monkeypatch,
        locks,
        PendingWorkflow("a", "worker-9", "v1"),
        PendingWorkflow("b", "worker-9", "v1"),
    )
    outcome = await _run(locks)
    assert reenqueues.calls == [("a", "worker-9", True), ("b", "worker-9", True)]
    assert outcome.reenqueued == ("a", "b")
    assert locks.holders == {}, "the sweep kept the dead executor's lease"
    assert all(session.closed for session in locks.sessions)


async def test_each_executor_is_judged_by_its_own_lease(
    monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
) -> None:
    """Respect every live executor, not just the first."""
    for live in ("worker-0", "worker-1"):
        assert locks.lease(live).try_acquire()
    reenqueues = _sweep_over(
        monkeypatch,
        locks,
        PendingWorkflow("a", "worker-0", "v1"),
        PendingWorkflow("b", "worker-1", "v1"),
        PendingWorkflow("c", "worker-9", "v1"),
    )
    outcome = await _run(locks)
    assert [call[0] for call in reenqueues.calls] == ["c"]
    assert outcome.left_running == 2


async def test_an_unknown_liveness_is_never_treated_as_dead(
    monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
) -> None:
    """A lease that cannot be checked is reported, and its rows are left alone."""
    locks.refuse_connections = 1
    reenqueues = _sweep_over(monkeypatch, locks, PendingWorkflow("a", "worker-9", "v1"))
    outcome = await _run(locks)
    assert reenqueues.calls == []
    assert [workflow_id for workflow_id, _ in outcome.failed] == ["a"]
    assert "worker-9" in outcome.failed[0][1]


async def test_a_failed_reenqueue_is_reported_and_the_lease_still_released(
    monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
) -> None:
    """One bad row is a per-workflow failure, not a lease left held until restart."""
    _sweep_over(monkeypatch, locks, PendingWorkflow("a", "worker-9", "v1"))
    failing = _Reenqueues(locks, fail=True)
    monkeypatch.setattr(sweep, "_reenqueue", failing)
    outcome = await _run(locks)
    assert [workflow_id for workflow_id, _ in outcome.failed] == ["a"]
    assert locks.holders == {}


async def test_an_ownerless_pending_row_is_reported_not_reenqueued(
    monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
) -> None:
    """DBOS re-enqueues conditionally on the owner; a row without one cannot be."""
    reenqueues = _sweep_over(monkeypatch, locks, PendingWorkflow("a", None, "v1"))
    outcome = await _run(locks)
    assert reenqueues.calls == []
    assert [workflow_id for workflow_id, _ in outcome.failed] == ["a"]
    assert "without an executor" in outcome.failed[0][1]
    assert locks.sessions == []


def test_the_recovery_reenqueue_binding_still_exists() -> None:
    """Pins the private ``_sys_db.reenqueue_for_recovery`` the sweep calls."""
    from dbos._sys_db import SystemDatabase

    parameters = inspect.signature(SystemDatabase.reenqueue_for_recovery).parameters
    assert list(parameters) == ["self", "workflow_id", "executor_ids", "recovery_queue_name"]
