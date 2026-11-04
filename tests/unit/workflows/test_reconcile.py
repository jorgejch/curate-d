"""What a reconciliation batch plans for its targets, without DBOS or the ledger."""

from uuid import uuid4

from curate_d.domain import (
    ContentObjectId,
    DestinationConfig,
    HashAlgorithm,
    Placement,
    PlacementState,
    Provider,
    StrongFingerprint,
    derive_destination_id,
)
from curate_d.reconciliation.convergence import Target, paired
from curate_d.workflows import reconcile

_CONTENT = ContentObjectId(uuid4())
_LOCATOR = "/lib/Photos/2024/a.jpg"
_LIB = DestinationConfig(name="lib", provider=Provider.PCLOUD, account="1", root_path="/lib")


def _placement(state: PlacementState, *, locator: str = _LOCATOR, attempts: int = 1) -> Placement:
    """Build a placement of the content under `lib`, in `state` at `locator`."""
    return Placement.build(
        content_object_id=_CONTENT,
        destination_id=derive_destination_id("pcloud", "1", "/lib"),
        destination_name="lib",
        locator=locator,
        template_version="seed-1",
    ).model_copy(update={"state": state, "attempts": attempts})


def _target(existing: Placement | None) -> Target:
    """Build a target at `_LOCATOR` of `lib`, paired with `existing` as evaluation pairs it."""
    return Target(
        content_object_id=_CONTENT,
        fingerprint=StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64),
        size_bytes=10,
        destination=_LIB,
        locator=_LOCATOR,
        template_version="seed-1",
        existing=paired(_LIB, [] if existing is None else [existing]),
    )


def test_an_unplaced_target_is_placed_as_its_first_attempt() -> None:
    """No placement recorded: one request at attempt 0, under the policy version."""
    plan = reconcile._plan_targets([_target(None)], frozenset(), 7)

    assert plan == reconcile.TargetPlan(
        place=(reconcile._request_for(_target(None), 0, 7),), stale=(), satisfied=0, in_flight=0
    )


def test_a_stale_attempt_is_failed_and_placed_again() -> None:
    """An attempt whose workflows no longer run is recorded stale and retried once more."""
    existing = _placement(PlacementState.COPYING, attempts=2)
    target = _target(existing)

    plan = reconcile._plan_targets([target], frozenset(), None)

    assert plan.stale == (existing.id,)
    assert plan.place == (reconcile._request_for(target, 3, None),)
    assert (plan.satisfied, plan.in_flight) == (0, 0)


def test_a_running_attempt_is_counted_in_flight_and_left_alone() -> None:
    """An attempt DBOS still runs, under either of its workflow IDs, plans nothing."""
    target = _target(_placement(PlacementState.VERIFYING, attempts=2))
    copy_id, verify_id = reconcile._attempt_ids(target)

    by_copy = reconcile._plan_targets([target], {copy_id}, None)
    by_verify = reconcile._plan_targets([target], {verify_id}, None)

    assert by_copy == by_verify == reconcile.TargetPlan(in_flight=1)


def test_a_present_copy_elsewhere_moves_as_a_new_attempt() -> None:
    """A present copy at another locator is placed again at attempt + 1, not re-registered."""
    existing = _placement(PlacementState.PRESENT, locator="/lib/Other/a.jpg", attempts=1)
    target = _target(existing)

    plan = reconcile._plan_targets([target], frozenset(), 3)

    assert plan.stale == ()
    assert plan.place == (reconcile._request_for(target, 2, 3),)


def test_a_failed_copy_is_placed_again_under_the_same_attempt() -> None:
    """A failed attempt is retried at its own number: only a moving copy opens a new one."""
    target = _target(_placement(PlacementState.FAILED, attempts=4))

    plan = reconcile._plan_targets([target], frozenset(), None)

    assert plan.place == (reconcile._request_for(target, 4, None),)


def test_a_satisfied_target_is_tallied_and_plans_nothing() -> None:
    """A present copy at the rendered locator needs no work."""
    plan = reconcile._plan_targets([_target(_placement(PlacementState.PRESENT))], frozenset(), 1)

    assert plan == reconcile.TargetPlan(satisfied=1)


def test_a_batch_tallies_every_target_in_order() -> None:
    """Placements and stale attempts keep the targets' order; the counts cover the rest."""
    stale = _placement(PlacementState.PENDING, attempts=1)
    targets = [
        _target(_placement(PlacementState.PRESENT)),
        _target(None),
        _target(stale),
        _target(_placement(PlacementState.VERIFYING, attempts=5)),
    ]
    running = {reconcile._attempt_ids(targets[3])[1]}

    plan = reconcile._plan_targets(targets, running, 2)

    assert plan.stale == (stale.id,)
    assert plan.place == (
        reconcile._request_for(targets[1], 0, 2),
        reconcile._request_for(targets[2], 2, 2),
    )
    assert (plan.satisfied, plan.in_flight) == (1, 1)
