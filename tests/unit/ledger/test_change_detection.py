"""Change detection between scans.

The interesting cases are the ones where the provider is wrong: a marker that fails to
advance, or a provider that offers no marker at all.
"""

from datetime import UTC, datetime, timedelta

import pytest

from curate_d.domain import ChangeSignal, Provider, SourceObject
from curate_d.ledger import ChangeVerdict, ListedFile, assess

_WHEN = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)


def _recorded(
    *, size_bytes: int = 100, version_marker: str | None = "rev-1", modified_at: datetime = _WHEN
) -> SourceObject:
    """Build a source object standing in for what the ledger has on record."""
    return SourceObject.build(
        provider=Provider.PCLOUD,
        account="acct",
        native_id="file-1",
        path="/a/file.jpg",
        size_bytes=size_bytes,
        modified_at=modified_at,
        version_marker=version_marker,
    )


def _listed(
    *, size_bytes: int = 100, version_marker: str | None = "rev-1", modified_at: datetime = _WHEN
) -> ListedFile:
    """Build a listing entry standing in for what a provider scan just reported."""
    return ListedFile(
        native_id="file-1",
        path="/a/file.jpg",
        size_bytes=size_bytes,
        modified_at=modified_at,
        version_marker=version_marker,
    )


def test_a_file_with_no_record_is_new() -> None:
    """A listing entry with no recorded row is NEW."""
    assert assess(_listed(), None).verdict is ChangeVerdict.NEW


def test_a_new_file_without_a_marker_records_the_weaker_signal() -> None:
    """A new file with no version marker is recorded under the size-and-time signal."""
    assessment = assess(_listed(version_marker=None), None)
    assert assessment.signal is ChangeSignal.SIZE_AND_MODIFIED_TIME


def test_an_unchanged_marker_and_size_means_no_content_read() -> None:
    """A matching marker and size yields UNCHANGED on the version-marker signal."""
    assessment = assess(_listed(), _recorded())
    assert assessment.verdict is ChangeVerdict.UNCHANGED
    assert assessment.signal is ChangeSignal.VERSION_MARKER


def test_an_advanced_marker_means_changed() -> None:
    """A version marker that differs from the recorded one is CHANGED."""
    assessment = assess(_listed(version_marker="rev-2"), _recorded())
    assert assessment.verdict is ChangeVerdict.CHANGED
    assert assessment.detail == "version_marker"


def test_a_size_change_behind_an_unchanged_marker_is_still_a_change() -> None:
    """Markers do not always advance. The bytes win over the provider's opinion."""
    assessment = assess(_listed(size_bytes=101), _recorded())
    assert assessment.verdict is ChangeVerdict.CHANGED
    assert assessment.detail == "size_bytes_despite_unchanged_version_marker"


def test_a_modification_time_change_behind_an_unchanged_marker_is_not_a_change() -> None:
    """Timestamps drift for reasons unrelated to content; the full rescan catches the rest."""
    assessment = assess(_listed(modified_at=_WHEN + timedelta(hours=1)), _recorded())
    assert assessment.verdict is ChangeVerdict.UNCHANGED


@pytest.mark.parametrize(
    ("listed_marker", "recorded_marker"),
    [(None, "rev-1"), ("rev-1", None), (None, None)],
)
def test_a_missing_marker_on_either_side_falls_back_to_size_and_time(
    listed_marker: str | None, recorded_marker: str | None
) -> None:
    """Missing a version marker on either side falls back to the size-and-time signal."""
    assessment = assess(
        _listed(version_marker=listed_marker, size_bytes=101),
        _recorded(version_marker=recorded_marker),
    )
    assert assessment.verdict is ChangeVerdict.CHANGED
    assert assessment.signal is ChangeSignal.SIZE_AND_MODIFIED_TIME
    assert assessment.detail == "size_bytes"


def test_the_fallback_notices_a_modification_time_change() -> None:
    """The size-and-time fallback detects a changed modification time."""
    assessment = assess(
        _listed(version_marker=None, modified_at=_WHEN + timedelta(minutes=1)),
        _recorded(version_marker=None),
    )
    assert assessment.verdict is ChangeVerdict.CHANGED
    assert assessment.detail == "modified_at"


def test_the_fallback_can_also_conclude_unchanged() -> None:
    """The size-and-time fallback reports UNCHANGED when neither attribute differs."""
    assessment = assess(_listed(version_marker=None), _recorded(version_marker=None))
    assert assessment.verdict is ChangeVerdict.UNCHANGED
    assert assessment.signal is ChangeSignal.SIZE_AND_MODIFIED_TIME
