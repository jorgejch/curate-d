"""What reconciliation does about one desired placement, and when it runs."""

from uuid import uuid4

import pytest

from curate_d.config import Settings
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
from curate_d.reconciliation.convergence import (
    Action,
    Target,
    decide,
    paired,
    retention_changed,
)
from curate_d.reconciliation.retention import decide_retention
from curate_d.reconciliation.store import RecordedRetention
from curate_d.runtime.profiles import WorkerProfile
from curate_d.workflows import reconcile
from curate_d.workflows.registry import schedules_for

_CONTENT = ContentObjectId(uuid4())
_LOCATOR = "/lib/Photos/2024/a.jpg"
_LIB = DestinationConfig(name="lib", provider=Provider.PCLOUD, account="1", root_path="/lib")


def _placement(state: PlacementState, *, locator: str = _LOCATOR) -> Placement:
    """A placement of the content made under `lib` at account 1, in `state` at `locator`."""
    return Placement.build(
        content_object_id=_CONTENT,
        destination_id=derive_destination_id("pcloud", "1", "/lib"),
        destination_name="lib",
        locator=locator,
        template_version="seed-1",
    ).model_copy(update={"state": state})


def _target(
    state: PlacementState | None,
    *,
    locator: str = _LOCATOR,
    destination: DestinationConfig = _LIB,
) -> Target:
    """A target at `_LOCATOR` of `destination`, paired as evaluation pairs it."""
    placements = [] if state is None else [_placement(state, locator=locator)]
    return Target(
        content_object_id=_CONTENT,
        fingerprint=StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64),
        size_bytes=10,
        destination=destination,
        locator=_LOCATOR,
        template_version="seed-1",
        existing=paired(destination, placements),
    )


def test_a_missing_placement_is_placed() -> None:
    """Nothing recorded yet."""
    assert decide(_target(None), running=False) is Action.PLACE


def test_a_present_copy_at_the_rendered_locator_satisfies() -> None:
    """No work."""
    assert decide(_target(PlacementState.PRESENT), running=False) is Action.SATISFIED


def test_a_present_copy_elsewhere_is_placed_again_at_the_new_locator() -> None:
    """A re-render moved it."""
    moved = _target(PlacementState.PRESENT, locator="/lib/Other/a.jpg")
    assert decide(moved, running=False) is Action.PLACE


@pytest.mark.parametrize(
    "state", [PlacementState.PENDING, PlacementState.COPYING, PlacementState.VERIFYING]
)
def test_an_attempt_in_flight_is_left_alone_while_it_runs(state: PlacementState) -> None:
    """And recorded failed once its workflows have ended without concluding."""
    assert decide(_target(state), running=True) is Action.IN_FLIGHT
    assert decide(_target(state), running=False) is Action.STALE


@pytest.mark.parametrize(
    "state", [PlacementState.FAILED, PlacementState.UNTRUSTED, PlacementState.ABSENT]
)
def test_a_failed_untrusted_or_lost_copy_is_placed_again(state: PlacementState) -> None:
    """A new attempt."""
    assert decide(_target(state), running=False) is Action.PLACE


def test_a_name_pointed_at_another_account_is_placed_again() -> None:
    """The present copy at the old account does not pair with the new one: new work."""
    moved = _LIB.model_copy(update={"account": "2"})
    target = _target(PlacementState.PRESENT, destination=moved)
    assert target.existing is None
    assert decide(target, running=False) is Action.PLACE


def test_a_renamed_destination_keeps_its_placement() -> None:
    """Same provider, account and root under a new name: the present copy satisfies it."""
    renamed = DestinationConfig(
        name="library", provider=Provider.PCLOUD, account="1", root_path="/lib/"
    )
    target = _target(PlacementState.PRESENT, destination=renamed)
    assert target.existing is not None
    assert decide(target, running=False) is Action.SATISFIED


def test_a_new_policy_version_records_an_unchanged_retention_decision_again() -> None:
    """Same outcome and reason under a new version is recorded; under the same one it is not."""
    decision = decide_retention(None, [], destinations={_LIB.id: "lib"})
    last = RecordedRetention.from_details(decision.audit_details(policy_version=3))

    assert retention_changed(last, decision, policy_version=4)
    assert not retention_changed(last, decision, policy_version=3)


def test_an_entry_without_a_policy_version_matches_only_an_unknown_one() -> None:
    """An entry from a build that did not record the version is recorded once more."""
    decision = decide_retention(None, [], destinations={_LIB.id: "lib"})
    details = {k: v for k, v in decision.audit_details().items() if k != "policy_version"}
    last = RecordedRetention.from_details(details)

    assert not retention_changed(last, decision, policy_version=None)
    assert retention_changed(last, decision, policy_version=3)
    assert retention_changed(None, decision, policy_version=3)


def test_the_sweep_and_the_checks_run_on_their_configured_schedules() -> None:
    """On the I/O profile's maintenance queue, with the configured cron."""
    settings = Settings(reconcile_schedule="*/5 * * * *", destination_check_schedule="0 2 * * *")
    sweep, check = reconcile.schedule(settings), reconcile.check_schedule(settings)
    assert (sweep["schedule"], sweep["queue_name"]) == ("*/5 * * * *", "maintenance")
    assert (check["schedule"], check["queue_name"]) == ("0 2 * * *", "maintenance")
    applied = {s["schedule_name"] for s in schedules_for(WorkerProfile.IO, settings)}
    assert {sweep["schedule_name"], check["schedule_name"]} <= applied
