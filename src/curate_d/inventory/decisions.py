"""What a scan does with each file it lists.

Built on `ledger.change_detection.assess`, which compares a listing entry with the
recorded row. This module adds the consequences: a rename updates the path and nothing
else, new and changed files are (re)recorded with the digests the provider reports and
become downstream work, and an unchanged file is not touched at all.
"""

from enum import StrEnum

from curate_d.domain import (
    ChangeSignal,
    DomainModel,
    Provider,
    SourceObject,
    SourceObjectState,
    StrongFingerprint,
)
from curate_d.ledger.change_detection import ChangeVerdict, ListedFile, assess


class FileAction(StrEnum):
    """The consequence of listing one file."""

    NEW = "new"
    CHANGED = "changed"
    RENAMED = "renamed"
    UNCHANGED = "unchanged"

    @property
    def creates_work(self) -> bool:
        """Whether the file's content must be (re)ingested."""
        return self in (FileAction.NEW, FileAction.CHANGED)


class FileDecision(DomainModel):
    """One listed file, what to do with it, and the signal the decision rests on."""

    listed: ListedFile
    action: FileAction
    signal: ChangeSignal
    detail: str | None = None


def decide(listed: ListedFile, recorded: SourceObject | None) -> FileDecision:
    """Decide what listing `listed` means, given what the ledger holds for it."""
    assessment = assess(listed, recorded)
    if assessment.verdict is ChangeVerdict.NEW:
        action = FileAction.NEW
    elif assessment.verdict is ChangeVerdict.CHANGED:
        action = FileAction.CHANGED
    elif recorded is not None and recorded.path != listed.path:
        action = FileAction.RENAMED
    else:
        action = FileAction.UNCHANGED
    return FileDecision(
        listed=listed, action=action, signal=assessment.signal, detail=assessment.detail
    )


def apply_decision(
    decision: FileDecision,
    recorded: SourceObject | None,
    *,
    provider: Provider,
    account: str,
    reported_digests: tuple[StrongFingerprint, ...],
) -> SourceObject | None:
    """Return the source object to record for a decision, or None to write nothing.

    A changed file's link to its old content is cleared: the content it now holds has not
    been fingerprinted, and placements of the old content remain valid for the old bytes.

    Raises:
        ValueError: `recorded` is absent for an action that updates an existing row.
    """
    listed = decision.listed
    if decision.action is FileAction.UNCHANGED:
        return None
    if decision.action is FileAction.NEW:
        return SourceObject.build(
            provider=provider,
            account=account,
            native_id=listed.native_id,
            path=listed.path,
            size_bytes=listed.size_bytes,
            modified_at=listed.modified_at,
            version_marker=listed.version_marker,
            reported_digests=reported_digests,
        )
    if recorded is None:
        raise ValueError(f"{decision.action.value} needs the recorded row for {listed.native_id}")
    if decision.action is FileAction.RENAMED:
        return SourceObject.model_validate(recorded.model_dump() | {"path": listed.path})
    return SourceObject.model_validate(
        recorded.model_dump()
        | {
            "path": listed.path,
            "size_bytes": listed.size_bytes,
            "modified_at": listed.modified_at,
            "version_marker": listed.version_marker,
            "state": SourceObjectState.DISCOVERED,
            "content_object_id": None,
            "change_signal": decision.signal,
            "failure_reason": None,
            "last_verified_at": None,
            "reported_digests": reported_digests,
        }
    )
