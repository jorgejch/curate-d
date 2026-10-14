"""The gated sweep's failure boundaries, on in-memory DBOS rows, leases and version locks."""

from collections.abc import Iterator
from contextlib import contextmanager
from sqlite3 import IntegrityError as SQLiteIntegrityError
from unittest.mock import AsyncMock

import pytest
from dbos import DBOS
from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError
from structlog.testing import capture_logs

from curate_d.config import Settings
from curate_d.runtime import sweep
from curate_d.runtime.dbos_app import APPLICATION_NAME
from curate_d.runtime.lease import lease_key
from curate_d.runtime.sweep import (
    MAX_SWEEP_REDISPATCHES,
    RECOVERY_SCHEDULE_NAME,
    REDISPATCH_COUNT_ATTRIBUTE,
    REDISPATCH_VERSION_ATTRIBUTE,
    ClientRecoveryControl,
    DBOSRecoveryControl,
    PendingWorkflow,
    SweepOutcome,
    recovery_successor_id,
    redispatch_count,
    run_recovery_sweep,
)
from curate_d.runtime.version_lease import LockMode
from tests.unit.runtime.fake_lease import FakeLockServer
from tests.unit.runtime.fake_recovery import (
    FakePromotionFence,
    FakeRecoveryControl,
    FakeRegistrations,
    FakeVersionHolds,
    Status,
    registered,
    workflow_status,
)

ORIGINAL = "original"
SUCCESSOR = recovery_successor_id(ORIGINAL)
WORKFLOW = PendingWorkflow(ORIGINAL, "retired", "v1")
UNROUTED_QUEUES = [None, "_dbos_internal_queue", "retired-queue"]
"""No queue, DBOS's internal queue, and a queue no curate.d worker dequeues."""


async def _kept() -> str | None:
    """Report every lock still held."""
    return None


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
    fence: FakePromotionFence | None = None,
) -> SweepOutcome:
    """Run the sweep as a sweeper on `own`, over in-memory collaborators."""
    return await run_recovery_sweep(
        Settings(),
        control=control,
        own_version=own,
        lease_factory=(locks or FakeLockServer()).lease,
        version_holds=holds or FakeVersionHolds(),
        registrations=registry or FakeRegistrations(registered("v1", "v2")),
        promotion_fence=fence or FakePromotionFence(),
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


async def test_the_dbos_adapter_resumes_onto_the_named_queue_and_replaces_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resume always names the row's queue; the attribute write goes through DBOS's update."""
    resume = AsyncMock()
    update = AsyncMock()
    monkeypatch.setattr(DBOS, "resume_workflow_async", resume)
    monkeypatch.setattr(DBOS, "update_workflow_attributes_async", update)
    control = DBOSRecoveryControl()
    await control.set_attributes(ORIGINAL, {"k": 1})
    await control.reenqueue(ORIGINAL, queue_name="stage")
    update.assert_awaited_once_with(ORIGINAL, {"k": 1})
    resume.assert_awaited_once_with(ORIGINAL, queue_name="stage")


class _Client:
    """The `DBOSClient` calls the client adapter makes, recorded."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.known = {ORIGINAL}
        self.forks: list[tuple[str | None, str, int, dict[str, object]]] = []
        self.assigned: str | None = None
        """The ID `SetWorkflowID` assigns while its block runs."""

    async def list_workflows_async(self, **filters: object) -> list[object]:
        self.calls.append(f"list:{sorted(filters)}")
        ids = filters.get("workflow_ids")
        if not isinstance(ids, list):
            return []
        return [workflow_status(workflow_id) for workflow_id in ids if workflow_id in self.known]

    async def list_workflow_steps_async(
        self, workflow_id: str, *, load_output: bool
    ) -> list[dict[str, int]]:
        self.calls.append(f"steps:{workflow_id}:load_output={load_output}")
        return [{"function_id": 4}, {"function_id": 1}]

    async def fork_workflow_async(
        self, workflow_id: str, start_step: int, **routing: object
    ) -> None:
        self.calls.append(f"fork:{self.assigned}")
        self.forks.append((self.assigned, workflow_id, start_step, routing))

    async def cancel_workflow_async(self, workflow_id: str) -> None:
        self.calls.append("cancel")

    async def update_workflow_attributes_async(
        self, workflow_id: str, attributes: dict[str, object]
    ) -> None:
        self.calls.append("set_attributes")

    async def resume_workflow_async(self, workflow_id: str, *, queue_name: str) -> None:
        self.calls.append(f"reenqueue:{queue_name}")

    async def get_latest_application_version_async(self) -> dict[str, str]:
        return {"version_name": "v2"}


def _assign_ids(monkeypatch: pytest.MonkeyPatch, client: _Client) -> None:
    """Make `SetWorkflowID` record its ID on `client` for the duration of its block."""

    @contextmanager
    def assign(workflow_id: str) -> Iterator[None]:
        """Expose `workflow_id` as the ID assigned to the next workflow."""
        client.assigned = workflow_id
        try:
            yield
        finally:
            client.assigned = None

    monkeypatch.setattr(sweep, "SetWorkflowID", assign)


@pytest.mark.parametrize("adapter", ["launched", "client"])
async def test_the_adapters_fork_under_the_successor_id_with_the_original_routing(
    monkeypatch: pytest.MonkeyPatch, adapter: str
) -> None:
    """SetWorkflowID makes the successor deterministic; version, queue and partition pass on."""
    client = _Client()
    _assign_ids(monkeypatch, client)
    control: DBOSRecoveryControl | ClientRecoveryControl
    if adapter == "launched":
        monkeypatch.setattr(DBOS, "fork_workflow_async", client.fork_workflow_async)
        control = DBOSRecoveryControl()
    else:
        control = ClientRecoveryControl(client, before_write=lambda: None)  # type: ignore[arg-type]
    await control.fork(
        ORIGINAL,
        SUCCESSOR,
        5,
        application_version="v2",
        queue_name="transfer-large",
        queue_partition_key="provider-1",
    )
    assert client.forks == [
        (
            SUCCESSOR,
            ORIGINAL,
            5,
            {
                "application_version": "v2",
                "queue_name": "transfer-large",
                "queue_partition_key": "provider-1",
            },
        )
    ]


async def test_the_adapters_read_step_ids_without_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Checkpoint discovery reads function IDs only, never the step outputs."""
    client = _Client()
    monkeypatch.setattr(DBOS, "list_workflow_steps_async", client.list_workflow_steps_async)
    assert await DBOSRecoveryControl().step_ids(ORIGINAL) == [4, 1]
    control = ClientRecoveryControl(client, before_write=lambda: None)  # type: ignore[arg-type]
    assert await control.step_ids(ORIGINAL) == [4, 1]
    assert client.calls == [f"steps:{ORIGINAL}:load_output=False"] * 2


async def test_the_client_adapter_returns_none_for_a_missing_workflow() -> None:
    """An empty listing is an absent workflow, not an index error."""
    client = _Client()
    client.known = set()
    control = ClientRecoveryControl(client, before_write=lambda: None)  # type: ignore[arg-type]
    assert await control.status(ORIGINAL) is None


async def test_the_client_adapter_checks_the_lease_before_every_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lost standalone version lease stops the next fork, cancel, attribute write or resume."""
    client = _Client()
    _assign_ids(monkeypatch, client)
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
    await control.set_attributes(ORIGINAL, {})
    await control.reenqueue(ORIGINAL, queue_name="stage")
    assert await control.latest_version() == "v2"
    status = await control.status(ORIGINAL)
    assert status is not None and status.workflow_id == ORIGINAL
    assert checks == ["checked"] * 4
    assert client.calls[:4] == [f"fork:{SUCCESSOR}", "cancel", "set_attributes", "reenqueue:stage"]


@pytest.mark.parametrize("operation", ["fork", "cancel", "set_attributes", "reenqueue"])
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
        elif operation == "set_attributes":
            await control.set_attributes(ORIGINAL, {})
        else:
            await control.reenqueue(ORIGINAL, queue_name="stage")
    assert client.calls == []


# One successor per original.


async def test_unrelated_id_collision_never_cancels_source() -> None:
    """A successor ID held by another workflow is a failure, not a repair."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL), workflow_status(SUCCESSOR)])
    with pytest.raises(ValueError, match="unrelated workflow"):
        await sweep._recover_version(control, WORKFLOW, "v2", lost=_kept)
    assert control.writes() == []


async def test_a_committed_successor_is_reused_and_the_source_retired() -> None:
    """A crash between fork and cancel is repaired without a second lineage."""
    control = FakeRecoveryControl(
        [
            workflow_status(ORIGINAL, was_forked_from=True),
            workflow_status(SUCCESSOR, status="ENQUEUED", version="v2", forked_from=ORIGINAL),
        ]
    )
    assert await sweep._recover_version(control, WORKFLOW, "v3", lost=_kept) == (SUCCESSOR, False)
    assert control.writes() == [("cancel", ORIGINAL)]


async def test_an_existing_manual_fork_requires_lineage_review() -> None:
    """A hand fork's successor is not the recovery successor; leave the original alone."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL, was_forked_from=True)])
    with pytest.raises(ValueError, match="inspect its existing lineage"):
        await sweep._recover_version(control, WORKFLOW, "v2", lost=_kept)
    assert control.writes() == []


async def test_a_cancelled_snapshot_is_not_forked_back_to_life() -> None:
    """A row finished after planning stays finished."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL, status="CANCELLED")])
    with pytest.raises(ValueError, match="no longer eligible"):
        await sweep._recover_version(control, WORKFLOW, "v2", lost=_kept)
    assert control.writes() == []


async def test_only_a_workflow_primary_key_conflict_can_be_repaired() -> None:
    """Any other integrity error stays a failure, and the original is not cancelled."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL)])
    control.errors[("fork", ORIGINAL)] = IntegrityError(
        "insert", {}, SQLiteIntegrityError("some other constraint failed")
    )
    with pytest.raises(IntegrityError):
        await sweep._recover_version(control, WORKFLOW, "v2", lost=_kept)
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
        await sweep._recover_version(control, WORKFLOW, "v2", lost=_kept)
    assert ("cancel", ORIGINAL) not in control.calls


async def test_fork_resumes_after_last_checkpoint_with_original_routing() -> None:
    """Committed steps are kept, and the successor keeps its queue and partition."""
    control = FakeRecoveryControl(
        [workflow_status(ORIGINAL, queue_name="transfer-large", queue_partition_key="provider-1")],
        steps={ORIGINAL: [1, 4]},
    )
    assert await sweep._recover_version(control, WORKFLOW, "v2", lost=_kept) == (SUCCESSOR, True)
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
    assert [call[0] for call in control.writes()] == ["set_attributes", "reenqueue"]
    assert registry.reads == 0


async def test_a_target_change_after_the_lock_stops_forking() -> None:
    """The sweep re-reads latest under the lock and stops if it moved."""
    control = FakeRecoveryControl(
        [workflow_status("a", version="v1"), workflow_status("b", version="v0")],
        latest=["v2", "v2", "v3"],
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


@pytest.mark.parametrize("queue", UNROUTED_QUEUES)
async def test_a_fork_onto_no_curate_d_queue_is_refused(queue: str | None) -> None:
    """Every worker dequeues the internal queue and none another, so the row is not routed."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL, version="v1", queue_name=queue)])
    outcome = await _sweep(control)
    assert [workflow_id for workflow_id, _ in outcome.failed] == [ORIGINAL]
    assert "refused" in outcome.failed[0][1]
    assert control.writes() == []


@pytest.mark.parametrize("queue", UNROUTED_QUEUES)
async def test_a_reenqueue_onto_no_curate_d_queue_is_refused(
    queue: str | None, locks: FakeLockServer
) -> None:
    """A dead executor's row without a curate.d queue is reported and left."""
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

    async def reenqueue(self, workflow_id: str, *, queue_name: str) -> None:
        executor_id = self.rows[workflow_id].executor_id
        assert executor_id is not None
        self.held_at_reenqueue.append(
            lease_key(APPLICATION_NAME, executor_id) in self.locks.holders
        )
        await super().reenqueue(workflow_id, queue_name=queue_name)


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
    """A row without an owner has no lease that could prove it dead."""
    control = FakeRecoveryControl([workflow_status("a", executor_id=None, version="v2")])
    outcome = await _sweep(control, locks=locks)
    assert control.writes() == []
    assert [workflow_id for workflow_id, _ in outcome.failed] == ["a"]
    assert "without an executor" in outcome.failed[0][1]
    assert locks.sessions == []


async def test_equal_registration_times_do_not_establish_deploy_order() -> None:
    """A source registered at the target's own time is not older, so its rows are left."""
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
        latest=["v3", "v3", "v3", "v4"],
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
    """A failed checkpoint read fails only its row; the version's other rows still recover."""
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
@pytest.mark.parametrize("queue", UNROUTED_QUEUES)
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
    monkeypatch: pytest.MonkeyPatch, version: str, state: Status | None
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


# Lock loss around a fork.


async def test_a_fence_lost_before_the_first_fork_forks_nothing() -> None:
    """The fence's own session is asked before each fork; lost, the batch stops unwritten."""
    control = FakeRecoveryControl([workflow_status("a"), workflow_status("b")])
    fence = FakePromotionFence()
    fence.lose_after(0)
    with capture_logs() as events:
        outcome = await _sweep(control, fence=fence)
    assert outcome.forked == ()
    assert [workflow_id for workflow_id, _ in outcome.failed] == ["a"]
    assert "promotion fence lost before fork" in outcome.failed[0][1]
    assert outcome.skipped == ("b",)
    assert control.writes() == []
    [event] = [event for event in events if event["event"] == "recovery_sweep.version_lock_lost"]
    assert event["log_level"] == "error"


async def test_a_version_lock_lost_before_a_fork_forks_nothing_more() -> None:
    """Earlier rows keep their forks; the row that found the loss and the rest are left."""
    control = FakeRecoveryControl([workflow_status("a"), workflow_status("b")])
    holds = FakeVersionHolds()
    # One check before a's fork and one before its cancel; b's first check finds it lost.
    holds.lose_after(2)
    outcome = await _sweep(control, holds=holds)
    assert outcome.forked == (("a", recovery_successor_id("a")),)
    assert outcome.failed == (
        (
            "b",
            "recovering workflow 'b' from version 'v1' onto 'v2' failed: "
            "version lock lost before fork",
        ),
    )
    assert ("fork", "b", recovery_successor_id("b"), "1") not in control.writes()
    assert control.rows["b"].status == "PENDING"
    assert holds.held == set()


async def test_a_version_lock_lost_after_a_fork_still_cancels_the_original() -> None:
    """Two live lineages are worse than one unconfirmed fork; the rest of the sweep stops."""
    control = FakeRecoveryControl(
        [workflow_status("a"), workflow_status("b"), workflow_status("c", version="v0")]
    )
    holds = FakeVersionHolds()
    holds.lose_after(1)
    with capture_logs() as events:
        outcome = await _sweep(
            control, holds=holds, registry=FakeRegistrations(registered("v0", "v1", "v2"))
        )
    assert control.writes() == [("fork", "a", recovery_successor_id("a"), "1"), ("cancel", "a")]
    assert control.rows["a"].status == "CANCELLED"
    assert outcome.forked == ()
    assert [workflow_id for workflow_id, _ in outcome.failed] == ["a"]
    assert "version lock lost after fork; the original was cancelled" in outcome.failed[0][1]
    assert outcome.skipped == ("b", "c")
    assert [event["event"] for event in events if event["log_level"] == "error"] == [
        "recovery_sweep.version_lock_lost"
    ]
    assert holds.events == ["hold:v1", "release:v1"]


async def test_a_version_lock_lost_before_a_retirement_still_cancels_and_stops() -> None:
    """A confirmed successor's original is cancelled under a lost version lock; the batch stops."""
    control = FakeRecoveryControl(
        [
            workflow_status("a", was_forked_from=True),
            workflow_status(
                recovery_successor_id("a"), status="ENQUEUED", version="v2", forked_from="a"
            ),
            workflow_status("b"),
        ]
    )
    holds = FakeVersionHolds()
    holds.lose_after(0)
    with capture_logs() as events:
        outcome = await _sweep(control, holds=holds)
    assert control.writes() == [("cancel", "a")]
    assert control.rows["a"].status == "CANCELLED"
    assert outcome.retired == ()
    assert outcome.failed == (
        (
            "a",
            "recovering workflow 'a' from version 'v1' onto 'v2' failed: "
            "version lock lost before retirement; the original was cancelled",
        ),
    )
    assert outcome.skipped == ("b",)
    assert [event["event"] for event in events if event["log_level"] == "error"] == [
        "recovery_sweep.version_lock_lost"
    ]


async def test_a_fence_lost_before_an_on_target_retirement_stops_the_batch() -> None:
    """A fence lost before an on-target retirement stops the batch."""
    control = FakeRecoveryControl(
        [
            workflow_status("a", version="v2", was_forked_from=True),
            workflow_status(
                recovery_successor_id("a"), status="ENQUEUED", version="v2", forked_from="a"
            ),
            workflow_status("b"),
        ]
    )
    fence = FakePromotionFence()
    fence.lose_after(0)
    with capture_logs() as events:
        outcome = await _sweep(control, fence=fence)
    assert control.writes() == [("cancel", "a")]
    assert outcome.retired == ()
    assert outcome.failed == (
        (
            "a",
            "recovering workflow 'a' from version 'v2' onto 'v2' failed: "
            "promotion fence lost before retirement; the original was cancelled",
        ),
    )
    assert outcome.skipped == ("b",)
    assert [event["event"] for event in events if event["log_level"] == "error"] == [
        "recovery_sweep.version_lock_lost"
    ]


async def test_a_lost_lock_leaves_a_stale_tick_alone_and_stops_the_batch() -> None:
    """A stale own tick is left alone once a lock is lost; the batch stops."""
    control = FakeRecoveryControl(
        [
            workflow_status(
                "tick", version="v1", queue_name="maintenance", schedule_name=RECOVERY_SCHEDULE_NAME
            ),
            workflow_status("b"),
        ]
    )
    holds = FakeVersionHolds()
    holds.lose_after(0)
    with capture_logs() as events:
        outcome = await _sweep(control, holds=holds)
    assert control.writes() == []
    assert control.rows["tick"].status == "PENDING"
    assert outcome.retired_ticks == ()
    assert outcome.failed == (
        (
            "tick",
            "recovering workflow 'tick' from version 'v1' onto 'v2' failed: "
            "version lock lost before retiring a stale tick; it was left",
        ),
    )
    assert outcome.skipped == ("b",)
    assert [event["event"] for event in events if event["log_level"] == "error"] == [
        "recovery_sweep.version_lock_lost"
    ]


async def test_an_original_that_survives_its_cancel_fails_as_an_unverified_lineage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The successor exists, so the original must read CANCELLED; the batch goes on."""
    control = FakeRecoveryControl([workflow_status("a"), workflow_status("b")])
    cancel = control.cancel

    async def ignore_a(workflow_id: str) -> None:
        """Cancel every row but `a`, as a cancel that never landed would."""
        if workflow_id != "a":
            await cancel(workflow_id)

    monkeypatch.setattr(control, "cancel", ignore_a)
    with capture_logs() as events:
        outcome = await _sweep(control)
    assert [workflow_id for workflow_id, _ in outcome.failed] == ["a"]
    assert "reads 'PENDING' after its cancel" in outcome.failed[0][1]
    assert outcome.forked == (("b", recovery_successor_id("b")),)
    [event] = [event for event in events if event["event"] == "recovery_sweep.lineage_unverified"]
    assert event["log_level"] == "error"


# The promotion fence.


async def test_a_promotion_in_progress_skips_forks_and_retirements_but_not_reenqueues(
    locks: FakeLockServer,
) -> None:
    """A promotion holding the fence may move latest backwards, so nothing is forked."""
    control = FakeRecoveryControl(
        [
            workflow_status("fork-me", version="v1"),
            workflow_status("retire-me", version="v2", was_forked_from=True),
            workflow_status("orphan", version="v2", executor_id="worker-9"),
        ]
    )
    fence = FakePromotionFence(promoting=True)
    holds = FakeVersionHolds()
    with capture_logs() as events:
        outcome = await _sweep(control, holds=holds, locks=locks, fence=fence)
    assert outcome == SweepOutcome(reenqueued=("orphan",), skipped=("fork-me", "retire-me"))
    assert control.writes() == [("set_attributes", "orphan"), ("reenqueue", "orphan", "stage")]
    assert holds.events == []
    assert fence.attempts == 1
    [skipped] = [event for event in events if event["event"] == "recovery_sweep.skipped"]
    assert skipped["reason"] == "a promotion is in progress"
    assert skipped["log_level"] == "warning"


async def test_the_fence_is_held_across_every_fork_and_retirement() -> None:
    """Taken once before the first write and released after the last version lock."""
    control = FakeRecoveryControl(
        [
            workflow_status("retire-me", version="v2", was_forked_from=True),
            workflow_status(
                recovery_successor_id("retire-me"),
                status="ENQUEUED",
                version="v2",
                forked_from="retire-me",
            ),
            workflow_status("a", version="v0"),
            workflow_status("b", version="v1"),
        ]
    )
    holds = FakeVersionHolds()
    fence = FakePromotionFence(events=holds.events)
    fenced_writes: list[bool] = []
    fork, cancel = control.fork, control.cancel

    async def fork_fenced(*args: object, **kwargs: object) -> None:
        """Record whether the fence is held, then fork."""
        fenced_writes.append(fence.held)
        await fork(*args, **kwargs)  # type: ignore[arg-type]

    async def cancel_fenced(workflow_id: str) -> None:
        """Record whether the fence is held, then cancel."""
        fenced_writes.append(fence.held)
        await cancel(workflow_id)

    control.fork, control.cancel = fork_fenced, cancel_fenced  # type: ignore[method-assign]
    outcome = await _sweep(
        control,
        registry=FakeRegistrations(registered("v0", "v1", "v2")),
        holds=holds,
        fence=fence,
    )
    assert outcome.retired == ("retire-me",)
    assert [original for original, _ in outcome.forked] == ["a", "b"]
    assert len(fenced_writes) == 5 and all(fenced_writes)
    assert holds.events == [
        "fence:hold",
        "hold:v0",
        "release:v0",
        "hold:v1",
        "release:v1",
        "fence:release",
    ]
    assert fence.attempts == 1


async def test_a_promotion_before_the_fence_is_seen_under_it() -> None:
    """Latest is re-read once the fence is held; a rollback since the first read forks nothing."""
    control = FakeRecoveryControl(
        [
            workflow_status("a", version="v1"),
            workflow_status("retire-me", version="v2", was_forked_from=True),
        ],
        latest=["v2", "v1"],
    )
    holds = FakeVersionHolds()
    fence = FakePromotionFence()
    outcome = await _sweep(control, holds=holds, fence=fence)
    assert outcome == SweepOutcome(skipped=("retire-me", "a"))
    assert control.writes() == []
    assert holds.events == []
    assert fence.events == ["fence:hold", "fence:release"]


async def test_a_fence_error_fails_every_fork_and_retirement_but_not_reenqueues(
    locks: FakeLockServer,
) -> None:
    """Unknown promotion state forks nothing; the rows are reported, re-enqueues still run."""
    control = FakeRecoveryControl(
        [
            workflow_status("fork-me", version="v1"),
            workflow_status("orphan", version="v2", executor_id="worker-9"),
        ]
    )
    outcome = await _sweep(
        control, locks=locks, fence=FakePromotionFence(error=OSError("fence unavailable"))
    )
    assert outcome.reenqueued == ("orphan",)
    assert [workflow_id for workflow_id, _ in outcome.failed] == ["fork-me"]
    assert "holding the promotion fence failed" in outcome.failed[0][1]
    assert control.writes() == [("set_attributes", "orphan"), ("reenqueue", "orphan", "stage")]


async def test_a_latest_read_failure_under_the_fence_fails_every_candidate() -> None:
    """Without a confirmed target nothing is forked, and every candidate is reported."""
    control = FakeRecoveryControl([workflow_status("a", version="v1")])
    fence = FakePromotionFence()
    original = control.latest_version
    reads = 0

    async def fail_second_read() -> str:
        """Answer the first read, then fail."""
        nonlocal reads
        reads += 1
        if reads > 1:
            raise OSError("latest unavailable")
        return await original()

    control.latest_version = fail_second_read  # type: ignore[method-assign]
    outcome = await _sweep(control, fence=fence)
    assert [workflow_id for workflow_id, _ in outcome.failed] == ["a"]
    assert "under the promotion fence" in outcome.failed[0][1]
    assert control.writes() == []
    assert fence.events == ["fence:hold", "fence:release"]


async def test_a_sweeper_off_the_target_never_takes_the_fence() -> None:
    """Only a sweeper that may fork needs the fence."""
    control = FakeRecoveryControl([workflow_status("parked", version="v1")], latest="v3")
    fence = FakePromotionFence()
    outcome = await _sweep(control, own="v2", fence=fence)
    assert outcome.skipped == ("parked",)
    assert fence.attempts == 0


async def test_a_sweep_with_only_reenqueues_never_takes_the_fence(locks: FakeLockServer) -> None:
    """Re-enqueues do not depend on latest, so they never wait on a promotion."""
    control = FakeRecoveryControl([workflow_status("orphan", version="v2", executor_id="w-9")])
    fence = FakePromotionFence(promoting=True)
    outcome = await _sweep(control, locks=locks, fence=fence)
    assert outcome == SweepOutcome(reenqueued=("orphan",))
    assert fence.attempts == 0


@pytest.mark.parametrize("step_ids, start_step", [([], 1), ([0], 1), ([4, 1, 3], 5)])
async def test_recovery_resumes_after_the_highest_checkpoint(
    step_ids: list[int], start_step: int
) -> None:
    """An empty or unordered checkpoint listing never skips or repeats committed work."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL)], steps={ORIGINAL: step_ids})

    assert await sweep._recover_version(control, WORKFLOW, "v2", lost=_kept) == (SUCCESSOR, True)
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
        assert await sweep._recover_version(control, WORKFLOW, "v2", lost=_kept) == (
            SUCCESSOR,
            False,
        )
        assert control.writes() == [("cancel", ORIGINAL)]
    else:
        with pytest.raises(IntegrityError) as raised:
            await sweep._recover_version(control, WORKFLOW, "v2", lost=_kept)
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
        await sweep._recover_version(control, WORKFLOW, "v2", lost=_kept)
    fork.assert_awaited_once()
    assert control.writes() == []
    assert control.rows[ORIGINAL].status == "PENDING"


async def test_a_retry_repairs_failed_cancellation_without_forking_again() -> None:
    """A retry after a rollback retires the ancestor without forking it a second time."""
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


@pytest.mark.parametrize("change", ["claimed", "completed", "cancelled", "deleted"])
async def test_a_row_that_changed_after_listing_is_not_resumed(
    monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer, change: str
) -> None:
    """Resume is unconditional, so the re-read gate keeps a claimed or finished row as it is."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL, version="v2")])
    list_pending = control.list_pending

    async def list_then_change() -> tuple[PendingWorkflow, ...]:
        """Return the listing, then change the row behind it."""
        pending = await list_pending()
        row = control.rows[ORIGINAL]
        if change == "claimed":
            row.executor_id = "replacement"
        elif change == "completed":
            row.status = "SUCCESS"
        elif change == "cancelled":
            row.status = "CANCELLED"
        else:
            del control.rows[ORIGINAL]
        return pending

    monkeypatch.setattr(control, "list_pending", list_then_change)
    assert await _sweep(control, locks=locks) == SweepOutcome()
    assert control.writes() == []


async def test_a_row_dequeued_right_after_its_resume_is_left_running(
    locks: FakeLockServer,
) -> None:
    """A live worker that picked the row up first is running it, not stranding it."""

    class _Dequeuing(FakeRecoveryControl):
        """Hand each resumed row to a live worker before the read-back."""

        async def reenqueue(self, workflow_id: str, *, queue_name: str) -> None:
            await super().reenqueue(workflow_id, queue_name=queue_name)
            self.rows[workflow_id].status = "PENDING"
            self.rows[workflow_id].executor_id = "worker-1"

    control = _Dequeuing([workflow_status(ORIGINAL, version="v2")])
    outcome = await _sweep(control, locks=locks)
    assert outcome == SweepOutcome(left_running=1)
    assert ("reenqueue", ORIGINAL, "stage") in control.writes()


async def test_a_row_cancelled_after_the_counter_write_is_not_resumed(
    locks: FakeLockServer,
) -> None:
    """A row cancelled after the counter write is not resumed."""

    class _Cancelling(FakeRecoveryControl):
        """Cancel each row as an operator would, right after its count is recorded."""

        async def set_attributes(self, workflow_id: str, attributes: dict[str, object]) -> None:
            await super().set_attributes(workflow_id, attributes)
            await self.cancel(workflow_id)

    control = _Cancelling([workflow_status(ORIGINAL, version="v2")])
    with capture_logs() as events:
        outcome = await _sweep(control, locks=locks)
    assert outcome == SweepOutcome()
    assert [call for call in control.writes() if call[0] == "reenqueue"] == []
    assert control.rows[ORIGINAL].status == "CANCELLED"
    assert [event for event in events if event["log_level"] == "error"] == []


async def test_a_resume_that_does_not_take_is_a_failure(
    monkeypatch: pytest.MonkeyPatch, locks: FakeLockServer
) -> None:
    """A row still PENDING under the dead executor after the resume is reported, not counted."""
    control = FakeRecoveryControl([workflow_status(ORIGINAL, version="v2")])
    monkeypatch.setattr(control, "reenqueue", AsyncMock())
    with capture_logs() as events:
        outcome = await _sweep(control, locks=locks)
    assert outcome.reenqueued == ()
    assert [workflow_id for workflow_id, _ in outcome.failed] == [ORIGINAL]
    assert "unverified" in outcome.failed[0][1]
    [event] = [
        event for event in events if event["event"] == "recovery_sweep.redispatch_unverified"
    ]
    assert event["log_level"] == "error"
    assert locks.holders == {}


async def test_a_row_at_the_redispatch_cap_is_neither_resumed_nor_counted(
    locks: FakeLockServer,
) -> None:
    """A row that crashes its worker on every run is reported each tick, never resumed again."""
    control = FakeRecoveryControl(
        [
            workflow_status(
                ORIGINAL,
                version="v2",
                attributes={
                    REDISPATCH_COUNT_ATTRIBUTE: MAX_SWEEP_REDISPATCHES,
                    REDISPATCH_VERSION_ATTRIBUTE: "v2",
                },
            )
        ]
    )
    with capture_logs() as events:
        outcome = await _sweep(control, locks=locks)
    assert outcome.reenqueued == ()
    assert [workflow_id for workflow_id, _ in outcome.failed] == [ORIGINAL]
    assert f"the cap of {MAX_SWEEP_REDISPATCHES}" in outcome.failed[0][1]
    assert control.writes() == []
    assert control.rows[ORIGINAL].status == "PENDING"
    [event] = [event for event in events if event["event"] == "recovery_sweep.redispatch_cap"]
    assert event["log_level"] == "error"


async def test_a_redispatch_count_from_another_version_restarts_at_zero(
    locks: FakeLockServer,
) -> None:
    """A new build may have fixed the crash, so its redispatches are counted afresh."""
    control = FakeRecoveryControl(
        [
            workflow_status(
                ORIGINAL,
                version="v2",
                attributes={
                    REDISPATCH_COUNT_ATTRIBUTE: MAX_SWEEP_REDISPATCHES,
                    REDISPATCH_VERSION_ATTRIBUTE: "v1",
                },
            )
        ]
    )
    outcome = await _sweep(control, locks=locks)
    assert outcome.reenqueued == (ORIGINAL,)
    assert control.rows[ORIGINAL].attributes == {
        REDISPATCH_COUNT_ATTRIBUTE: 1,
        REDISPATCH_VERSION_ATTRIBUTE: "v2",
    }


async def test_a_versionless_row_is_capped_under_no_version(locks: FakeLockServer) -> None:
    """A hand-written row without a version is capped under no version, never rescued forever."""
    control = FakeRecoveryControl(
        [
            workflow_status(
                ORIGINAL,
                version=None,
                attributes={
                    REDISPATCH_COUNT_ATTRIBUTE: MAX_SWEEP_REDISPATCHES,
                    REDISPATCH_VERSION_ATTRIBUTE: None,
                },
            )
        ]
    )
    with capture_logs() as events:
        outcome = await _sweep(control, locks=locks)
    assert outcome.reenqueued == ()
    assert [workflow_id for workflow_id, _ in outcome.failed] == [ORIGINAL]
    assert f"the cap of {MAX_SWEEP_REDISPATCHES}" in outcome.failed[0][1]
    assert control.writes() == []
    [event] = [event for event in events if event["event"] == "recovery_sweep.redispatch_cap"]
    assert event["log_level"] == "error"


async def test_a_count_recorded_without_a_version_restarts_once_the_row_is_versioned(
    locks: FakeLockServer,
) -> None:
    """A dequeue stamps the executor's version, so a count kept under no version restarts."""
    control = FakeRecoveryControl(
        [
            workflow_status(
                ORIGINAL,
                version="v2",
                attributes={
                    REDISPATCH_COUNT_ATTRIBUTE: MAX_SWEEP_REDISPATCHES,
                    REDISPATCH_VERSION_ATTRIBUTE: None,
                },
            )
        ]
    )
    outcome = await _sweep(control, locks=locks)
    assert outcome.reenqueued == (ORIGINAL,)
    assert control.rows[ORIGINAL].attributes == {
        REDISPATCH_COUNT_ATTRIBUTE: 1,
        REDISPATCH_VERSION_ATTRIBUTE: "v2",
    }


async def test_a_redispatch_is_counted_before_the_resume_and_keeps_other_attributes(
    locks: FakeLockServer,
) -> None:
    """DBOS replaces the whole mapping, so the sweep writes the row's own attributes back."""
    control = FakeRecoveryControl(
        [
            workflow_status(
                ORIGINAL,
                version="v2",
                attributes={
                    "tenant": "acme",
                    REDISPATCH_COUNT_ATTRIBUTE: 3,
                    REDISPATCH_VERSION_ATTRIBUTE: "v2",
                },
            )
        ]
    )
    outcome = await _sweep(control, locks=locks)
    assert outcome.reenqueued == (ORIGINAL,)
    assert control.writes() == [("set_attributes", ORIGINAL), ("reenqueue", ORIGINAL, "stage")]
    assert control.rows[ORIGINAL].attributes == {
        "tenant": "acme",
        REDISPATCH_COUNT_ATTRIBUTE: 4,
        REDISPATCH_VERSION_ATTRIBUTE: "v2",
    }


@pytest.mark.parametrize(
    "attributes, count",
    [
        (None, 0),
        ({REDISPATCH_COUNT_ATTRIBUTE: 4, REDISPATCH_VERSION_ATTRIBUTE: "v2"}, 4),
        ({REDISPATCH_COUNT_ATTRIBUTE: 4}, 0),
        ({REDISPATCH_COUNT_ATTRIBUTE: "4", REDISPATCH_VERSION_ATTRIBUTE: "v2"}, 0),
        ({REDISPATCH_COUNT_ATTRIBUTE: True, REDISPATCH_VERSION_ATTRIBUTE: "v2"}, 0),
        ({REDISPATCH_COUNT_ATTRIBUTE: -1, REDISPATCH_VERSION_ATTRIBUTE: "v2"}, 0),
    ],
)
def test_the_redispatch_count_reads_only_a_count_recorded_under_the_rows_version(
    attributes: dict[str, object] | None, count: int
) -> None:
    """A missing, foreign-version or malformed count reads as zero."""
    assert redispatch_count(workflow_status(ORIGINAL, version="v2", attributes=attributes)) == count


async def test_a_latest_version_read_failure_under_one_lock_allows_other_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreadable latest version fails that source's rows; the next source still recovers."""
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
        AsyncMock(side_effect=["v2", "v2", OSError("latest unavailable"), "v2"]),
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
