"""Deciding whether a file changed since the last scan.

Pure functions over a listing entry and the recorded row. Keeping the decision separate
from the I/O that produced either side is what makes every branch here — including the
ones that only occur on providers we cannot test against — unit-testable.
"""

from enum import StrEnum

from pydantic import AwareDatetime, Field

from curate_d.domain.base import DomainModel
from curate_d.domain.source import ChangeSignal, SourceObject


class ChangeVerdict(StrEnum):
    """What a scan should do with a file it just listed."""

    NEW = "new"
    """No record exists; ingest it."""

    UNCHANGED = "unchanged"
    """Recorded state matches; skip without reading content."""

    CHANGED = "changed"
    """Recorded state differs; re-ingest."""


class ListedFile(DomainModel):
    """One entry as a provider's listing reports it, before anything is recorded."""

    native_id: str = Field(min_length=1)
    path: str = Field(min_length=1)
    size_bytes: int = Field(ge=0)
    modified_at: AwareDatetime
    version_marker: str | None = None


class ChangeAssessment(DomainModel):
    """The verdict plus the signal it was reached on."""

    verdict: ChangeVerdict
    signal: ChangeSignal
    detail: str | None = Field(
        default=None,
        description="Which attribute differed, for the audit trail.",
    )


def assess(listed: ListedFile, recorded: SourceObject | None) -> ChangeAssessment:
    """Compare a listing entry against what the ledger holds.

    Where the provider supplies a version marker it is authoritative and size is checked
    alongside it, since a marker that fails to advance is the failure mode the periodic
    full rescan exists to catch. Without a marker the comparison falls back to size and
    modification time, and says so: a weaker signal that claims to be a strong one is
    worse than no signal.
    """
    if recorded is None:
        return ChangeAssessment(
            verdict=ChangeVerdict.NEW,
            signal=(
                ChangeSignal.VERSION_MARKER
                if listed.version_marker is not None
                else ChangeSignal.SIZE_AND_MODIFIED_TIME
            ),
            detail=None,
        )

    if listed.version_marker is not None and recorded.version_marker is not None:
        if listed.version_marker != recorded.version_marker:
            return ChangeAssessment(
                verdict=ChangeVerdict.CHANGED,
                signal=ChangeSignal.VERSION_MARKER,
                detail="version_marker",
            )
        if listed.size_bytes != recorded.size_bytes:
            # The marker says unchanged and the size says otherwise. Trust the bytes.
            return ChangeAssessment(
                verdict=ChangeVerdict.CHANGED,
                signal=ChangeSignal.VERSION_MARKER,
                detail="size_bytes_despite_unchanged_version_marker",
            )
        return ChangeAssessment(
            verdict=ChangeVerdict.UNCHANGED, signal=ChangeSignal.VERSION_MARKER, detail=None
        )

    if listed.size_bytes != recorded.size_bytes:
        return ChangeAssessment(
            verdict=ChangeVerdict.CHANGED,
            signal=ChangeSignal.SIZE_AND_MODIFIED_TIME,
            detail="size_bytes",
        )
    if listed.modified_at != recorded.modified_at:
        return ChangeAssessment(
            verdict=ChangeVerdict.CHANGED,
            signal=ChangeSignal.SIZE_AND_MODIFIED_TIME,
            detail="modified_at",
        )
    return ChangeAssessment(
        verdict=ChangeVerdict.UNCHANGED, signal=ChangeSignal.SIZE_AND_MODIFIED_TIME, detail=None
    )
