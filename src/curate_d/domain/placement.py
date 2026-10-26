"""The per-destination identity layer.

A placement is one content object's copy at one destination, together with the strongest
check that copy actually survived. Source release is gated on that level, so the ladder
below is the load-bearing part of the zero-loss guarantee.
"""

from enum import IntEnum, StrEnum

from pydantic import AwareDatetime, Field, field_validator

from curate_d.domain.base import DomainModel
from curate_d.domain.ids import (
    ContentObjectId,
    DestinationId,
    PlacementId,
    SourceObjectId,
    derive_destination_id,
    derive_placement_id,
    normalize_root,
)
from curate_d.domain.source import Provider


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


class DestinationIdentity(DomainModel):
    """Where copies are kept: one account's root at one provider.

    A placement belongs to this, not to the policy's name for it. Two names with one
    identity are one destination; one name pointed at another account or root is
    another destination.
    """

    provider: Provider
    account: str = Field(min_length=1)
    root_path: str = Field(min_length=1, description="Path all rendered locators hang from.")

    @field_validator("root_path")
    @classmethod
    def _normalize_root(cls, value: str) -> str:
        """Return the root in canonical form; see `normalize_root`.

        Raises:
            ValueError: the root is not absolute or has an empty, "." or ".." segment.
        """
        return normalize_root(value)

    @property
    def id(self) -> DestinationId:
        """Return the identity's derived identifier."""
        return derive_destination_id(self.provider.value, self.account, self.root_path)

    def contains(self, other: "DestinationIdentity") -> bool:
        """Return whether `other` is this destination or lies inside it.

        Two such destinations would hold the same physical files, so one copy would
        count twice.
        """
        if (self.provider, self.account) != (other.provider, other.account):
            return False
        if self.root_path == "/":
            return True
        return other.root_path == self.root_path or other.root_path.startswith(self.root_path + "/")

    def label(self) -> str:
        """Return the identity as one readable string, "provider:account:root"."""
        return f"{self.provider.value}:{self.account}:{self.root_path}"


class Placement(DomainModel):
    """One content object's copy at one destination."""

    id: PlacementId
    content_object_id: ContentObjectId
    destination_id: DestinationId = Field(
        description="The destination identity the copy is at; see `DestinationIdentity`."
    )
    destination_name: str = Field(
        min_length=1,
        description="The policy's name for the destination when this was last placed: a "
        "label, not part of the placement's identity.",
    )
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
        destination_id: DestinationId,
        destination_name: str,
        locator: str,
        template_version: str,
    ) -> "Placement":
        """Construct with the identifier derived from content object and destination identity.

        A row recorded before destinations had identities keeps the identifier it was
        given then, so look an existing placement up by content and destination, never
        by this identifier.
        """
        return cls(
            id=derive_placement_id(content_object_id, destination_id),
            content_object_id=content_object_id,
            destination_id=destination_id,
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
    UNSELECTED = "unselected"
    """Classified, but no placement rule selects a destination for it."""


class PolicySource(StrEnum):
    """Where a dry run's placement rules came from."""

    ACTIVE = "active"
    """The policy in force when the run started."""
    DRAFT = "draft"
    """A draft policy submitted with the run and stored nowhere else."""
    DESTINATIONS = "destinations"
    """No rules: every object was evaluated against the destinations the run named."""
