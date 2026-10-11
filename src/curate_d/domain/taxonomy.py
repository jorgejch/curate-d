"""The closed-set taxonomy classification is held to.

Category, subcategory and place label facts must be values listed here. A value outside
it is rejected rather than accepted, so open-vocabulary output can never produce four
sibling folders that are spelling variants of one category; the rejected value is kept
so a person can decide whether it belongs.
"""

from collections.abc import Mapping
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from curate_d.domain.base import DomainModel


class TaxonomyField(StrEnum):
    """The facts whose values the taxonomy closes."""

    CATEGORY = "category"
    SUBCATEGORY = "subcategory"
    PLACE_LABEL = "place_label"


def _check_value(value: str) -> None:
    """Reject a value that cannot name a folder or that pads itself with whitespace.

    Raises:
        ValueError: the value is empty, padded, or contains a path separator.
    """
    if not value or value != value.strip() or "/" in value or "\\" in value:
        raise ValueError(f"taxonomy value {value!r} must be non-empty, unpadded and slash-free")


class Taxonomy(DomainModel):
    """Categories with their subcategories, and place labels."""

    categories: Mapping[str, tuple[str, ...]] = Field(
        description="Each category with the subcategories allowed under it."
    )
    place_labels: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check_values(self) -> Self:
        """Reject malformed or duplicated values.

        Raises:
            ValueError: a value is malformed, or a subcategory or label is listed twice.
        """
        for category, subcategories in self.categories.items():
            _check_value(category)
            for subcategory in subcategories:
                _check_value(subcategory)
            if len(set(subcategories)) != len(subcategories):
                raise ValueError(f"category {category!r} lists a subcategory twice")
        for label in self.place_labels:
            _check_value(label)
        if len(set(self.place_labels)) != len(self.place_labels):
            raise ValueError("a place label is listed twice")
        return self

    def accepts(self, field: TaxonomyField, value: str, *, category: str | None = None) -> bool:
        """Whether `value` is allowed for `field`; subcategories within `category`."""
        if field is TaxonomyField.CATEGORY:
            return value in self.categories
        if field is TaxonomyField.SUBCATEGORY:
            return category is not None and value in self.categories.get(category, ())
        return value in self.place_labels

    def promote(
        self, field: TaxonomyField, value: str, *, category: str | None = None
    ) -> "Taxonomy":
        """A taxonomy that also accepts `value` for `field`.

        Raises:
            ValueError: a subcategory is promoted without an existing category, or the
                value is malformed.
        """
        _check_value(value)
        categories = {name: tuple(subs) for name, subs in self.categories.items()}
        labels = self.place_labels
        if field is TaxonomyField.CATEGORY:
            categories.setdefault(value, ())
        elif field is TaxonomyField.SUBCATEGORY:
            if category is None or category not in categories:
                raise ValueError(
                    f"subcategory {value!r} needs an existing category, got {category!r}"
                )
            if value not in categories[category]:
                categories[category] = (*categories[category], value)
        elif value not in labels:
            labels = (*labels, value)
        return Taxonomy(categories=categories, place_labels=labels)


# Deliberately small: the working taxonomy is derived from a dry run over the real
# library (design: "Taxonomy derived from a dry run, not designed up front").
SEED_TAXONOMY = Taxonomy(
    categories={
        "Photos": ("Camera", "Screenshots"),
        "Videos": ("Camera",),
        "Audio": ("Music", "Recordings"),
        "Documents": ("Finance", "Identity", "Medical", "Work", "Home"),
        "Archives": (),
    },
    place_labels=(),
)
