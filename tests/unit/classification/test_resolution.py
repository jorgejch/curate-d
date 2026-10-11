"""A person's resolution of a review item, checked before anything is recorded."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from curate_d.classification.resolution import (
    ResolutionError,
    decided_by_person,
    exclusion_audit,
    now_acceptable,
    resolution_summary,
    resolve_with_facts,
    set_aside_audit,
    values_to_promote,
)
from curate_d.domain import (
    SEED_TAXONOMY,
    AuditEntryType,
    ClassificationResult,
    ClassificationTier,
    ContentObjectId,
    Facts,
    MediaKind,
    RejectedValue,
    ReviewItem,
    ReviewReason,
    ReviewStatus,
    TaxonomyField,
)

_NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)
_RECEIPTS = RejectedValue(field=TaxonomyField.SUBCATEGORY, value="Receipts", category="Documents")
_BOOKS = RejectedValue(field=TaxonomyField.CATEGORY, value="Books")


def _item(**changes: object) -> ReviewItem:
    """An open item that rejected a subcategory."""
    return ReviewItem(
        id=uuid4(),
        content_object_id=ContentObjectId(uuid4()),
        reason=ReviewReason.REJECTED_VALUE,
        detail="subcategory outside the taxonomy",
        rejected_values=(_RECEIPTS,),
        created_at=_NOW,
    ).model_copy(update=changes)


def _facts(**changes: object) -> Facts:
    """Document facts inside the seed taxonomy."""
    return Facts(
        media_kind=MediaKind.DOCUMENT, category="Documents", subcategory="Finance"
    ).model_copy(update=changes)


def test_valid_facts_become_a_human_review_classification() -> None:
    """Full confidence, the taxonomy version, and an audit entry naming the person."""
    item = _item()
    result, audit = resolve_with_facts(
        item, _facts(), SEED_TAXONOMY, taxonomy_version=3, by="tester", now=_NOW
    )
    assert (result.tier, result.confidence, result.taxonomy_version) == (
        ClassificationTier.HUMAN_REVIEW,
        1.0,
        3,
    )
    assert result.content_object_id == item.content_object_id
    assert audit.entry_type is AuditEntryType.CLASSIFICATION_DECIDED
    assert audit.decided_by == "review:tester"
    assert audit.details["review_item"] == str(item.id)
    assert audit.details["subcategory"] == "Finance"


def test_a_value_outside_the_taxonomy_is_refused_with_a_way_forward() -> None:
    """The message names the value and says to promote it."""
    with pytest.raises(ResolutionError, match=r"subcategory 'Receipts'.*promote"):
        resolve_with_facts(
            _item(),
            _facts(subcategory="Receipts"),
            SEED_TAXONOMY,
            taxonomy_version=1,
            by="tester",
            now=_NOW,
        )


def test_a_closed_item_cannot_be_resolved_again() -> None:
    """Resolving twice is refused."""
    with pytest.raises(ResolutionError, match="already resolved"):
        resolve_with_facts(
            _item(status=ReviewStatus.RESOLVED),
            _facts(),
            SEED_TAXONOMY,
            taxonomy_version=1,
            by="tester",
            now=_NOW,
        )


def test_the_summary_names_the_assigned_classification() -> None:
    """Kind, category and subcategory, as far as they are given."""
    assert resolution_summary(_facts()) == "facts assigned: document/Documents/Finance"
    assert resolution_summary(Facts(media_kind=MediaKind.PHOTO)) == "facts assigned: photo"


def test_an_exclusion_is_audited_with_its_reason() -> None:
    """Recorded as a classification the person rejected."""
    item = _item()
    audit = exclusion_audit(item, "personal", by="tester", now=_NOW)
    assert audit.entry_type is AuditEntryType.CLASSIFICATION_REJECTED
    assert (audit.details["resolution"], audit.details["reason"]) == ("excluded", "personal")
    assert audit.content_object_id == item.content_object_id


def test_an_excluded_item_cannot_be_excluded_again() -> None:
    """Closed items stay closed."""
    with pytest.raises(ResolutionError, match="already excluded"):
        exclusion_audit(_item(status=ReviewStatus.EXCLUDED), "x", by="tester", now=_NOW)


def test_promoting_with_no_values_named_promotes_all_the_item_rejected() -> None:
    """The common case: take what the item was refused."""
    assert values_to_promote(_item(), []) == [_RECEIPTS]


def test_only_values_the_item_rejected_can_be_promoted_through_it() -> None:
    """Promoting something else goes through configuration, not review."""
    with pytest.raises(ResolutionError, match="did not reject category 'Books'"):
        values_to_promote(_item(), [_BOOKS])


def test_an_item_that_rejected_nothing_has_nothing_to_promote() -> None:
    """Low confidence is not a taxonomy gap."""
    with pytest.raises(ResolutionError, match="rejected no taxonomy values"):
        values_to_promote(_item(reason=ReviewReason.LOW_CONFIDENCE, rejected_values=()), [])


def test_items_held_only_for_values_now_in_the_taxonomy_are_reclassifiable() -> None:
    """Receipts promoted under Documents frees the item that was refused it."""
    grown = SEED_TAXONOMY.promote(TaxonomyField.SUBCATEGORY, "Receipts", category="Documents")
    waiting = _item()
    still = _item(rejected_values=(_RECEIPTS, _BOOKS))
    unsure = _item(reason=ReviewReason.LOW_CONFIDENCE, rejected_values=())
    closed = _item(status=ReviewStatus.RESOLVED)
    assert now_acceptable([waiting, still, unsure, closed], grown) == [waiting]
    assert now_acceptable([waiting], SEED_TAXONOMY) == []


def test_a_person_decided_the_content_when_it_was_resolved_or_excluded() -> None:
    """Human-review facts or an exclusion; an automatic classification is not one."""
    content = ContentObjectId(uuid4())

    def recorded(tier: ClassificationTier) -> ClassificationResult:
        """A classification at `tier`."""
        return ClassificationResult(
            content_object_id=content, facts=_facts(), tier=tier, confidence=1.0, produced_at=_NOW
        )

    assert decided_by_person(recorded(ClassificationTier.HUMAN_REVIEW), excluded=False)
    assert decided_by_person(None, excluded=True)
    assert not decided_by_person(recorded(ClassificationTier.REMOTE_MODEL), excluded=False)
    assert not decided_by_person(None, excluded=False)


def test_a_result_set_aside_is_audited_with_what_it_proposed() -> None:
    """Rejected, saying why, with the tier and the facts it would have recorded."""
    content = ContentObjectId(uuid4())
    audit = set_aside_audit(
        content, ClassificationTier.REMOTE_MODEL, _facts(subcategory="Receipts"), now=_NOW
    )
    assert audit.entry_type is AuditEntryType.CLASSIFICATION_REJECTED
    assert audit.decided_by == "classification:remote_model"
    assert audit.details["set_aside"] == "a person decided this content first"
    assert audit.details["subcategory"] == "Receipts"
