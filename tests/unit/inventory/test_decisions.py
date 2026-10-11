"""What a scan records for each listed file."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from curate_d.domain import (
    ChangeSignal,
    ContentObjectId,
    HashAlgorithm,
    Provider,
    SourceObject,
    SourceObjectState,
    StrongFingerprint,
)
from curate_d.inventory.decisions import FileAction, apply_decision, decide
from curate_d.ledger.change_detection import ListedFile

_WHEN = datetime(2026, 9, 1, tzinfo=UTC)
_SHA1 = StrongFingerprint(algorithm=HashAlgorithm.SHA1, digest="1" * 40)
_NEW_SHA1 = StrongFingerprint(algorithm=HashAlgorithm.SHA1, digest="2" * 40)


def _listed(
    *, path: str = "/a/x.jpg", size: int = 10, marker: str | None = "m1", modified: datetime = _WHEN
) -> ListedFile:
    """A listing entry for file 7."""
    return ListedFile(
        native_id="7", path=path, size_bytes=size, modified_at=modified, version_marker=marker
    )


def _recorded(*, marker: str | None = "m1") -> SourceObject:
    """File 7 as recorded after fingerprinting."""
    return SourceObject.build(
        provider=Provider.PCLOUD,
        account="acct",
        native_id="7",
        path="/a/x.jpg",
        size_bytes=10,
        modified_at=_WHEN,
        version_marker=marker,
        reported_digests=(_SHA1,),
    ).model_copy(
        update={
            "state": SourceObjectState.FINGERPRINTED,
            "content_object_id": ContentObjectId(uuid4()),
            "last_verified_at": _WHEN,
        }
    )


def _apply(listed: ListedFile, recorded: SourceObject | None) -> SourceObject | None:
    """Decide and apply, with a fresh digest reported."""
    return apply_decision(
        decide(listed, recorded),
        recorded,
        provider=Provider.PCLOUD,
        account="acct",
        reported_digests=(_NEW_SHA1,),
    )


def test_an_unchanged_file_is_skipped_without_work() -> None:
    """Same identifier, marker and size: nothing is written or created."""
    decision = decide(_listed(), _recorded())
    assert decision.action is FileAction.UNCHANGED
    assert not decision.action.creates_work
    assert _apply(_listed(), _recorded()) is None


def test_a_new_file_is_recorded_with_its_digests_and_creates_work() -> None:
    """A file never seen before becomes a discovered source object."""
    decision = decide(_listed(), None)
    assert decision.action is FileAction.NEW
    assert decision.action.creates_work
    created = _apply(_listed(), None)
    assert created is not None
    assert created.state is SourceObjectState.DISCOVERED
    assert created.reported(HashAlgorithm.SHA1) == _NEW_SHA1


@pytest.mark.parametrize("listed", [_listed(marker="m2"), _listed(size=11)])
def test_a_modified_file_is_rerecorded_and_creates_work(listed: ListedFile) -> None:
    """A new marker or size resets the row to discovered with the new digests."""
    recorded = _recorded()
    decision = decide(listed, recorded)
    assert decision.action is FileAction.CHANGED
    assert decision.action.creates_work
    updated = _apply(listed, recorded)
    assert updated is not None
    assert updated.id == recorded.id
    assert updated.state is SourceObjectState.DISCOVERED
    assert updated.content_object_id is None
    assert updated.last_verified_at is None
    assert updated.reported_digests == (_NEW_SHA1,)
    assert (updated.version_marker, updated.size_bytes) == (
        listed.version_marker,
        listed.size_bytes,
    )


def test_a_renamed_file_updates_its_existing_source_object() -> None:
    """Same identifier at a new path: the row keeps its identity, content and state."""
    recorded = _recorded()
    listed = _listed(path="/b/renamed.jpg")
    decision = decide(listed, recorded)
    assert decision.action is FileAction.RENAMED
    assert not decision.action.creates_work
    updated = _apply(listed, recorded)
    assert updated is not None
    assert updated.id == recorded.id
    assert updated.path == "/b/renamed.jpg"
    assert updated.content_object_id == recorded.content_object_id
    assert updated.state is SourceObjectState.FINGERPRINTED
    assert updated.reported_digests == recorded.reported_digests


def test_without_a_marker_the_weaker_signal_is_recorded() -> None:
    """A marker-less provider falls back to size and time, and the change says so."""
    recorded = _recorded(marker=None)
    listed = _listed(marker=None, modified=_WHEN + timedelta(seconds=1))
    decision = decide(listed, recorded)
    assert decision.action is FileAction.CHANGED
    assert decision.signal is ChangeSignal.SIZE_AND_MODIFIED_TIME
    updated = _apply(listed, recorded)
    assert updated is not None
    assert updated.change_signal is ChangeSignal.SIZE_AND_MODIFIED_TIME


def test_an_update_without_the_recorded_row_is_refused() -> None:
    """Applying a change to a row that was not read is a caller bug."""
    decision = decide(_listed(marker="m2"), _recorded())
    with pytest.raises(ValueError, match="recorded row"):
        apply_decision(
            decision, None, provider=Provider.PCLOUD, account="acct", reported_digests=()
        )
