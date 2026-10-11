"""Classification facts.

Facts describe what a file *is*. They never describe where it goes: rendering is a
separate, versioned function of facts, which is what makes relayout free and identical
inputs reproducible.
"""

from enum import StrEnum

from pydantic import AwareDatetime, Field

from curate_d.domain.base import DomainModel
from curate_d.domain.ids import ContentObjectId


class MediaKind(StrEnum):
    """Coarse content class, resolvable from the media type alone."""

    DOCUMENT = "document"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    ARCHIVE = "archive"
    OTHER = "other"


class ClassificationTier(StrEnum):
    """Which technique produced a result, in ascending order of cost."""

    DETERMINISTIC = "deterministic"
    LOCAL_INFERENCE = "local_inference"
    REMOTE_MODEL = "remote_model"
    HUMAN_REVIEW = "human_review"


class GeoPoint(DomainModel):
    """A location fact, from embedded metadata rather than inference."""

    latitude: float = Field(ge=-90.0, le=90.0)
    longitude: float = Field(ge=-180.0, le=180.0)


class Facts(DomainModel):
    """The closed set of things classification may assert about a content object.

    Every field is optional because a tier asserts only what it can support. Which of
    them a given destination path actually needs is the template's business, and a
    template that requires an absent fact fails loudly rather than substituting a
    placeholder.
    """

    media_kind: MediaKind
    category: str | None = Field(
        default=None,
        description="Taxonomy value; validated against the configured closed set.",
    )
    subcategory: str | None = Field(default=None, description="Taxonomy value.")
    title: str | None = Field(default=None, description="Human-meaningful name for the content.")
    captured_at: AwareDatetime | None = Field(
        default=None,
        description="When the content was created, from embedded metadata where available.",
    )
    place: GeoPoint | None = None
    place_label: str | None = Field(
        default=None,
        description="Taxonomy value naming the location; derived from clustering, not guessed.",
    )
    event_group: str | None = Field(
        default=None,
        description="Identifier of the space-and-time cluster this content belongs to.",
    )
    correspondent: str | None = Field(
        default=None,
        description="Issuing or receiving party for documents.",
    )
    language: str | None = Field(
        default=None,
        min_length=2,
        max_length=8,
        description="BCP 47 language tag of extracted text.",
    )


class ClassificationResult(DomainModel):
    """Facts plus the provenance needed to explain or reproduce them.

    The model identifier is recorded rather than inferred from configuration, so results
    produced before and after a model change can be told apart in the audit trail.
    """

    content_object_id: ContentObjectId
    facts: Facts
    tier: ClassificationTier
    confidence: float = Field(ge=0.0, le=1.0)
    produced_at: AwareDatetime
    model_id: str | None = Field(
        default=None,
        description="Remote model actually called; absent for tiers that call no model.",
    )
    taxonomy_version: int | None = Field(
        default=None,
        ge=1,
        description="Taxonomy the closed-set facts were checked against.",
    )
