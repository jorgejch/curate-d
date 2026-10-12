"""The attempt a starting copy records, which names its verification."""

from uuid import uuid4

import pytest

from curate_d.domain import ContentObjectId, Placement, PlacementState
from curate_d.placement.pipeline import recorded_attempt


def _placement(state: PlacementState, attempts: int) -> Placement:
    """Return a placement in `state` that has recorded `attempts`."""
    built = Placement.build(
        content_object_id=ContentObjectId(uuid4()),
        destination_name="library",
        locator="/Photos/2026/IMG_0001.JPG",
        template_version="seed-1",
    )
    return built.model_copy(update={"state": state, "attempts": attempts})


def test_a_first_copy_records_the_attempt_it_was_asked_for() -> None:
    """With nothing recorded, the request's attempt stands."""
    assert recorded_attempt(None, 0) == 0


@pytest.mark.parametrize(
    "state", [PlacementState.FAILED, PlacementState.UNTRUSTED, PlacementState.ABSENT]
)
def test_a_copy_after_a_concluded_attempt_never_lowers_the_count(state: PlacementState) -> None:
    """Failing, distrusting or losing a copy already moved to the next attempt."""
    assert recorded_attempt(_placement(state, 2), 2) == 2
    assert recorded_attempt(_placement(state, 2), 0) == 2
    assert recorded_attempt(_placement(state, 2), 3) == 3


@pytest.mark.parametrize(
    "state", [PlacementState.COPYING, PlacementState.VERIFYING, PlacementState.PRESENT]
)
def test_an_old_attempt_started_over_one_with_a_verification_records_the_next(
    state: PlacementState,
) -> None:
    """A rewound attempt never shares the identity of a verification that already exists."""
    assert recorded_attempt(_placement(state, 2), 2) == 3
    assert recorded_attempt(_placement(state, 2), 0) == 3


def test_a_move_of_a_registered_copy_records_the_attempt_it_was_asked_for() -> None:
    """Reconciliation moves a registered copy as the next attempt already."""
    assert recorded_attempt(_placement(PlacementState.PRESENT, 2), 3) == 3
