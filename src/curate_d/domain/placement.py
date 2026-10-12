"""The per-destination identity layer.

A placement is one content object's copy at one destination, together with the strongest
check that copy actually survived. Source release is gated on that level, so the ladder
below is the load-bearing part of the zero-loss guarantee.
"""

from enum import IntEnum, StrEnum

from pydantic import AwareDatetime, Field

from curate_d.domain.base import DomainModel
from curate_d.domain.ids import (
    ContentObjectId,
    PlacementId,
    SourceObjectId,
    derive_placement_id,
)


class VerificationLevel(IntEnum):
    """How strongly a destination copy was confirmed to match its source.

    Ordered, so a policy can require "at least" a level rather than enumerate rungs.
    Only the two strong rungs may ever unlock releasing a source.
    """

    UNVERIFIABLE = 0
    """Nothing about the destination copy could be compared to the source."""

    WEAK = 1
    """Size and modification time agreed; the bytes were never checked."""

    STRONG_IN_TRANSIT = 2
    """We computed a digest over the bytes ourselves and it matched the reported digest.

    The digest is computed in transit or over a read-back from the destination, and
    compared with the digest the other side reports.
    """

    STRONG_COMMON_HASH = 3
    """Both ends reported the same algorithm's digest and the digests matched."""

    @property
    def permits_source_release(self) -> bool:
        """Whether a copy at this level may count toward releasing the original."""
        return self >= VerificationLevel.STRONG_IN_TRANSIT


class PlacementState(StrEnum):
    """Lifecycle of a single placement.

    PRESENT is only ever reached through verification, never through a completed copy:
    an unverified copy is indistinguishable from a corrupted one.
    """

    PENDING = "pending"
    COPYING = "copying"
    VERIFYING = "verifying"
    PRESENT = "present"
    ABSENT = "absent"
    FAILED = "failed"
    UNTRUSTED = "untrusted"


class Destination(DomainModel):
    """A named target the system can place content into."""

    name: str = Field(min_length=1, description="Stable configuration key for this target.")
    provider: str = Field(min_length=1)
    account: str = Field(min_length=1)
    root_path: str = Field(min_length=1, description="Path all rendered locators hang from.")


class Placement(DomainModel):
    """One content object's copy at one destination."""

    id: PlacementId
    content_object_id: ContentObjectId
    destination_name: str = Field(min_length=1)
    locator: str = Field(min_length=1, description="Rendered destination path.")
    template_version: str = Field(min_length=1, description="Template that produced the locator.")
    state: PlacementState = PlacementState.PENDING
    verification_level: VerificationLevel = VerificationLevel.UNVERIFIABLE
    verified_at: AwareDatetime | None = None
    failure_reason: str | None = None
    observed_source_digest: str | None = Field(
        default=None,
        description="Source-side value at the last verification; kept on mismatch as evidence.",
    )
    observed_destination_digest: str | None = Field(
        default=None,
        description="Destination-side value at the last verification.",
    )
    source_object_id: SourceObjectId | None = Field(
        default=None, description="The source copied by the latest attempt."
    )
    attempts: int = Field(
        default=0, ge=0, description="Attempts after the first; each is a new workflow."
    )

    @classmethod
    def build(
        cls,
        *,
        content_object_id: ContentObjectId,
        destination_name: str,
        locator: str,
        template_version: str,
    ) -> "Placement":
        """Construct with the identifier derived from content object and destination."""
        return cls(
            id=derive_placement_id(content_object_id, destination_name),
            content_object_id=content_object_id,
            destination_name=destination_name,
            locator=locator,
            template_version=template_version,
        )


class DryRunOutcome(StrEnum):
    """What a dry run concluded for one content object at one destination."""

    PROPOSED = "proposed"
    ALREADY_PRESENT = "already_present"
    RENDER_FAILED = "render_failed"
    UNCLASSIFIED = "unclassified"


class PolicySource(StrEnum):
    """Where a dry run's placement rules came from."""

    ACTIVE = "active"
    """The policy in force when the run started."""
    DRAFT = "draft"
    """A draft policy submitted with the run and stored nowhere else."""
    DESTINATIONS = "destinations"
    """No rules: every object was evaluated against the destinations the run named."""
