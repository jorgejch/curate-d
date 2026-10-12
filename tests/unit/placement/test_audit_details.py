"""The evidence a placement's audit entries carry, and the identity of its verification."""

from datetime import UTC, datetime
from uuid import uuid4

from curate_d.domain import (
    ContentObjectId,
    DestinationConfig,
    PlacementId,
    Provider,
    SourceObject,
)
from curate_d.placement.pipeline import location_details, started_details
from curate_d.workflows.placement import verification_workflow_for

_HELD_BY = ContentObjectId(uuid4())


def _source(version_marker: str | None = "rev-7") -> SourceObject:
    """A source at a known path and version."""
    return SourceObject.build(
        provider=Provider.PCLOUD,
        account="source-account",
        native_id="f1",
        path="/Photos/IMG_0001.JPG",
        size_bytes=10,
        modified_at=datetime(2026, 1, 1, tzinfo=UTC),
        version_marker=version_marker,
    )


def test_the_location_names_the_provider_account_and_root() -> None:
    """The location in force when the copy started, not just the destination's name."""
    destination = DestinationConfig(
        name="library", provider=Provider.PCLOUD, account="dest-account", root_path="/Library"
    )
    assert location_details(destination) == {
        "destination_provider": "pcloud",
        "destination_account": "dest-account",
        "destination_root": "/Library",
    }


def test_a_started_entry_carries_the_source_version_attempt_and_context() -> None:
    """Context such as the policy version joins the copy's own evidence."""
    details = started_details(
        template_version="seed-1",
        source=_source(),
        attempt=2,
        transformations={"Photos": "photos"},
        rendered_locator="/Photos/IMG_0001.JPG",
        context={"policy_version": 3, "destination_root": "/Library"},
    )
    assert details == {
        "policy_version": 3,
        "destination_root": "/Library",
        "template_version": "seed-1",
        "source_path": "/Photos/IMG_0001.JPG",
        "source_version_marker": "rev-7",
        "attempt": 2,
        "original_Photos": "photos",
    }


def test_a_started_entry_names_the_content_that_held_the_rendered_locator() -> None:
    """The rendered locator is recorded only when other content pushed this one off it."""
    details = started_details(
        template_version="seed-1",
        source=_source(None),
        attempt=0,
        transformations={},
        rendered_locator="/Photos/IMG_0001.JPG",
        held_by=_HELD_BY,
    )
    assert details["rendered_locator"] == "/Photos/IMG_0001.JPG"
    assert details["held_by"] == str(_HELD_BY)
    assert details["source_version_marker"] is None


def test_context_never_overrides_the_copys_own_evidence() -> None:
    """A caller's key with the same name loses to what the start itself recorded."""
    details = started_details(
        template_version="seed-1",
        source=_source(),
        attempt=1,
        transformations={},
        rendered_locator="/a.jpg",
        context={"attempt": 99, "source_path": "/elsewhere"},
    )
    assert (details["attempt"], details["source_path"]) == (1, "/Photos/IMG_0001.JPG")


def test_each_attempt_is_verified_under_its_own_identity() -> None:
    """A later attempt's verification never collapses onto an earlier one's."""
    placement_id = PlacementId(uuid4())
    identities = {verification_workflow_for(placement_id, attempt) for attempt in range(3)}
    assert len(identities) == 3
    assert verification_workflow_for(placement_id, 2) == f"verify:{placement_id}:2"
