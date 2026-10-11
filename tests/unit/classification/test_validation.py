"""Facts are held to their schema, and closed-set values to the taxonomy."""

import pytest
from pydantic import ValidationError

from curate_d.classification.validation import validate_facts
from curate_d.configuration.documents import parse_taxonomy
from curate_d.configuration.store import ConfigurationError, check_document
from curate_d.domain import (
    SEED_TAXONOMY,
    ConfigurationKind,
    MediaKind,
    Taxonomy,
    TaxonomyField,
)


def test_schema_conforming_facts_in_the_taxonomy_are_accepted() -> None:
    """JSON-shaped values are coerced and every closed-set value is known."""
    result = validate_facts(
        {
            "media_kind": "document",
            "category": "Documents",
            "subcategory": "Finance",
            "captured_at": "2026-03-01T10:00:00+00:00",
            "title": "Electricity bill",
        },
        SEED_TAXONOMY,
    )
    assert result.accepted
    assert result.facts is not None
    assert result.facts.media_kind is MediaKind.DOCUMENT


def test_a_field_outside_the_schema_is_an_error() -> None:
    """An invented field makes the whole proposal unusable."""
    result = validate_facts(
        {"media_kind": "document", "destination_path": "/Finance/bill.pdf"}, SEED_TAXONOMY
    )
    assert not result.accepted
    assert result.facts is None
    assert any("destination_path" in error for error in result.errors)


def test_a_value_of_the_wrong_type_is_an_error() -> None:
    """A confidence-shaped string where a timestamp belongs is refused."""
    result = validate_facts({"media_kind": "photo", "captured_at": "yesterday"}, SEED_TAXONOMY)
    assert result.facts is None
    assert any(error.startswith("captured_at") for error in result.errors)


def test_a_proposal_that_is_not_an_object_is_an_error() -> None:
    """A list or a string is not a set of facts."""
    assert validate_facts(["photo"], SEED_TAXONOMY).errors


def test_a_category_outside_the_taxonomy_is_rejected_and_recorded() -> None:
    """The facts parse, but the value is refused and kept for review."""
    result = validate_facts({"media_kind": "document", "category": "Receipts"}, SEED_TAXONOMY)
    assert not result.accepted
    assert result.facts is not None
    [rejected] = result.rejected
    assert (rejected.field, rejected.value) == (TaxonomyField.CATEGORY, "Receipts")


def test_a_subcategory_is_judged_within_its_category() -> None:
    """A valid subcategory under the wrong category is still rejected, with that category."""
    result = validate_facts(
        {"media_kind": "document", "category": "Audio", "subcategory": "Finance"}, SEED_TAXONOMY
    )
    [rejected] = result.rejected
    assert rejected.field is TaxonomyField.SUBCATEGORY
    assert rejected.category == "Audio"


def test_an_unknown_place_label_is_rejected() -> None:
    """Place labels are closed too."""
    result = validate_facts({"media_kind": "photo", "place_label": "Paris"}, SEED_TAXONOMY)
    assert [r.field for r in result.rejected] == [TaxonomyField.PLACE_LABEL]


def test_promotion_makes_a_rejected_value_acceptable() -> None:
    """Promoting a subcategory under its category lets the same facts through."""
    proposal = {"media_kind": "document", "category": "Documents", "subcategory": "Receipts"}
    assert not validate_facts(proposal, SEED_TAXONOMY).accepted
    promoted = SEED_TAXONOMY.promote(TaxonomyField.SUBCATEGORY, "Receipts", category="Documents")
    assert validate_facts(proposal, promoted).accepted


def test_promotion_is_idempotent_and_keeps_existing_values() -> None:
    """Promoting a known value changes nothing; others are kept."""
    once = SEED_TAXONOMY.promote(TaxonomyField.PLACE_LABEL, "Paris")
    assert once.promote(TaxonomyField.PLACE_LABEL, "Paris") == once
    assert once.categories == SEED_TAXONOMY.categories
    assert once.promote(TaxonomyField.CATEGORY, "Books").accepts(TaxonomyField.CATEGORY, "Books")


def test_a_subcategory_cannot_be_promoted_without_its_category() -> None:
    """There is nowhere for an orphan subcategory to live."""
    with pytest.raises(ValueError, match="existing category"):
        SEED_TAXONOMY.promote(TaxonomyField.SUBCATEGORY, "Receipts", category="Nope")


@pytest.mark.parametrize(
    "document",
    [
        {"categories": {"": []}},
        {"categories": {"A/B": []}},
        {"categories": {" Padded": []}},
        {"categories": {"A": ["x", "x"]}},
        {"categories": {}, "place_labels": ["Home", "Home"]},
        {"categories": {}, "colours": []},
        {"categories": ["A"]},
    ],
)
def test_a_malformed_taxonomy_document_is_rejected(document: dict[str, object]) -> None:
    """Configuration is validated before it can take effect."""
    with pytest.raises((ValidationError, ValueError)):
        parse_taxonomy(document)
    with pytest.raises(ConfigurationError) as caught:
        check_document(ConfigurationKind.TAXONOMY, document, parse_taxonomy)
    assert caught.value.problems


def test_a_taxonomy_document_round_trips() -> None:
    """The seed stored as JSON parses back to itself."""
    assert parse_taxonomy(SEED_TAXONOMY.model_dump(mode="json")) == SEED_TAXONOMY
    assert isinstance(parse_taxonomy({"categories": {"A": ["b"]}}), Taxonomy)
