"""Consistency between the taxonomy and the conditions that select on its values.

Placement rules, retention rules and templates select content by category and
subcategory, and the taxonomy lists the values those can take. The two are versioned
apart, so each side is checked when it changes:

- A policy or template set is activated against the taxonomy in force, and refused if a
  condition names a value it does not list. Activation reads that taxonomy with
  `taxonomy_in_force` while holding the taxonomy's lock in shared mode. A dry run's
  drafts pass the same check, `check_against`, when the run is requested and when it
  opens.
- A taxonomy is never refused for the conditions it leaves without a value to match:
  removing a value in use would otherwise take three steps. Its activation reports them
  instead (`orphans_in_force`).

A kind never stored is read as its seed, which is what would be put in force on first
use; nothing here stores one. Stored versions are read with the read parsers, so a stored
policy's retired fields are dropped and no taxonomy is checked; a version in force that
still does not read raises a `ConfigurationError` naming its kind.
"""

from collections.abc import Mapping
from functools import partial
from typing import Final

from pydantic import BaseModel, Field

from curate_d.configuration.documents import parse_policy, parse_stored_policy, parse_taxonomy
from curate_d.configuration.store import (
    ConfigurationRepository,
    DocumentValidator,
    check_document,
)
from curate_d.domain import (
    SEED_POLICY,
    SEED_TAXONOMY,
    ConfigurationKind,
    DomainModel,
    PlacementPolicy,
    Taxonomy,
    TaxonomyField,
    orphaned,
)
from curate_d.rendering.templates import SEED_TEMPLATES, TemplateSet, parse_templates

_SUBMISSION_PARSERS: Final[Mapping[ConfigurationKind, DocumentValidator]] = {
    ConfigurationKind.TAXONOMY: parse_taxonomy,
    ConfigurationKind.TEMPLATES: parse_templates,
    ConfigurationKind.POLICY: parse_policy,
}


def submission_parser(kind: ConfigurationKind, taxonomy: Taxonomy | None) -> DocumentValidator:
    """Return the parser activating `kind` validates with, against `taxonomy` when given."""
    if taxonomy is None or kind is ConfigurationKind.TAXONOMY:
        return _SUBMISSION_PARSERS[kind]
    if kind is ConfigurationKind.POLICY:
        return partial(parse_policy, taxonomy=taxonomy)
    return partial(parse_templates, taxonomy=taxonomy)


def check_against(
    kind: ConfigurationKind, document: BaseModel, *, taxonomy: Taxonomy | None
) -> BaseModel:
    """Validate a submitted document as activating it would, storing nothing.

    Args:
        kind: The kind `document` is submitted as.
        document: The submitted document, already parsed.
        taxonomy: The taxonomy in force, read by the caller under the taxonomy's shared
            lock: every category and subcategory condition of a policy or template set
            must be one it lists. Ignored for a taxonomy.

    Returns:
        The document as parsed from its stored (JSON) form.

    Raises:
        ConfigurationError: the document is invalid for `kind`, or names a category or
            subcategory `taxonomy` does not list.
    """
    return check_document(kind, document.model_dump(mode="json"), submission_parser(kind, taxonomy))


class OrphanedCondition(DomainModel):
    """A condition whose value the taxonomy no longer lists, so it matches nothing."""

    kind: ConfigurationKind = Field(description="The document holding the condition.")
    owner: str = Field(description="The rule or template, e.g. \"placement rule 'docs'\".")
    field: TaxonomyField
    value: str


def orphans(
    taxonomy: Taxonomy, policy: PlacementPolicy, templates: TemplateSet
) -> list[OrphanedCondition]:
    """Return every policy and template condition `taxonomy` does not list a value for.

    Placement rules come first, then retention rules, then templates, each in document
    order.
    """
    owners = [
        *((ConfigurationKind.POLICY, f"placement rule {r.name!r}", r.when) for r in policy.rules),
        *(
            (ConfigurationKind.POLICY, f"retention rule {r.name!r}", r.when)
            for r in policy.retention
        ),
        *(
            (ConfigurationKind.TEMPLATES, f"template {t.name!r}", t.when)
            for t in templates.templates
        ),
    ]
    return [
        OrphanedCondition(kind=kind, owner=owner, field=field, value=when[field.value])
        for kind, owner, when in owners
        for field in orphaned(when, taxonomy)
    ]


async def taxonomy_in_force(configurations: ConfigurationRepository) -> Taxonomy:
    """Return the active taxonomy, or the seed if none was ever stored.

    Raises:
        ConfigurationError: the stored taxonomy in force does not read.
    """
    stored = await configurations.active(ConfigurationKind.TAXONOMY)
    if stored is None:
        return SEED_TAXONOMY
    parsed = check_document(ConfigurationKind.TAXONOMY, stored[1], parse_taxonomy)
    assert isinstance(parsed, Taxonomy)
    return parsed


async def orphans_in_force(
    configurations: ConfigurationRepository, taxonomy: Taxonomy
) -> list[OrphanedCondition]:
    """Return the conditions of the active policy and templates `taxonomy` orphans.

    Raises:
        ConfigurationError: the stored policy or template set in force does not read; it
            names the kind and each problem.
    """
    policy = await configurations.active(ConfigurationKind.POLICY)
    templates = await configurations.active(ConfigurationKind.TEMPLATES)
    stored_policy = (
        SEED_POLICY
        if policy is None
        else check_document(ConfigurationKind.POLICY, policy[1], parse_stored_policy)
    )
    stored_templates = (
        SEED_TEMPLATES
        if templates is None
        else check_document(ConfigurationKind.TEMPLATES, templates[1], parse_templates)
    )
    assert isinstance(stored_policy, PlacementPolicy)
    assert isinstance(stored_templates, TemplateSet)
    return orphans(taxonomy, stored_policy, stored_templates)
