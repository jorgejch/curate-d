"""The attempt a starting copy records, which names its verification."""

from uuid import uuid4

import pytest

from curate_d.domain import ContentObjectId, Placement, PlacementState, derive_destination_id
from curate_d.placement.pipeline import recorded_attempt

# The placement's recorded attempt in every case below.
_RECORDED = 2
# States whose recorded attempt already has, or is about to have, a verification.
_WITH_VERIFICATION = (PlacementState.COPYING, PlacementState.VERIFYING, PlacementState.PRESENT)
_WITHOUT_VERIFICATION = (
    PlacementState.PENDING,
    PlacementState.ABSENT,
    PlacementState.FAILED,
    PlacementState.UNTRUSTED,
)


def _placement(state: PlacementState, attempts: int) -> Placement:
    """Return a placement in `state` that has recorded `attempts`."""
    built = Placement.build(
        content_object_id=ContentObjectId(uuid4()),
        destination_id=derive_destination_id("pcloud", "dest-account", "/Library"),
        destination_name="library",
        locator="/Photos/2026/IMG_0001.JPG",
        template_version="seed-1",
    )
    return built.model_copy(update={"state": state, "attempts": attempts})


def test_the_cases_cover_every_state() -> None:
    """Every placement state falls in exactly one of the two groups below."""
    assert set(_WITH_VERIFICATION) | set(_WITHOUT_VERIFICATION) == set(PlacementState)
    assert not set(_WITH_VERIFICATION) & set(_WITHOUT_VERIFICATION)


@pytest.mark.parametrize("attempt", [0, 3])
def test_a_first_copy_records_the_attempt_it_was_asked_for(attempt: int) -> None:
    """With nothing recorded, the request's attempt stands."""
    assert recorded_attempt(None, attempt) == attempt


@pytest.mark.parametrize("state", _WITH_VERIFICATION)
@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(_RECORDED - 1, _RECORDED + 1), (_RECORDED, _RECORDED + 1), (_RECORDED + 1, _RECORDED + 1)],
    ids=["older", "equal", "newer"],
)
def test_an_attempt_over_one_with_a_verification_records_an_identity_of_its_own(
    state: PlacementState, attempt: int, expected: int
) -> None:
    """An older or equal attempt records the next one; a newer one keeps its own."""
    assert recorded_attempt(_placement(state, _RECORDED), attempt) == expected


@pytest.mark.parametrize("state", _WITHOUT_VERIFICATION)
@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(_RECORDED - 1, _RECORDED), (_RECORDED, _RECORDED), (_RECORDED + 1, _RECORDED + 1)],
    ids=["older", "equal", "newer"],
)
def test_an_attempt_over_one_without_a_verification_never_lowers_the_count(
    state: PlacementState, attempt: int, expected: int
) -> None:
    """Concluding a copy already moved to the next attempt, so the larger count stands."""
    assert recorded_attempt(_placement(state, _RECORDED), attempt) == expected
