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
)
from curate_d.reconciliation.convergence import Action, Target, decide
from curate_d.runtime.profiles import WorkerProfile
from curate_d.workflows import reconcile
from curate_d.workflows.registry import schedules_for

_CONTENT = ContentObjectId(uuid4())
_LOCATOR = "/lib/Photos/2024/a.jpg"


def _target(state: PlacementState | None, *, locator: str = _LOCATOR) -> Target:
    """A target at `_LOCATOR`, with a placement in `state` at `locator`, or none."""
    existing = (
        None
        if state is None
        else Placement.build(
            content_object_id=_CONTENT,
            destination_name="lib",
            locator=locator,
            template_version="seed-1",
        ).model_copy(update={"state": state})
    )
    return Target(
        content_object_id=_CONTENT,
        fingerprint=StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64),
        size_bytes=10,
        destination=DestinationConfig(
            name="lib", provider=Provider.PCLOUD, account="1", root_path="/lib"
        ),
        locator=_LOCATOR,
        template_version="seed-1",
        existing=existing,
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


def test_the_sweep_and_the_checks_run_on_their_configured_schedules() -> None:
    """On the I/O profile's maintenance queue, with the configured cron."""
    settings = Settings(reconcile_schedule="*/5 * * * *", destination_check_schedule="0 2 * * *")
    sweep, check = reconcile.schedule(settings), reconcile.check_schedule(settings)
    assert (sweep["schedule"], sweep["queue_name"]) == ("*/5 * * * *", "maintenance")
    assert (check["schedule"], check["queue_name"]) == ("0 2 * * *", "maintenance")
    applied = {s["schedule_name"] for s in schedules_for(WorkerProfile.IO, settings)}
    assert {sweep["schedule_name"], check["schedule_name"]} <= applied
