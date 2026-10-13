"""The gated sweep's failure boundaries, on in-memory DBOS rows, leases and version locks."""

import inspect
from sqlite3 import IntegrityError as SQLiteIntegrityError
from unittest.mock import AsyncMock, Mock

import pytest
from dbos import DBOS, DBOSClient
from dbos._context import get_local_dbos_context
from dbos._sys_db import WorkflowStatuses
from dbos._utils import INTERNAL_QUEUE_NAME
from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError

from curate_d.config import Settings
from curate_d.runtime import sweep
from curate_d.runtime.dbos_app import APPLICATION_NAME
from curate_d.runtime.lease import lease_key
from curate_d.runtime.sweep import (
    RECOVERY_SCHEDULE_NAME,
    ClientRecoveryControl,
    DBOSRecoveryControl,
    PendingWorkflow,
    SweepOutcome,
    recovery_successor_id,
    run_recovery_sweep,
)
from curate_d.runtime.version_lease import LockMode
from tests.unit.runtime.fake_lease import FakeLockServer
from tests.unit.runtime.fake_recovery import (
    FakeRecoveryControl,
    FakeRegistrations,
    FakeVersionHolds,
    registered,
    workflow_status,
)

ORIGINAL = "original"
SUCCESSOR = recovery_successor_id(ORIGINAL)
WORKFLOW = PendingWorkflow(ORIGINAL, "retired", "v1")


@pytest.fixture
def locks() -> FakeLockServer:
    """Return the lock server both the sweep and the simulated executors lease from."""
    return FakeLockServer()


async def _sweep(
    control: FakeRecoveryControl,
    *,
    own: str = "v2",
    registry: FakeRegistrations | None = None,
    holds: FakeVersionHolds | None = None,
    locks: FakeLockServer | None = None,
) -> SweepOutcome:
    """Run the sweep as a sweeper on `own`, over in-memory collaborators."""
    return await run_recovery_sweep(
        Settings(),
        control=control,
        own_version=own,
        lease_factory=(locks or FakeLockServer()).lease,
        version_holds=holds or FakeVersionHolds(),
        registrations=registry or FakeRegistrations(registered("v1", "v2")),
    )


# The adapters.


async def test_the_dbos_adapter_lists_routing_lineage_and_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every field the gate decides on comes through, own-tick schedule included."""
    row = workflow_status(
        ORIGINAL,
        status="ENQUEUED",
        queue_name="transfer-large",
        queue_partition_key="provider-1",
        was_forked_from=True,
        schedule_name=RECOVERY_SCHEDULE_NAME,
    )
    listing = AsyncMock(return_value=[row])
    monkeypatch.setattr(DBOS, "list_workflows_async", listing)
    assert await DBOSRecoveryControl().list_pending() == (
        PendingWorkflow(
            ORIGINAL,
            "retired",
            "v1",
            status="ENQUEUED",
            queue_name="transfer-large",
            queue_partition_key="provider-1",
            was_forked_from=True,
            schedule_name=RECOVERY_SCHEDULE_NAME,
        ),
    )
    listing.assert_awaited_once_with(
        status=["PENDING", "ENQUEUED"], load_input=False, load_output=False
    )


async def test_the_dbos_adapter_forks_under_the_successor_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SetWorkflowID is what makes the successor deterministic."""
    assigned: list[str] = []

    async def fork(workflow_id: str, start_step: int, **routing: object) -> None:
        context = get_local_dbos_context()
        assert context is not None
        assigned.append(context.id_assigned_for_next_workflow)

    monkeypatch.setattr(DBOS, "fork_workflow_async", fork)
    await DBOSRecoveryControl().fork(
        ORIGINAL,
        SUCCESSOR,
        3,
        application_version="v2",
        queue_name="stage",
        queue_partition_key=None,
    )
    assert assigned == [SUCCESSOR]


async def test_the_dbos_adapter_reads_the_latest_version_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The target is the version name, not the version record."""
    monkeypatch.setattr(
        DBOS,
        "get_latest_application_version_async",
        AsyncMock(return_value={"version_name": "v2", "version_id": "x"}),
    )
    assert await DBOSRecoveryControl().latest_version() == "v2"


async def test_the_dbos_adapter_reenqueues_through_the_system_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-enqueue is conditional on the owner; the default queue is never relied on."""
    calls: list[tuple[str, list[str], str]] = []

    class _SysDB:
        def reenqueue_for_recovery(
            self, workflow_id: str, executor_ids: list[str], recovery_queue_name: str
        ) -> bool:
            calls.append((workflow_id, executor_ids, recovery_queue_name))
            return True

    class _Instance:
        _sys_db = _SysDB()

    monkeypatch.setattr(sweep, "_get_dbos_instance", _Instance)
    assert await DBOSRecoveryControl().reenqueue(ORIGINAL, "worker-9")
    assert calls == [(ORIGINAL, ["worker-9"], INTERNAL_QUEUE_NAME)]


class _ClientSysDB:
    """The client's private system database, recording re-enqueues."""

    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def reenqueue_for_recovery(
        self, workflow_id: str, executor_ids: list[str], recovery_queue_name: str
    ) -> bool:
        self.calls.append("reenqueue")
        return True


class _Client:
    """The `DBOSClient` calls the client adapter makes, recorded."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._sys_db = _ClientSysDB(self.calls)

    async def list_workflows_async(self, **filters: object) -> list[object]:
        self.calls.append(f"list:{sorted(filters)}")
        return [workflow_status(ORIGINAL)] if filters.get("workflow_ids") else []

    async def fork_workflow_async(
        self, workflow_id: str, start_step: int, **routing: object
    ) -> None:
        context = get_local_dbos_context()
        assert context is not None
        self.calls.append(f"fork:{context.id_assigned_for_next_workflow}")

    async def cancel_workflow_async(self, workflow_id: str) -> None:
        self.calls.append("cancel")

    async def get_latest_application_version_async(self) -> dict[str, str]:
        return {"version_name": "v2"}


async def test_the_client_adapter_checks_the_lease_before_every_write() -> None:
    """A lost standalone version lease stops the next fork, cancel or re-enqueue."""
    client = _Client()
    checks: list[str] = []
    control = ClientRecoveryControl(
        client,  # type: ignore[arg-type]
        before_write=lambda: checks.append("checked"),
    )
    await control.fork(
        ORIGINAL,
        SUCCESSOR,
        1,
        application_version="v2",
        queue_name="stage",
        queue_partition_key=None,
    )
    await control.cancel(ORIGINAL)
    assert await control.reenqueue(ORIGINAL, "worker-9")
    assert await control.latest_version() == "v2"
    status = await control.status(ORIGINAL)
    assert status is not None and status.workflow_id == ORIGINAL
    assert checks == ["checked"] * 3
    assert client.calls[:3] == [f"fork:{SUCCESSOR}", "cancel", "reenqueue"]


@pytest.mark.parametrize("operation", ["fork", "cancel", "reenqueue"])
async def test_a_lost_lease_stops_the_client_adapters_write(operation: str) -> None:
    """The write never reaches DBOS once the lease check raises."""
    client = _Client()

    def lost() -> None:
        raise RuntimeError("version lease lost")

    control = ClientRecoveryControl(client, before_write=lost)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="version lease lost"):
        if operation == "fork":
            await control.fork(
                ORIGINAL,
                SUCCESSOR,
                1,
                application_version="v2",
                queue_name="stage",
                queue_partition_key="provider-1",
            )
        elif operation == "cancel":
            await control.cancel(ORIGINAL)
        else:
            await control.reenqueue(ORIGINAL, "worker-9")
    assert client.calls == []


def test_the_recovery_reenqueue_binding_still_exists() -> None:
    """Pins the private ``_sys_db.reenqueue_for_recovery`` both adapters call."""
    from dbos._sys_db import SystemDatabase

    parameters = inspect.signature(SystemDatabase.reenqueue_for_recovery).parameters
    assert list(parameters) == ["self", "workflow_id", "executor_ids", "recovery_queue_name"]


def test_the_client_still_holds_its_system_database_privately() -> None:
    """Pins ``DBOSClient._sys_db``, which the operator sweep re-enqueues through."""
    assert "self._sys_db" in inspect.getsource(DBOSClient.__init__)


# One successor per original.


async def test_unrelated_id_collision_never_cancels_source() -> None:
    """A successor ID held by another workflow is a failure, not a repair."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL), workflow_status(SUCCESSOR)])
    with pytest.raises(ValueError, match="unrelated workflow"):
        await sweep._recover_version(control, WORKFLOW, "v2")
    assert control.writes() == []


async def test_a_committed_successor_is_reused_and_the_source_retired() -> None:
    """A crash between fork and cancel is repaired without a second lineage."""
    control = FakeRecoveryControl(
        [
            workflow_status(ORIGINAL, was_forked_from=True),
            workflow_status(SUCCESSOR, status="ENQUEUED", version="v2", forked_from=ORIGINAL),
        ]
    )
    assert await sweep._recover_version(control, WORKFLOW, "v3") == (SUCCESSOR, False)
    assert control.writes() == [("cancel", ORIGINAL)]


async def test_an_existing_manual_fork_requires_lineage_review() -> None:
    """A hand fork's successor is not the recovery successor; leave the original alone."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL, was_forked_from=True)])
    with pytest.raises(ValueError, match="inspect its existing lineage"):
        await sweep._recover_version(control, WORKFLOW, "v2")
    assert control.writes() == []


async def test_a_cancelled_snapshot_is_not_forked_back_to_life() -> None:
    """A row finished after planning stays finished."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL, status="CANCELLED")])
    with pytest.raises(ValueError, match="no longer eligible"):
        await sweep._recover_version(control, WORKFLOW, "v2")
    assert control.writes() == []


async def test_only_a_workflow_primary_key_conflict_can_be_repaired() -> None:
    """Any other integrity error stays a failure, and the original is not cancelled."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL)])
    control.errors[("fork", ORIGINAL)] = IntegrityError(
        "insert", {}, SQLiteIntegrityError("some other constraint failed")
    )
    with pytest.raises(IntegrityError):
        await sweep._recover_version(control, WORKFLOW, "v2")
    assert ("cancel", ORIGINAL) not in control.calls


async def test_an_unconfirmed_duplicate_insert_does_not_cancel_source() -> None:
    """A conflict without the matching successor is not another sweep's win."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL)])
    control.errors[("fork", ORIGINAL)] = IntegrityError(
        "insert",
        {},
        SQLiteIntegrityError("UNIQUE constraint failed: workflow_status.workflow_uuid"),
    )
    with pytest.raises(IntegrityError):
        await sweep._recover_version(control, WORKFLOW, "v2")
    assert ("cancel", ORIGINAL) not in control.calls


async def test_fork_resumes_after_last_checkpoint_with_original_routing() -> None:
    """Committed steps are kept, and the successor keeps its queue and partition."""
    control = FakeRecoveryControl(
        [workflow_status(ORIGINAL, queue_name="transfer-large", queue_partition_key="provider-1")],
        steps={ORIGINAL: [1, 4]},
    )
    assert await sweep._recover_version(control, WORKFLOW, "v2") == (SUCCESSOR, True)
    assert control.writes() == [("fork", ORIGINAL, SUCCESSOR, "5"), ("cancel", ORIGINAL)]
    successor = control.rows[SUCCESSOR]
    assert (successor.queue_name, successor.queue_partition_key) == ("transfer-large", "provider-1")


# The target.


async def test_the_plan_runs_against_the_latest_version() -> None:
    """A registered, older, unheld version's row is forked onto the target."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL, version="v1")], latest="v2")
    holds = FakeVersionHolds()
    outcome = await _sweep(control, holds=holds)
    assert outcome.forked == ((ORIGINAL, SUCCESSOR),)
    assert control.rows[SUCCESSOR].app_version == "v2"
    assert holds.events == ["hold:v1", "release:v1"]


async def test_a_sweeper_off_the_target_forks_nothing_but_still_reenqueues(
    locks: FakeLockServer,
) -> None:
    """Mid-rollout an old replica's tick must not fork the new version's rows backwards."""
    control = FakeRecoveryControl(
        [
            workflow_status("parked", version="v1"),
            workflow_status("orphan", version="v3", executor_id="worker-9"),
        ],
        latest="v3",
    )
    registry = FakeRegistrations(registered("v1", "v2", "v3"))
    outcome = await _sweep(control, own="v2", registry=registry, locks=locks)
    assert outcome.skipped == ("parked",)
    assert outcome.reenqueued == ("orphan",)
    assert outcome.forked == ()
    assert [call[0] for call in control.writes()] == ["reenqueue"]
    assert registry.reads == 0


async def test_a_target_change_after_the_lock_stops_forking() -> None:
    """The sweep re-reads latest under the lock and stops if it moved."""
    control = FakeRecoveryControl(
        [workflow_status("a", version="v1"), workflow_status("b", version="v0")],
        latest=["v2", "v3"],
    )
    holds = FakeVersionHolds()
    outcome = await _sweep(
        control, registry=FakeRegistrations(registered("v0", "v1", "v2")), holds=holds
    )
    assert outcome.forked == ()
    assert sorted(outcome.skipped) == ["a", "b"]
    assert control.writes() == []
    assert holds.held == set()
    assert len([event for event in holds.events if event.startswith("hold:")]) == 1


async def test_an_unreadable_latest_version_writes_nothing() -> None:
    """Without a target nothing can be planned."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL, executor_id="worker-9", version="v2")])
    control.errors[("latest_version", "*")] = OSError("database gone")
    with pytest.raises(OSError):
        await _sweep(control)
    assert control.writes() == []


async def test_an_unreadable_listing_writes_nothing() -> None:
    """A failed listing ends the sweep before any write."""
    control = FakeRecoveryControl()
    control.errors[("list_pending", "*")] = OSError("database gone")
    with pytest.raises(OSError):
        await _sweep(control)
    assert control.writes() == []


# The source.


async def test_an_unregistered_version_is_left_alone() -> None:
    """A build that predates the lease holds no lock, so its silence proves nothing."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL, version="v0")])
    holds = FakeVersionHolds()
    outcome = await _sweep(control, holds=holds)
    assert outcome.unregistered == (ORIGINAL,)
    assert control.writes() == []
    assert holds.events == []


async def test_a_version_newer_than_the_target_is_left_alone() -> None:
    """After a rollback the replaced build's rows wait for the roll-forward."""
    registry = FakeRegistrations(registered("v1", "v2"))
    control = FakeRecoveryControl([workflow_status(ORIGINAL, version="v2")], latest="v1")
    outcome = await _sweep(control, own="v1", registry=registry)
    assert outcome.newer_than_target == (ORIGINAL,)
    assert control.writes() == []


async def test_an_unreadable_registry_writes_nothing(locks: FakeLockServer) -> None:
    """The registry is read before any write, re-enqueues included."""
    control = FakeRecoveryControl(
        [
            workflow_status(ORIGINAL, version="v1"),
            workflow_status("orphan", version="v2", executor_id="worker-9"),
        ]
    )
    with pytest.raises(OSError):
        await _sweep(control, registry=FakeRegistrations(error=OSError("down")), locks=locks)
    assert control.writes() == []


async def test_an_unregistered_target_forks_nothing() -> None:
    """Without the target's registration the deploy order is unknown."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL, version="v1")])
    outcome = await _sweep(control, registry=FakeRegistrations(registered("v1")))
    assert [workflow_id for workflow_id, _ in outcome.failed] == [ORIGINAL]
    assert "is not registered" in outcome.failed[0][1]
    assert control.writes() == []


# The lock.


@pytest.mark.parametrize("mode", [LockMode.SHARED, LockMode.EXCLUSIVE])
async def test_a_held_version_lock_leaves_its_rows(mode: LockMode) -> None:
    """Live workers hold it shared; another sweep holds it exclusively."""
    control = FakeRecoveryControl(
        [
            workflow_status("fork-me", version="v1"),
            workflow_status("retire-me", version="v1", was_forked_from=True),
        ]
    )
    outcome = await _sweep(control, holds=FakeVersionHolds(held_elsewhere={"v1": mode}))
    assert outcome.version_live == 2
    assert outcome.left_running == 0
    assert control.writes() == []


async def test_a_target_version_retirement_takes_no_version_lock() -> None:
    """The sweeper holds the target's shared lease itself."""
    control = FakeRecoveryControl(
        [
            workflow_status(ORIGINAL, version="v2", was_forked_from=True),
            workflow_status(SUCCESSOR, status="ENQUEUED", version="v2", forked_from=ORIGINAL),
        ]
    )
    holds = FakeVersionHolds()
    outcome = await _sweep(control, holds=holds)
    assert outcome.retired == (ORIGINAL,)
    assert holds.events == []


async def test_the_lock_is_released_when_a_fork_fails() -> None:
    """A failed fork leaves the original uncancelled and the version unlocked."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL, version="v1")])
    control.errors[("fork", ORIGINAL)] = RuntimeError("fork exploded")
    holds = FakeVersionHolds()
    outcome = await _sweep(control, holds=holds)
    assert [workflow_id for workflow_id, _ in outcome.failed] == [ORIGINAL]
    assert "fork exploded" in outcome.failed[0][1]
    assert ("cancel", ORIGINAL) not in control.calls
    assert holds.events == ["hold:v1", "release:v1"]


async def test_a_lock_error_fails_that_version_and_continues() -> None:
    """Nothing of the failing version is forked; other versions still are."""
    control = FakeRecoveryControl(
        [workflow_status("a", version="v0"), workflow_status("b", version="v1")], latest="v2"
    )
    holds = FakeVersionHolds(errors={"v0": OSError("lock failed")})
    outcome = await _sweep(
        control, registry=FakeRegistrations(registered("v0", "v1", "v2")), holds=holds
    )
    assert [workflow_id for workflow_id, _ in outcome.failed] == ["a"]
    assert "holding version 'v0'" in outcome.failed[0][1]
    assert [original for original, _ in outcome.forked] == ["b"]


async def test_a_crash_after_fork_is_retired_under_the_source_lock() -> None:
    """The committed successor is confirmed and the original cancelled, nothing more."""
    control = FakeRecoveryControl(
        [
            workflow_status(ORIGINAL, version="v1", was_forked_from=True),
            workflow_status(SUCCESSOR, status="ENQUEUED", version="v2", forked_from=ORIGINAL),
        ]
    )
    holds = FakeVersionHolds()
    outcome = await _sweep(control, holds=holds)
    assert outcome.retired == (ORIGINAL,)
    assert outcome.forked == ()
    assert control.writes() == [("cancel", ORIGINAL)]
    assert holds.events == ["hold:v1", "release:v1"]


# Own ticks and queue routing.


async def test_a_stale_tick_of_a_retired_version_is_cancelled_not_forked() -> None:
    """Forking a tick would run a stale sweep; the next tick does its work."""
    control = FakeRecoveryControl(
        [
            workflow_status(
                "tick", version="v1", queue_name="maintenance", schedule_name=RECOVERY_SCHEDULE_NAME
            )
        ]
    )
    holds = FakeVersionHolds()
    outcome = await _sweep(control, holds=holds)
    assert outcome.retired_ticks == ("tick",)
    assert control.writes() == [("cancel", "tick")]
    assert holds.events == ["hold:v1", "release:v1"]


async def test_a_stale_tick_of_a_live_version_is_left() -> None:
    """The gate applies to own ticks as to any fork."""
    control = FakeRecoveryControl(
        [workflow_status("tick", version="v1", schedule_name=RECOVERY_SCHEDULE_NAME)]
    )
    outcome = await _sweep(control, holds=FakeVersionHolds(held_elsewhere={"v1": LockMode.SHARED}))
    assert outcome.retired_ticks == ()
    assert outcome.version_live == 1
    assert control.writes() == []


async def test_a_dead_executors_stale_tick_is_cancelled_not_reenqueued(
    locks: FakeLockServer,
) -> None:
    """Re-enqueueing it would duplicate the next tick's work."""
    control = FakeRecoveryControl(
        [
            workflow_status(
                "tick", version="v2", executor_id="worker-9", schedule_name=RECOVERY_SCHEDULE_NAME
            )
        ]
    )
    outcome = await _sweep(control, locks=locks)
    assert outcome.retired_ticks == ("tick",)
    assert outcome.reenqueued == ()
    assert control.writes() == [("cancel", "tick")]


@pytest.mark.parametrize("queue", [None, INTERNAL_QUEUE_NAME])
async def test_a_fork_onto_no_named_queue_is_refused(queue: str | None) -> None:
    """Every worker dequeues the internal queue, so the row could cross the profile split."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL, version="v1", queue_name=queue)])
    outcome = await _sweep(control)
    assert [workflow_id for workflow_id, _ in outcome.failed] == [ORIGINAL]
    assert "refused" in outcome.failed[0][1]
    assert control.writes() == []


@pytest.mark.parametrize("queue", [None, INTERNAL_QUEUE_NAME])
async def test_a_reenqueue_onto_no_named_queue_is_refused(
    queue: str | None, locks: FakeLockServer
) -> None:
    """A dead executor's row without a named queue is reported and left."""
    control = FakeRecoveryControl(
        [workflow_status(ORIGINAL, version="v2", executor_id="worker-9", queue_name=queue)]
    )
    outcome = await _sweep(control, locks=locks)
    assert [workflow_id for workflow_id, _ in outcome.failed] == [ORIGINAL]
    assert control.writes() == []
    assert locks.sessions == []


async def test_retiring_a_row_without_a_queue_still_cancels_it() -> None:
    """Cancelling needs no queue."""
    control = FakeRecoveryControl(
        [
            workflow_status(ORIGINAL, version="v2", queue_name=None, was_forked_from=True),
            workflow_status(SUCCESSOR, status="ENQUEUED", version="v2", forked_from=ORIGINAL),
        ]
    )
    outcome = await _sweep(control)
    assert outcome.retired == (ORIGINAL,)
    assert outcome.failed == ()


async def test_a_reenqueued_row_keeps_its_queue(locks: FakeLockServer) -> None:
    """The row goes back to the queue it names, not to the internal one."""
    control = FakeRecoveryControl(
        [workflow_status(ORIGINAL, version="v2", executor_id="worker-9", queue_name="stage")]
    )
    outcome = await _sweep(control, locks=locks)
    assert outcome.reenqueued == (ORIGINAL,)
    assert control.rows[ORIGINAL].queue_name == "stage"


# Executor liveness.


class _LeaseAwareControl(FakeRecoveryControl):
    """Record whether the sweep held the owner's lease at each re-enqueue."""

    def __init__(self, locks: FakeLockServer, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.locks = locks
        self.held_at_reenqueue: list[bool] = []

    async def reenqueue(self, workflow_id: str, executor_id: str) -> bool:
        self.held_at_reenqueue.append(
            lease_key(APPLICATION_NAME, executor_id) in self.locks.holders
        )
        return await super().reenqueue(workflow_id, executor_id)


async def test_an_executor_holding_its_lease_is_never_touched(locks: FakeLockServer) -> None:
    """Re-dispatching its rows would run the same placement twice from two processes."""
    assert locks.lease("worker-0").try_acquire()
    control = FakeRecoveryControl(
        [workflow_status("running", executor_id="worker-0", version="v2")]
    )
    outcome = await _sweep(control, locks=locks)
    assert control.writes() == []
    assert outcome.left_running == 1
    assert outcome.failed == ()


async def test_a_dead_executor_is_recovered_under_its_lease_then_released(
    locks: FakeLockServer,
) -> None:
    """Held throughout, so the executor cannot start and recover the same rows mid-sweep."""
    control = _LeaseAwareControl(
        locks,
        [
            workflow_status("a", executor_id="worker-9", version="v2"),
            workflow_status("b", executor_id="worker-9", version="v2"),
        ],
    )
    outcome = await _sweep(control, locks=locks)
    assert outcome.reenqueued == ("a", "b")
    assert control.held_at_reenqueue == [True, True]
    assert locks.holders == {}, "the sweep kept the dead executor's lease"
    assert all(session.closed for session in locks.sessions)


async def test_each_executor_is_judged_by_its_own_lease(locks: FakeLockServer) -> None:
    """Respect every live executor, not just the first."""
    for live in ("worker-0", "worker-1"):
        assert locks.lease(live).try_acquire()
    control = FakeRecoveryControl(
        [
            workflow_status("a", executor_id="worker-0", version="v2"),
            workflow_status("b", executor_id="worker-1", version="v2"),
            workflow_status("c", executor_id="worker-9", version="v2"),
        ]
    )
    outcome = await _sweep(control, locks=locks)
    assert outcome.reenqueued == ("c",)
    assert outcome.left_running == 2


async def test_an_unknown_liveness_is_never_treated_as_dead(locks: FakeLockServer) -> None:
    """A lease that cannot be checked is reported, and its rows are left alone."""
    locks.refuse_connections = 1
    control = FakeRecoveryControl([workflow_status("a", executor_id="worker-9", version="v2")])
    outcome = await _sweep(control, locks=locks)
    assert control.writes() == []
    assert [workflow_id for workflow_id, _ in outcome.failed] == ["a"]
    assert "worker-9" in outcome.failed[0][1]


async def test_a_failed_reenqueue_is_reported_and_the_lease_still_released(
    locks: FakeLockServer,
) -> None:
    """One bad row is a per-workflow failure, not a lease left held until restart."""
    control = FakeRecoveryControl(
        [
            workflow_status("a", executor_id="worker-9", version="v2"),
            workflow_status("b", executor_id="worker-9", version="v2"),
        ]
    )
    control.errors[("reenqueue", "a")] = RuntimeError("database gone")
    outcome = await _sweep(control, locks=locks)
    assert [workflow_id for workflow_id, _ in outcome.failed] == ["a"]
    assert outcome.reenqueued == ("b",)
    assert locks.holders == {}


async def test_an_ownerless_pending_row_is_reported_not_reenqueued(locks: FakeLockServer) -> None:
    """DBOS re-enqueues conditionally on the owner; a row without one cannot be."""
    control = FakeRecoveryControl([workflow_status("a", executor_id=None, version="v2")])
    outcome = await _sweep(control, locks=locks)
    assert control.writes() == []
    assert [workflow_id for workflow_id, _ in outcome.failed] == ["a"]
    assert "without an executor" in outcome.failed[0][1]
    assert locks.sessions == []


async def test_equal_registration_times_do_not_establish_deploy_order() -> None:
    """A tied timestamp cannot authorize recovery of either version's rows."""
    timestamp = registered("v1")["v1"]
    control = FakeRecoveryControl([workflow_status(ORIGINAL)])
    holds = FakeVersionHolds()
    outcome = await _sweep(
        control,
        registry=FakeRegistrations({"v1": timestamp, "v2": timestamp}),
        holds=holds,
    )
    assert outcome == SweepOutcome(newer_than_target=(ORIGINAL,))
    assert holds.events == []
    assert control.writes() == []


async def test_a_target_change_after_one_version_preserves_completed_recovery() -> None:
    """A new target stops subsequent source groups without losing the earlier tally."""
    control = FakeRecoveryControl(
        [workflow_status(name, version=name) for name in ("v0", "v1", "v2")],
        latest=["v3", "v3", "v4"],
    )
    holds = FakeVersionHolds()
    outcome = await _sweep(
        control,
        own="v3",
        registry=FakeRegistrations(registered("v0", "v1", "v2", "v3")),
        holds=holds,
    )
    assert outcome == SweepOutcome(
        forked=(("v0", recovery_successor_id("v0")),), skipped=("v1", "v2")
    )
    assert holds.events == ["hold:v0", "release:v0", "hold:v1", "release:v1"]
    assert holds.held == set()
    assert control.rows["v0"].status == "CANCELLED"
    assert control.rows["v1"].status == control.rows["v2"].status == "PENDING"


async def test_a_failed_row_does_not_strand_other_rows_of_its_version() -> None:
    """The source lock stays available for remaining rows after a checkpoint read fails."""
    control = FakeRecoveryControl([workflow_status("bad"), workflow_status("good")])
    control.errors[("step_ids", "bad")] = OSError("checkpoint read failed")
    holds = FakeVersionHolds()
    outcome = await _sweep(control, holds=holds)
    assert outcome.forked == (("good", recovery_successor_id("good")),)
    assert len(outcome.failed) == 1
    assert outcome.failed[0][0] == "bad"
    assert "checkpoint read failed" in outcome.failed[0][1]
    assert control.rows["bad"].status == "PENDING"
    assert ("cancel", "bad") not in control.writes()
    assert holds.events == ["hold:v1", "release:v1"]


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("queue", [None, INTERNAL_QUEUE_NAME])
async def test_stale_ticks_can_be_retired_without_a_routable_queue(
    version: str, queue: str | None
) -> None:
    """Both version and executor recovery cancel stale ticks without needing routing."""
    control = FakeRecoveryControl(
        [
            workflow_status(
                "tick", version=version, queue_name=queue, schedule_name=RECOVERY_SCHEDULE_NAME
            )
        ]
    )
    outcome = await _sweep(control)
    assert outcome == SweepOutcome(retired_ticks=("tick",))
    assert control.writes() == [("cancel", "tick")]


@pytest.mark.parametrize("version", ["v1", "v2"])
@pytest.mark.parametrize("state", [None, "SUCCESS", "ERROR", "CANCELLED", "DELAYED"])
async def test_a_tick_that_became_inactive_after_listing_is_left_alone(
    monkeypatch: pytest.MonkeyPatch, version: str, state: WorkflowStatuses | None
) -> None:
    """The fresh status prevents cancellation of a completed, delayed or deleted tick."""
    row = workflow_status("tick", version=version, schedule_name=RECOVERY_SCHEDULE_NAME)
    control = FakeRecoveryControl([row])
    snapshot = await control.list_pending()
    monkeypatch.setattr(control, "list_pending", AsyncMock(return_value=snapshot))
    if state is None:
        del control.rows["tick"]
    else:
        row.status = state
    outcome = await _sweep(control)
    assert outcome == SweepOutcome()
    assert control.writes() == []


@pytest.mark.parametrize("version", ["v1", "v2"])
async def test_a_tick_cancel_failure_is_reported_and_releases_its_locks(
    version: str, locks: FakeLockServer
) -> None:
    """A failed tick retirement is never counted as recovered or left holding a lease."""
    control = FakeRecoveryControl(
        [workflow_status("tick", version=version, schedule_name=RECOVERY_SCHEDULE_NAME)]
    )
    control.errors[("cancel", "tick")] = OSError("cancel failed")
    holds = FakeVersionHolds()
    outcome = await _sweep(control, holds=holds, locks=locks)
    assert len(outcome.failed) == 1
    assert outcome.failed[0][0] == "tick"
    assert "cancel failed" in outcome.failed[0][1]
    assert outcome.retired_ticks == ()
    assert outcome.recovered_count == 0
    assert control.rows["tick"].status == "PENDING"
    assert holds.held == set()
    assert locks.holders == {}
    assert all(session.closed for session in locks.sessions)


async def test_a_live_executors_current_tick_is_not_cancelled(locks: FakeLockServer) -> None:
    """The running sweep must not cancel its own tick on the target version."""
    worker = locks.lease("worker-0", version="v2")
    assert worker.try_acquire()
    try:
        control = FakeRecoveryControl(
            [
                workflow_status(
                    "tick",
                    executor_id="worker-0",
                    version="v2",
                    schedule_name=RECOVERY_SCHEDULE_NAME,
                )
            ]
        )
        outcome = await _sweep(control, locks=locks)
        assert outcome == SweepOutcome(left_running=1)
        assert control.writes() == []
        assert worker.held
    finally:
        worker.release()


async def test_other_schedules_ticks_still_receive_normal_recovery() -> None:
    """Only recovery-sweep ticks are disposable; other scheduled work keeps its lineage."""
    control = FakeRecoveryControl(
        [workflow_status(ORIGINAL, schedule_name="history-prune", queue_name="maintenance")]
    )
    outcome = await _sweep(control)
    assert outcome == SweepOutcome(forked=((ORIGINAL, SUCCESSOR),))
    assert control.rows[SUCCESSOR].queue_name == "maintenance"


async def test_an_empty_sweep_never_reads_the_registry_or_takes_locks(
    locks: FakeLockServer,
) -> None:
    """No eligible rows means no dependency on version registry availability."""
    registry = FakeRegistrations(error=OSError("registry unavailable"))
    holds = FakeVersionHolds()
    outcome = await _sweep(FakeRecoveryControl(), registry=registry, holds=holds, locks=locks)
    assert outcome == SweepOutcome()
    assert registry.reads == 0
    assert holds.events == []
    assert locks.sessions == []


@pytest.mark.parametrize("adapter", ["launched", "client"])
@pytest.mark.parametrize("step_ids", [[], [4, 1, 3]])
async def test_adapters_read_checkpoint_ids_without_loading_outputs(
    monkeypatch: pytest.MonkeyPatch, adapter: str, step_ids: list[int]
) -> None:
    """Checkpoint discovery keeps every ID without loading potentially large outputs."""
    client = Mock(spec=DBOSClient)
    boundary = DBOS if adapter == "launched" else client
    listing = AsyncMock(return_value=[{"function_id": step_id} for step_id in step_ids])
    monkeypatch.setattr(boundary, "list_workflow_steps_async", listing)
    guard = Mock()
    control = (
        DBOSRecoveryControl()
        if adapter == "launched"
        else ClientRecoveryControl(client, before_write=guard)
    )

    assert await control.step_ids(ORIGINAL) == step_ids
    listing.assert_awaited_once_with(ORIGINAL, load_output=False)
    guard.assert_not_called()


async def test_the_client_adapter_maps_pending_rows_without_a_write_guard() -> None:
    """Operator reads preserve routing, lineage and schedule without requiring a live lease."""
    client = Mock(spec=DBOSClient)
    row = workflow_status(
        ORIGINAL,
        status="ENQUEUED",
        executor_id=None,
        queue_name="maintenance",
        queue_partition_key="provider-1",
        was_forked_from=True,
        schedule_name=RECOVERY_SCHEDULE_NAME,
    )
    client.list_workflows_async.return_value = [row]
    guard = Mock()
    control = ClientRecoveryControl(client, before_write=guard)

    assert await control.list_pending() == (
        PendingWorkflow(
            ORIGINAL,
            None,
            "v1",
            status="ENQUEUED",
            queue_name="maintenance",
            queue_partition_key="provider-1",
            was_forked_from=True,
            schedule_name=RECOVERY_SCHEDULE_NAME,
        ),
    )
    client.list_workflows_async.assert_awaited_once_with(
        status=["PENDING", "ENQUEUED"], load_input=False, load_output=False
    )
    guard.assert_not_called()


async def test_the_client_adapter_returns_none_for_a_deleted_workflow() -> None:
    """A missing workflow is returned as absent instead of indexing an empty listing."""
    client = Mock(spec=DBOSClient)
    client.list_workflows_async.return_value = []
    guard = Mock()

    assert await ClientRecoveryControl(client, before_write=guard).status(ORIGINAL) is None
    client.list_workflows_async.assert_awaited_once_with(
        workflow_ids=[ORIGINAL], load_input=False, load_output=False
    )
    guard.assert_not_called()


@pytest.mark.parametrize("adapter", ["launched", "client"])
async def test_adapters_preserve_fork_routing_and_restore_context_after_failure(
    monkeypatch: pytest.MonkeyPatch, adapter: str
) -> None:
    """A failed fork forwards its routing and cannot leak its successor ID to later work."""
    client = Mock(spec=DBOSClient)
    boundary = DBOS if adapter == "launched" else client
    error = RuntimeError("fork unavailable")
    assigned: list[str] = []

    async def fail(*args: object, **kwargs: object) -> None:
        """Record the assigned successor inside the failing DBOS call."""
        context = get_local_dbos_context()
        assert context is not None
        assigned.append(context.id_assigned_for_next_workflow)
        raise error

    fork = AsyncMock(side_effect=fail)
    monkeypatch.setattr(boundary, "fork_workflow_async", fork)
    guard = Mock()
    control = (
        DBOSRecoveryControl()
        if adapter == "launched"
        else ClientRecoveryControl(client, before_write=guard)
    )
    previous_context = get_local_dbos_context()
    previous_id = (
        None if previous_context is None else previous_context.id_assigned_for_next_workflow
    )

    with pytest.raises(RuntimeError) as raised:
        await control.fork(
            ORIGINAL,
            SUCCESSOR,
            5,
            application_version="v2",
            queue_name="transfer-large",
            queue_partition_key="provider-1",
        )
    assert raised.value is error
    fork.assert_awaited_once_with(
        ORIGINAL,
        5,
        application_version="v2",
        queue_name="transfer-large",
        queue_partition_key="provider-1",
    )
    assert assigned == [SUCCESSOR]
    assert get_local_dbos_context() is previous_context
    if previous_context is not None:
        assert previous_context.id_assigned_for_next_workflow == previous_id
    if adapter == "client":
        guard.assert_called_once_with()


@pytest.mark.parametrize("step_ids, start_step", [([], 1), ([0], 1), ([4, 1, 3], 5)])
async def test_recovery_resumes_after_the_highest_checkpoint(
    step_ids: list[int], start_step: int
) -> None:
    """An empty or unordered checkpoint listing never skips or repeats committed work."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL)], steps={ORIGINAL: step_ids})

    assert await sweep._recover_version(control, WORKFLOW, "v2") == (SUCCESSOR, True)
    assert control.writes() == [
        ("fork", ORIGINAL, SUCCESSOR, str(start_step)),
        ("cancel", ORIGINAL),
    ]


@pytest.mark.parametrize("workflow_id_conflict", [True, False])
async def test_a_concurrent_successor_repairs_only_a_workflow_id_conflict(
    monkeypatch: pytest.MonkeyPatch, workflow_id_conflict: bool
) -> None:
    """A confirmed concurrent successor permits retirement only for the expected conflict."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL)])
    error = IntegrityError(
        "insert",
        {},
        SQLiteIntegrityError(
            "UNIQUE constraint failed: workflow_status.workflow_uuid"
            if workflow_id_conflict
            else "UNIQUE constraint failed: workflow_status.name"
        ),
    )

    async def concurrent_fork(*args: object, **kwargs: object) -> None:
        """Publish the other sweeper's successor before reporting this insert's failure."""
        control.rows[SUCCESSOR] = workflow_status(
            SUCCESSOR, status="ENQUEUED", version="v2", forked_from=ORIGINAL
        )
        raise error

    fork = AsyncMock(side_effect=concurrent_fork)
    monkeypatch.setattr(control, "fork", fork)
    if workflow_id_conflict:
        assert await sweep._recover_version(control, WORKFLOW, "v2") == (SUCCESSOR, False)
        assert control.writes() == [("cancel", ORIGINAL)]
    else:
        with pytest.raises(IntegrityError) as raised:
            await sweep._recover_version(control, WORKFLOW, "v2")
        assert raised.value is error
        assert control.writes() == []
        assert control.rows[ORIGINAL].status == "PENDING"
    fork.assert_awaited_once()


async def test_a_fork_that_publishes_no_successor_never_cancels_its_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful adapter return is insufficient without a committed matching successor."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL)])
    fork = AsyncMock()
    monkeypatch.setattr(control, "fork", fork)

    with pytest.raises(ValueError, match="did not publish its expected successor"):
        await sweep._recover_version(control, WORKFLOW, "v2")
    fork.assert_awaited_once()
    assert control.writes() == []
    assert control.rows[ORIGINAL].status == "PENDING"


async def test_a_retry_repairs_failed_cancellation_without_forking_again() -> None:
    """A retry after deployment retires the ancestor and preserves its committed successor."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL)])
    control.errors[("cancel", ORIGINAL)] = OSError("cancel unavailable")
    holds = FakeVersionHolds()

    first = await _sweep(control, holds=holds)
    assert len(first.failed) == 1
    assert first.failed[0][0] == ORIGINAL
    assert "cancel unavailable" in first.failed[0][1]
    assert first.forked == ()
    assert first.retired == ()
    successor = control.rows[SUCCESSOR]
    assert successor.app_version == "v2"
    assert control.rows[ORIGINAL].status == "PENDING"

    control.errors.clear()
    # Roll back to v1: v2 is now newer than the target, so the committed leaf stays parked.
    control.latest_version = AsyncMock(return_value="v1")  # type: ignore[method-assign]
    second = await _sweep(control, own="v1", holds=holds)

    assert second == SweepOutcome(retired=(ORIGINAL,), newer_than_target=(SUCCESSOR,))
    assert control.rows[SUCCESSOR] is successor
    assert control.rows[ORIGINAL].status == "CANCELLED"
    assert [call for call in control.writes() if call[0] == "fork"] == [
        ("fork", ORIGINAL, SUCCESSOR, "1")
    ]
    assert holds.held == set()


@pytest.mark.parametrize("constraint", ["workflow_status_pkey", "other_unique_key", None])
def test_postgres_conflicts_require_the_workflow_primary_key(constraint: str | None) -> None:
    """PostgreSQL's diagnostic constraint name must identify the deterministic ID conflict."""
    error = IntegrityError(
        "insert",
        {},
        UniqueViolation(
            "duplicate key", info={} if constraint is None else {ord("n"): constraint.encode()}
        ),
    )
    assert sweep._is_workflow_id_conflict(error) is (constraint == "workflow_status_pkey")


@pytest.mark.parametrize("change", ["owner", "status", "deleted"])
async def test_conditional_reenqueue_does_not_claim_a_row_changed_after_listing(
    monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer, change: str
) -> None:
    """Concurrent completion, deletion or reassignment is a no-op rather than a recovery."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL, version="v2")])
    list_pending = control.list_pending

    async def list_then_change() -> tuple[PendingWorkflow, ...]:
        """Return the snapshot and change the live row before lease acquisition."""
        pending = await list_pending()
        if change == "owner":
            control.rows[ORIGINAL].executor_id = "replacement"
        elif change == "status":
            control.rows[ORIGINAL].status = "SUCCESS"
        else:
            del control.rows[ORIGINAL]
        return pending

    monkeypatch.setattr(control, "list_pending", list_then_change)
    assert await _sweep(control, locks=locks) == SweepOutcome()
    assert control.writes() == [("reenqueue", ORIGINAL, "retired")]
    if change == "owner":
        assert control.rows[ORIGINAL].executor_id == "replacement"
    elif change == "status":
        assert control.rows[ORIGINAL].status == "SUCCESS"
    else:
        assert ORIGINAL not in control.rows
    assert locks.holders == {}
    assert all(session.closed for session in locks.sessions)


async def test_a_latest_version_read_failure_under_one_lock_allows_other_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unknown liveness fails every row of that source while another source still recovers."""
    control = FakeRecoveryControl(
        [
            workflow_status("a", version="v0"),
            workflow_status("b", version="v0"),
            workflow_status("c", version="v1"),
        ]
    )
    monkeypatch.setattr(
        control,
        "latest_version",
        AsyncMock(side_effect=["v2", OSError("latest unavailable"), "v2"]),
    )
    holds = FakeVersionHolds()
    outcome = await _sweep(
        control, holds=holds, registry=FakeRegistrations(registered("v0", "v1", "v2"))
    )

    assert [workflow_id for workflow_id, _ in outcome.failed] == ["a", "b"]
    assert all("latest unavailable" in reason for _, reason in outcome.failed)
    assert outcome.forked == (("c", recovery_successor_id("c")),)
    assert control.rows["a"].status == control.rows["b"].status == "PENDING"
    assert holds.events == ["hold:v0", "release:v0", "hold:v1", "release:v1"]
    assert holds.held == set()
