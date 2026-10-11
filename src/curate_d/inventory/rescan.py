"""What a full rescan concludes about each file it re-reads.

Incremental scans trust the provider's version marker. A full rescan does not: it reads
the content again and compares its fingerprint with the one recorded. A difference behind
an unchanged marker is exactly the failure the marker was trusted not to have, so it is
recorded as a discrepancy against that provider as well as being re-ingested.
"""

from enum import StrEnum

from pydantic import AwareDatetime

from curate_d.domain import (
    ChangeSignal,
    SourceObject,
    SourceObjectState,
    StrongFingerprint,
)
from curate_d.ingest.hashing import ContentDigests


class RescanVerdict(StrEnum):
    """The outcome of re-reading one file."""

    CONFIRMED = "confirmed"
    """Its content is still the recorded content."""

    DISCREPANCY = "discrepancy"
    """Its content changed while its version marker did not."""

    UNFINGERPRINTED = "unfingerprinted"
    """No content is recorded to compare with; it is ingested again instead."""


def assess_rescan(
    source: SourceObject, recorded: StrongFingerprint | None, observed: ContentDigests
) -> RescanVerdict:
    """Compare what was read with the recorded fingerprint."""
    if source.content_object_id is None or recorded is None:
        return RescanVerdict.UNFINGERPRINTED
    if recorded.matches(observed.fingerprint):
        return RescanVerdict.CONFIRMED
    return RescanVerdict.DISCREPANCY


def apply_discrepancy(
    source: SourceObject, observed: ContentDigests, *, now: AwareDatetime
) -> SourceObject:
    """The source as a discrepancy leaves it: back to discovered, awaiting re-ingest.

    Its reported digests are replaced with what the rescan read. The listing's digests
    described the old content, and an ingest comparing new bytes against them would
    reject the file as changed indefinitely.
    """
    return SourceObject.model_validate(
        source.model_dump()
        | {
            "size_bytes": observed.size_bytes,
            "state": SourceObjectState.DISCOVERED,
            "content_object_id": None,
            "change_signal": ChangeSignal.CONTENT_FINGERPRINT,
            "last_verified_at": now,
            "failure_reason": None,
            "reported_digests": observed.as_reported,
        }
    )
