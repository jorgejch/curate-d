"""The placement workflow's ledger transactions, against a faked datasource."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from curate_d.domain import (
    ContentObjectId,
    DestinationConfig,
    HashAlgorithm,
    Placement,
    PlacementId,
    PlacementState,
    Provider,
    SourceObject,
    StrongFingerprint,
    derive_destination_id,
    derive_source_object_id,
)
from curate_d.workflows import placement

_SOURCE = SourceObject(
    id=derive_source_object_id("pcloud", "acct-1", "file-9"),
    provider=Provider.PCLOUD,
    account="acct-1",
    native_id="file-9",
    path="/Photos/2026/img.heic",
    size_bytes=1024,
    modified_at=datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC),
)


@pytest.mark.parametrize(("checkpoint", "recorded"), [(True, True), (False, False), (None, True)])
def test_a_pre_deploy_none_checkpoint_counts_as_recorded(
    checkpoint: bool | None, recorded: bool
) -> None:
    """A replayed defer, fail or evidence None was an unguarded write that landed."""
    assert placement._recorded(checkpoint) is recorded


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        (PlacementState.PRESENT, placement.PlaceOutcome.ALREADY_PRESENT),
        (PlacementState.COPYING, placement.PlaceOutcome.SUPERSEDED),
        (PlacementState.VERIFYING, placement.PlaceOutcome.SUPERSEDED),
    ],
)
def test_a_start_that_copies_nothing_ends_by_the_state_it_left(
    state: PlacementState, outcome: placement.PlaceOutcome
) -> None:
    """A registered placement ends already present; an attempt under way elsewhere, superseded."""
    left = Placement.build(
        content_object_id=ContentObjectId(uuid4()),
        destination_id=derive_destination_id("pcloud", "acct-1", "/Library"),
        destination_name="library",
        locator="/Photos/2026/img.heic",
        template_version="seed-1",
    ).model_copy(update={"state": state})
    assert placement._unstarted(left) is outcome


@pytest.mark.parametrize(
    ("checkpoint", "conclusion"),
    [
        ((True, _SOURCE), (True, _SOURCE)),
        ((True, None), (True, None)),
        ((False, None), (False, None)),
        (_SOURCE, (True, _SOURCE)),
        (None, (True, None)),
    ],
)
def test_a_pre_deploy_conclude_checkpoint_reads_as_concluded(
    checkpoint: tuple[bool, SourceObject | None] | SourceObject | None,
    conclusion: tuple[bool, SourceObject | None],
) -> None:
    """A replayed bare source or None concluded, and keeps the source it sent back."""
    assert placement._conclusion(checkpoint) == conclusion


@pytest.mark.parametrize("recorded", [True, False])
async def test_a_deferral_reports_whether_the_attempt_was_still_copying(
    monkeypatch: pytest.MonkeyPatch, recorded: bool
) -> None:
    """The deferral's result reaches the workflow, so a superseded wait ends it."""
    connection = object()
    seen: list[tuple[object, PlacementId, str, int | None]] = []

    async def session_connection() -> object:
        """Return the transaction's connection."""
        return connection

    async def defer(
        given: object, given_id: PlacementId, reason: str, *, attempt: int | None = None
    ) -> bool:
        """Record the call and report `recorded`."""
        seen.append((given, given_id, reason, attempt))
        return recorded

    datasource = SimpleNamespace(sql_session=lambda: SimpleNamespace(connection=session_connection))
    monkeypatch.setattr(placement, "current", lambda: SimpleNamespace(datasource=datasource))
    monkeypatch.setattr(placement, "defer", defer)
    placement_id = PlacementId(uuid4())

    assert await placement._defer_tx(placement_id, "scratch exhausted", 3) is recorded

    assert seen == [(connection, placement_id, "scratch exhausted", 3)]


def _request(account: str, name: str = "library") -> placement.PlacementRequest:
    """Return a first attempt at placing one file under `name` at `/Library` of `account`."""
    return placement.PlacementRequest(
        content_object_id=ContentObjectId(uuid4()),
        fingerprint=StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64),
        destination=DestinationConfig(
            name=name, provider=Provider.PCLOUD, account=account, root_path="/Library"
        ),
        locator="/Library/img.heic",
        template_version="seed-1",
        size_bytes=1024,
    )


def test_a_placement_at_a_names_new_account_is_other_work() -> None:
    """One name at two accounts is two workflows; a rename is the same one."""
    old, new = _request("acct-1"), _request("acct-2")
    renamed = _request("acct-1", name="photos")
    assert placement.placement_workflow_for(old) != placement.placement_workflow_for(new)
    assert placement.placement_workflow_for(old) == placement.placement_workflow_for(renamed)
