"""Observability: states folded into buckets, the spend cap, and failure detail."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from dbos import WorkflowStatus

from curate_d.api.jobs import (
    CLASSIFICATION_BUCKETS,
    PLACEMENT_BUCKETS,
    SCAN_BUCKETS,
    SOURCE_BUCKETS,
    WORKFLOW_BUCKETS,
    Bucket,
    FailedUnit,
    Spend,
    Stage,
    classification_states,
    progress,
    spend_report,
    workflow_failure,
)
from curate_d.classification.store import SpendStatus
from curate_d.domain import PlacementState, ScanStatus, SourceObjectState
from tests.unit.api.fakes import FakeJobs, backends, client, idle_jobs


def test_states_fold_into_the_four_buckets() -> None:
    """Copying and verifying are in progress; untrusted and absent copies are failures."""
    folded = progress(
        {
            "pending": 1,
            "copying": 2,
            "verifying": 3,
            "present": 4,
            "failed": 5,
            "untrusted": 6,
            "absent": 7,
        },
        PLACEMENT_BUCKETS,
    )
    assert (folded.pending, folded.in_progress, folded.completed, folded.failed) == (1, 5, 4, 18)
    assert folded.by_state["untrusted"] == 6


def test_a_state_the_mapping_does_not_know_counts_as_pending() -> None:
    """New states are never silently counted as done."""
    folded = progress({"SOMETHING_NEW": 2}, WORKFLOW_BUCKETS)
    assert folded.pending == 2


def test_states_with_nothing_in_them_are_left_out() -> None:
    """Zero counts add noise, not information."""
    assert progress({"SUCCESS": 0, "ERROR": 1}, WORKFLOW_BUCKETS).by_state == {"ERROR": 1}


@pytest.mark.parametrize(
    ("mapping", "states"),
    [
        (SCAN_BUCKETS, list(ScanStatus)),
        (SOURCE_BUCKETS, list(SourceObjectState)),
        (PLACEMENT_BUCKETS, list(PlacementState)),
    ],
)
def test_every_recorded_state_has_a_bucket(mapping: dict[str, Bucket], states: list[str]) -> None:
    """A state added to the domain must be placed deliberately."""
    assert set(states) == set(mapping)


def test_awaiting_review_is_failed_classification() -> None:
    """A person has to decide, so it is not pending."""
    assert CLASSIFICATION_BUCKETS["awaiting_review"] is Bucket.FAILED


def test_counts_and_a_reached_spend_cap_are_reported() -> None:
    """The cap breach is visible with when it happened."""
    reached = datetime(2026, 9, 20, 8, tzinfo=UTC)
    summary = idle_jobs().model_copy(
        update={
            "placements": progress({"present": 3, "failed": 1}, PLACEMENT_BUCKETS),
            "spend": Spend(
                period="2026-09",
                spent_usd=Decimal("10.00"),
                cap_usd=Decimal("10.00"),
                calls=500,
                cap_reached=True,
                cap_reached_at=reached,
            ),
        }
    )
    with client(replace(backends(), jobs=FakeJobs(summary=summary))) as http:
        reported = http.get("/jobs").json()
    assert (reported["placements"]["completed"], reported["placements"]["failed"]) == (3, 1)
    assert reported["spend"]["cap_reached"] is True
    assert reported["spend"]["cap_reached_at"] == "2026-09-20T08:00:00Z"


def test_a_failed_placement_reports_what_failed_and_why() -> None:
    """The content object, the placement and the reason."""
    content, placement = uuid4(), uuid4()
    fake = FakeJobs(
        failed=[
            FailedUnit(
                stage=Stage.PLACEMENT,
                reason="untrusted: digests differ",
                where="library:/Photos/x.jpg",
                content_object_id=content,
                placement_id=placement,
            )
        ]
    )
    with client(replace(backends(), jobs=fake)) as http:
        [failure] = http.get("/jobs/failures", params={"limit": 10}).json()
    assert failure["stage"] == "placement"
    assert failure["reason"] == "untrusted: digests differ"
    assert (failure["content_object_id"], failure["placement_id"]) == (str(content), str(placement))
    assert fake.limits == [10]


@pytest.mark.parametrize("limit", [0, 1001])
def test_an_out_of_range_failure_limit_is_refused(limit: int) -> None:
    """Between one and a thousand per stage."""
    with client() as http:
        assert http.get("/jobs/failures", params={"limit": limit}).status_code == 422


def test_content_not_yet_settled_is_unclassified() -> None:
    """What is neither classified, in review nor held is waiting."""
    assert classification_states(total=10, classified=5, reviewing=2, held=1) == {
        "classified": 5,
        "awaiting_review": 2,
        "held": 1,
        "unclassified": 2,
    }


def test_nothing_spent_yet_reports_the_configured_cap() -> None:
    """A month with no calls has no row; the cap in force is reported."""
    report = spend_report("2026-10", None, cap=Decimal("25"))
    assert (report.spent_usd, report.cap_usd, report.calls, report.cap_reached) == (
        Decimal(0),
        Decimal("25"),
        0,
        False,
    )


def test_a_month_that_reached_its_cap_says_when() -> None:
    """The breach and its moment come from the recorded status."""
    reached = datetime(2026, 9, 20, tzinfo=UTC)
    status = SpendStatus(
        period="2026-09",
        spent_usd=Decimal("9.99"),
        cap_usd=Decimal("10"),
        calls=42,
        cap_reached_at=reached,
    )
    report = spend_report("2026-09", status, cap=Decimal("99"))
    assert (report.cap_usd, report.calls, report.cap_reached, report.cap_reached_at) == (
        Decimal("10"),
        42,
        True,
        reached,
    )


def _status(**fields: object) -> WorkflowStatus:
    """A workflow status with `fields` set."""
    status = WorkflowStatus()
    for name, value in {
        "workflow_id": "w-1",
        "status": "ERROR",
        "name": "place_content",
        "queue_name": "transfer-small",
        "error": None,
        "updated_at": None,
        **fields,
    }.items():
        setattr(status, name, value)
    return status


def test_a_failed_workflow_carries_its_error_and_queue() -> None:
    """The decoded exception, the queue and the workflow's name."""
    failure = workflow_failure(_status(error=ValueError("disk full"), updated_at=1_790_000_000_000))
    assert failure.reason == "ERROR: disk full"
    assert failure.where == "transfer-small: place_content"
    assert failure.workflow_id == "w-1"
    assert failure.failed_at == datetime.fromtimestamp(1_790_000_000, tz=UTC)


def test_a_cancelled_unqueued_workflow_reports_its_state() -> None:
    """Without an error, the terminal state is the reason."""
    failure = workflow_failure(_status(status="CANCELLED", queue_name=None))
    assert (failure.reason, failure.where, failure.failed_at) == (
        "CANCELLED",
        "(unqueued): place_content",
        None,
    )
