"""What a person decides about an item in review, checked before anything is recorded.

Assigned facts are held to the same schema and taxonomy as any tier's: a value the
taxonomy lacks is refused, and the way to use it is to promote it first. Accepted facts
become the content's classification at the human-review tier, which makes it eligible for
rendering and placement. An excluded item's content is never placed. When the taxonomy
grows to contain what an item was refused for, the item's content is classified again.
"""

from collections.abc import Sequence
from typing import Final

from pydantic import AwareDatetime

from curate_d.classification.validation import taxonomy_rejections
from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    AuditValue,
    ClassificationResult,
    ClassificationTier,
    ContentObjectId,
    Facts,
    RejectedValue,
    ReviewItem,
    ReviewReason,
    ReviewStatus,
    Taxonomy,
)

DECIDED_BY: Final = "review"


class ResolutionError(ValueError):
    """A resolution was refused; the item stays in review."""


def _require_open(item: ReviewItem) -> None:
    """Refuse to resolve an item that is no longer in review.

    Raises:
        ResolutionError: the item was already resolved or excluded.
    """
    if item.status is not ReviewStatus.OPEN:
        raise ResolutionError(f"review item {item.id} is already {item.status.value}")


def resolve_with_facts(
    item: ReviewItem,
    facts: Facts,
    taxonomy: Taxonomy,
    *,
    taxonomy_version: int,
    by: str,
    now: AwareDatetime,
) -> tuple[ClassificationResult, AuditEntry]:
    """The classification and audit entry that resolving `item` with `facts` records.

    Raises:
        ResolutionError: the item is not open, or a value is outside the taxonomy.
    """
    _require_open(item)
    rejected = taxonomy_rejections(facts, taxonomy)
    if rejected:
        listed = ", ".join(f"{value.field.value} {value.value!r}" for value in rejected)
        raise ResolutionError(
            f"outside the taxonomy: {listed}; promote them before resolving with them"
        )
    result = ClassificationResult(
        content_object_id=item.content_object_id,
        facts=facts,
        tier=ClassificationTier.HUMAN_REVIEW,
        confidence=1.0,
        produced_at=now,
        taxonomy_version=taxonomy_version,
    )
    details: dict[str, AuditValue] = {
        "tier": ClassificationTier.HUMAN_REVIEW.value,
        "review_item": str(item.id),
        "review_reason": item.reason.value,
        "media_kind": facts.media_kind.value,
        "category": facts.category,
        "subcategory": facts.subcategory,
        "captured_at": None if facts.captured_at is None else facts.captured_at.isoformat(),
        "taxonomy_version": taxonomy_version,
    }
    audit = AuditEntry(
        entry_type=AuditEntryType.CLASSIFICATION_DECIDED,
        occurred_at=now,
        decided_by=f"{DECIDED_BY}:{by}",
        details=details,
        content_object_id=item.content_object_id,
    )
    return result, audit


def resolution_summary(facts: Facts) -> str:
    """How a resolution by facts is described on the closed item."""
    parts = [facts.media_kind.value, facts.category, facts.subcategory]
    return "facts assigned: " + "/".join(part for part in parts if part)


def exclusion_audit(item: ReviewItem, reason: str, *, by: str, now: AwareDatetime) -> AuditEntry:
    """The audit entry for excluding `item`'s content from organization.

    Raises:
        ResolutionError: the item is not open.
    """
    _require_open(item)
    return AuditEntry(
        entry_type=AuditEntryType.CLASSIFICATION_REJECTED,
        occurred_at=now,
        decided_by=f"{DECIDED_BY}:{by}",
        details={
            "resolution": "excluded",
            "reason": reason,
            "review_item": str(item.id),
            "review_reason": item.reason.value,
        },
        content_object_id=item.content_object_id,
    )


def decided_by_person(existing: ClassificationResult | None, *, excluded: bool) -> bool:
    """Whether a person has already decided the content: resolved it, or excluded it.

    An automatic result arriving after that, such as a re-classification whose remote
    call was still running, must not replace the person's decision.
    """
    return excluded or (existing is not None and existing.tier is ClassificationTier.HUMAN_REVIEW)


def set_aside_audit(
    content_object_id: ContentObjectId,
    tier: ClassificationTier,
    facts: Facts,
    *,
    now: AwareDatetime,
) -> AuditEntry:
    """The audit entry for an automatic result set aside because a person decided first."""
    return AuditEntry(
        entry_type=AuditEntryType.CLASSIFICATION_REJECTED,
        occurred_at=now,
        decided_by=f"classification:{tier.value}",
        details={
            "set_aside": "a person decided this content first",
            "tier": tier.value,
            "category": facts.category,
            "subcategory": facts.subcategory,
        },
        content_object_id=content_object_id,
    )


def values_to_promote(item: ReviewItem, requested: Sequence[RejectedValue]) -> list[RejectedValue]:
    """The rejected values of `item` to add to the taxonomy: those requested, or all.

    Raises:
        ResolutionError: the item is not open, rejected nothing, or a requested value is
            not one it rejected.
    """
    _require_open(item)
    if not item.rejected_values:
        raise ResolutionError(f"review item {item.id} rejected no taxonomy values")
    if not requested:
        return list(item.rejected_values)
    foreign = [value for value in requested if value not in item.rejected_values]
    if foreign:
        listed = ", ".join(f"{value.field.value} {value.value!r}" for value in foreign)
        raise ResolutionError(f"review item {item.id} did not reject {listed}")
    return list(requested)


def now_acceptable(items: Sequence[ReviewItem], taxonomy: Taxonomy) -> list[ReviewItem]:
    """Open items held back only by values `taxonomy` now contains.

    These are worth classifying again: the values that sent them to review would now be
    accepted. An item that rejected nothing, or still rejects something, is left alone.
    """
    return [
        item
        for item in items
        if item.status is ReviewStatus.OPEN
        and item.reason is ReviewReason.REJECTED_VALUE
        and item.rejected_values
        and all(
            taxonomy.accepts(value.field, value.value, category=value.category)
            for value in item.rejected_values
        )
    ]
