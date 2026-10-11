"""The review queue: content the pipeline could not decide on by itself.

An item names the content object, why it is here, and any taxonomy values a tier proposed
that the taxonomy does not contain. Resolving it assigns facts, promotes a value into the
taxonomy, or excludes the content; the item then leaves the queue.
"""

from enum import StrEnum
from uuid import UUID

from pydantic import AwareDatetime, Field

from curate_d.domain.base import DomainModel
from curate_d.domain.ids import ContentObjectId
from curate_d.domain.taxonomy import TaxonomyField


class ReviewReason(StrEnum):
    """Why an item needs a person."""

    LOW_CONFIDENCE = "low_confidence"
    """No tier reached the confidence threshold."""

    REJECTED_VALUE = "rejected_value"
    """A tier proposed a taxonomy value the taxonomy does not contain."""

    MALFORMED_OUTPUT = "malformed_output"
    """The remote model's answer did not conform to the facts schema."""

    RENDER_FAILED = "render_failed"
    """The facts lack something the path template requires."""


class ReviewStatus(StrEnum):
    """Where an item stands."""

    OPEN = "open"
    RESOLVED = "resolved"
    EXCLUDED = "excluded"


class RejectedValue(DomainModel):
    """A value proposed for a closed-set fact and refused by the taxonomy."""

    field: TaxonomyField
    value: str = Field(min_length=1)
    category: str | None = Field(
        default=None, description="The category a rejected subcategory was proposed under."
    )


class ReviewItem(DomainModel):
    """One content object awaiting a person's decision."""

    id: UUID
    content_object_id: ContentObjectId
    reason: ReviewReason
    detail: str = Field(min_length=1)
    rejected_values: tuple[RejectedValue, ...] = ()
    status: ReviewStatus = ReviewStatus.OPEN
    created_at: AwareDatetime
    resolved_at: AwareDatetime | None = None
    resolution: str | None = None
