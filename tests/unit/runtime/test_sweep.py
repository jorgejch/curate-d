"""The recovery sweep's decision rules.

Every rule here answers the same question — will any live worker ever claim this row —
and getting it wrong is expensive in both directions. Too eager re-dispatches work that
is running, losing exactly-once to the sweep itself. Too cautious leaves orphans pending
forever, which is the gap the sweep exists to close.

The planner decides by version and lineage alone; whether an owner is alive is decided
by its executor lease when the sweep runs (test_sweep_execution.py).
"""

import pytest

from curate_d.runtime.sweep import (
    PendingWorkflow,
    RecoveryAction,
    SweepOutcome,
    plan_recovery,
    recovery_successor_id,
)

CURRENT = "0.2.0"
RETIRED = "0.1.0"


def _pending(
    workflow_id: str = "placement:sha256:abc:archive",
    executor_id: str | None = "worker-0",
    application_version: str | None = CURRENT,
) -> PendingWorkflow:
    return PendingWorkflow(
        workflow_id=workflow_id,
        executor_id=executor_id,
        application_version=application_version,
    )


def _plan(*pending: PendingWorkflow) -> tuple[tuple[str, RecoveryAction], ...]:
    return tuple(
        (entry.workflow.workflow_id, entry.action)
        for entry in plan_recovery(pending, current_version=CURRENT)
    )


def test_pending_work_on_the_current_version_is_a_reenqueue_candidate() -> None:
    """Nothing sweeps for identities that no longer boot; the lease decides which did."""
    assert _plan(_pending(workflow_id="orphan", executor_id="worker-9")) == (
        ("orphan", RecoveryAction.REENQUEUE),
    )


def test_a_superseded_version_is_forked_rather_than_reenqueued() -> None:
    """Dequeue is version-filtered: re-enqueueing leaves it ENQUEUED and unclaimable."""
    assert _plan(
        _pending(workflow_id="parked", executor_id="worker-9", application_version=RETIRED)
    ) == (("parked", RecoveryAction.FORK),)


def test_a_live_identity_on_a_superseded_version_is_still_forked() -> None:
    """The pod restarted onto a new version and will not reclaim its own predecessor's work.

    Its identity is live, so an identity-only check would call this healthy and skip it.
    """
    assert _plan(
        _pending(workflow_id="parked", executor_id="worker-0", application_version=RETIRED)
    ) == (("parked", RecoveryAction.FORK),)


def test_a_versionless_pending_workflow_is_a_reenqueue_candidate() -> None:
    """A version-less row is dequeueable by the latest version; only identity strands it."""
    assert _plan(
        _pending(workflow_id="orphan", executor_id="worker-9", application_version=None)
    ) == (("orphan", RecoveryAction.REENQUEUE),)


def test_a_workflow_with_no_executor_recorded_is_treated_as_orphaned() -> None:
    """No owner means no process will ever scan for it on startup."""
    assert _plan(_pending(workflow_id="ownerless", executor_id=None)) == (
        ("ownerless", RecoveryAction.REENQUEUE),
    )


def test_a_mixed_population_is_partitioned_by_the_remedy_each_row_needs() -> None:
    """The realistic case after a scale-down mid-deployment."""
    assert _plan(
        _pending(workflow_id="running", executor_id="worker-0"),
        _pending(workflow_id="orphan", executor_id="worker-9"),
        _pending(workflow_id="parked", executor_id="worker-0", application_version=RETIRED),
    ) == (
        ("running", RecoveryAction.REENQUEUE),
        ("orphan", RecoveryAction.REENQUEUE),
        ("parked", RecoveryAction.FORK),
    )


def test_an_empty_population_plans_nothing() -> None:
    assert _plan() == ()


def test_the_plan_explains_why_each_workflow_was_selected() -> None:
    """The reason is what an operator reads in the log; a bare action is not diagnosable."""
    plan = plan_recovery(
        [
            _pending(workflow_id="orphan", executor_id="worker-9"),
            _pending(workflow_id="parked", application_version=RETIRED),
        ],
        current_version=CURRENT,
    )
    reasons = {entry.workflow.workflow_id: entry.reason for entry in plan}
    assert "worker-9" in reasons["orphan"]
    assert "lease" in reasons["orphan"]
    assert RETIRED in reasons["parked"]
    assert "undequeueable" in reasons["parked"]


def test_the_outcome_counts_both_remedies_as_recovered() -> None:
    outcome = SweepOutcome(reenqueued=("a", "b"), forked=(("c", "c-fork"),))
    assert outcome.recovered_count == 3


def test_an_empty_outcome_reports_nothing_recovered() -> None:
    assert SweepOutcome().recovered_count == 0


@pytest.mark.parametrize("status", ["ENQUEUED", "DELAYED"])
@pytest.mark.parametrize("version", [CURRENT, None])
def test_healthy_queued_work_is_not_an_executor_orphan(status: str, version: str | None) -> None:
    assert _plan(PendingWorkflow("queued", None, version, status=status)) == ()


def test_old_version_enqueued_work_is_recovered_without_an_executor() -> None:
    workflow = PendingWorkflow("queued", None, RETIRED, status="ENQUEUED")
    assert _plan(workflow) == (("queued", RecoveryAction.FORK),)


def test_old_version_delayed_work_waits_for_the_scheduler() -> None:
    workflow = PendingWorkflow("delayed", None, RETIRED, status="DELAYED")
    assert _plan(workflow) == ()


@pytest.mark.parametrize("version", [CURRENT, RETIRED, None])
def test_a_forked_ancestor_is_retired_even_after_rollback(version: str | None) -> None:
    workflow = PendingWorkflow("ancestor", "worker-9", version, was_forked_from=True)
    assert _plan(workflow) == (("ancestor", RecoveryAction.RETIRE),)


def test_each_lineage_hop_has_a_distinct_stable_recovery_id() -> None:
    successor = recovery_successor_id("source")
    assert successor == recovery_successor_id("source")
    assert successor != recovery_successor_id("other")
    assert successor != recovery_successor_id(successor)
