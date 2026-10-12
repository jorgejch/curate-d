"""The active taxonomy, and growing it from what the pipeline rejected.

The seed taxonomy is deliberately small; the working one is derived from a dry run. The
procedure: run a dry run over the library, look at the values the tiers proposed and the
taxonomy rejected, promote the ones that recur, and run again. Promotion adds a new
taxonomy version; content classified afterwards is held to it, and review items rejected
only for a now-promoted value can be resolved with it.
"""

from collections.abc import Sequence
from typing import Final

from pydantic import Field

from curate_d.classification.store import RejectedValueCount
from curate_d.configuration.documents import parse_taxonomy
from curate_d.configuration.store import ConfigurationRepository
from curate_d.domain import (
    SEED_TAXONOMY,
    ConfigurationKind,
    DomainModel,
    RejectedValue,
    Taxonomy,
    TaxonomyField,
)

SEEDED_BY: Final = "seed"


class ActiveTaxonomy(DomainModel):
    """The taxonomy in force and its version."""

    version: int = Field(ge=1)
    taxonomy: Taxonomy


async def active_taxonomy(configurations: ConfigurationRepository) -> ActiveTaxonomy:
    """The active taxonomy, storing the seed as version 1 if none was ever stored."""
    version = await configurations.ensure(
        ConfigurationKind.TAXONOMY, SEED_TAXONOMY, parse_taxonomy, created_by=SEEDED_BY
    )
    stored = await configurations.active(ConfigurationKind.TAXONOMY)
    assert stored is not None
    return ActiveTaxonomy(version=version, taxonomy=parse_taxonomy(stored[1]))


def promote_all(taxonomy: Taxonomy, values: Sequence[RejectedValue]) -> Taxonomy:
    """`taxonomy` extended with `values`, categories before the subcategories under them.

    Raises:
        ValueError: a subcategory's category is neither in the taxonomy nor promoted.
    """
    ordered = sorted(values, key=lambda v: v.field is not TaxonomyField.CATEGORY)
    for value in ordered:
        taxonomy = taxonomy.promote(value.field, value.value, category=value.category)
    return taxonomy


def recurring(counts: Sequence[RejectedValueCount], *, min_occurrences: int) -> list[RejectedValue]:
    """The rejected values seen at least `min_occurrences` times: promotion candidates."""
    return [
        RejectedValue(field=count.field, value=count.value, category=count.category)
        for count in counts
        if count.occurrences >= min_occurrences
    ]


async def promote(
    configurations: ConfigurationRepository,
    values: Sequence[RejectedValue],
    *,
    promoted_by: str,
) -> ActiveTaxonomy:
    """Promote values into the taxonomy as a new active version.

    The taxonomy's lock is taken before the version in force is read, so a taxonomy
    activated meanwhile is extended rather than overwritten: promotion only ever adds
    values, and so never orphans a policy or template condition unreported.

    Raises:
        ConfigurationError: the promoted taxonomy is invalid; the current one stays.
        ValueError: a subcategory is promoted without its category.
    """
    await configurations.lock(ConfigurationKind.TAXONOMY)
    current = await active_taxonomy(configurations)
    extended = promote_all(current.taxonomy, values)
    if extended == current.taxonomy:
        return current
    version = await configurations.activate(
        ConfigurationKind.TAXONOMY,
        extended.model_dump(mode="json"),
        parse_taxonomy,
        created_by=promoted_by,
    )
    return ActiveTaxonomy(version=version, taxonomy=extended)
