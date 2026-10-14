"""Conditions match facts exactly, and refuse values that could never match."""

import pytest

from curate_d.domain import (
    SEED_TAXONOMY,
    Facts,
    MediaKind,
    TaxonomyField,
    check_conditions,
    matches,
    orphaned,
)


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ({}, True),
        ({"media_kind": "document"}, True),
        ({"media_kind": "photo"}, False),
        ({"category": "Documents", "subcategory": "Finance"}, True),
        ({"category": "Documents", "subcategory": "Home"}, False),
        ({"category": "documents"}, False),
    ],
)
def test_a_condition_matches_only_equal_values(when: dict[str, str], expected: bool) -> None:
    """Every condition must equal its fact; case counts."""
    facts = Facts(media_kind=MediaKind.DOCUMENT, category="Documents", subcategory="Finance")
    assert matches(when, facts) is expected


def test_a_condition_on_an_absent_fact_matches_nothing() -> None:
    """An unset category equals no value."""
    assert not matches({"category": "Documents"}, Facts(media_kind=MediaKind.DOCUMENT))


@pytest.mark.parametrize("kind", list(MediaKind))
def test_every_media_kind_can_select(kind: MediaKind) -> None:
    """Each `MediaKind` value is accepted as a condition."""
    check_conditions("rule 'r'", {"media_kind": kind.value})


@pytest.mark.parametrize(
    ("when", "message"),
    [
        ({"title": "t"}, r"rule 'r' selects on \['title'\]"),
        ({"media_kind": "photos"}, r"rule 'r' selects media_kind 'photos'; one of \['archive'"),
        ({"media_kind": "Photo"}, "media_kind 'Photo'"),
        ({"category": " Photos"}, "category ' Photos'"),
        ({"category": ""}, "category ''"),
        ({"subcategory": "Camera/Raw"}, "subcategory 'Camera/Raw'"),
    ],
)
def test_a_condition_that_cannot_match_is_refused(when: dict[str, str], message: str) -> None:
    """An unknown fact, media kind, or malformed taxonomy value is refused with the owner."""
    with pytest.raises(ValueError, match=message):
        check_conditions("rule 'r'", when)


def test_without_a_taxonomy_an_unlisted_category_passes() -> None:
    """The static check reads no taxonomy."""
    check_conditions("rule 'r'", {"category": "Fotos"})


def test_with_a_taxonomy_an_unlisted_value_is_refused() -> None:
    """The message names the owner, the field and the value."""
    with pytest.raises(
        ValueError,
        match="rule 'r' selects category 'Fotos', which the taxonomy in force does not list",
    ):
        check_conditions("rule 'r'", {"category": "Fotos"}, taxonomy=SEED_TAXONOMY)


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ({}, []),
        ({"media_kind": "photo"}, []),
        ({"category": "Photos"}, []),
        ({"category": "Fotos"}, [TaxonomyField.CATEGORY]),
        ({"category": "Documents", "subcategory": "Finance"}, []),
        ({"category": "Documents", "subcategory": "Receipts"}, [TaxonomyField.SUBCATEGORY]),
        ({"category": "Photos", "subcategory": "Finance"}, [TaxonomyField.SUBCATEGORY]),
        ({"subcategory": "Finance"}, []),
        ({"subcategory": "Receipts"}, [TaxonomyField.SUBCATEGORY]),
        ({"category": "Fotos", "subcategory": "Camera"}, [TaxonomyField.CATEGORY]),
    ],
)
def test_orphaned_reports_values_the_taxonomy_does_not_list(
    when: dict[str, str], expected: list[TaxonomyField]
) -> None:
    """A subcategory is checked under its category, or under any when none is named."""
    assert orphaned(when, SEED_TAXONOMY) == expected
