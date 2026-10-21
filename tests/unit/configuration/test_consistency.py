"""A taxonomy change reports the policy and template conditions it leaves unmatched."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

import pytest

from curate_d.configuration.consistency import (
    OrphanedCondition,
    orphans,
    orphans_in_force,
    taxonomy_in_force,
)
from curate_d.configuration.documents import parse_policy
from curate_d.configuration.store import ConfigurationError, ConfigurationRepository
from curate_d.domain import (
    SEED_POLICY,
    SEED_TAXONOMY,
    ConfigurationKind,
    Taxonomy,
    TaxonomyField,
)
from curate_d.rendering.templates import SEED_TEMPLATES

_DOCUMENT: dict[str, object] = {
    "destinations": [{"name": "archive", "provider": "pcloud", "account": "1", "root_path": "/"}],
    "rules": [
        {"name": "photos", "when": {"category": "Photos"}, "destinations": ["archive"]},
        {
            "name": "finance",
            "when": {"category": "Documents", "subcategory": "Finance"},
            "destinations": ["archive"],
        },
        {"name": "music", "when": {"subcategory": "Music"}, "destinations": ["archive"]},
    ],
    "retention": [{"name": "keep-docs", "when": {"category": "Documents"}}],
}
_POLICY = parse_policy(_DOCUMENT)
_SHRUNKEN = Taxonomy(categories={"Photos": ("Camera",), "Audio": ("Recordings",)})


@dataclass
class _Repository:
    """A configuration store holding the given active documents, by kind."""

    stored: dict[ConfigurationKind, Mapping[str, object]] = field(default_factory=dict)

    async def active(self, kind: ConfigurationKind) -> tuple[int, Mapping[str, object]] | None:
        """Return the stored document as version 1, or None if none was stored."""
        document = self.stored.get(kind)
        return None if document is None else (1, document)


def _orphan(
    kind: ConfigurationKind, owner: str, taxonomy_field: TaxonomyField, value: str
) -> OrphanedCondition:
    """Return the expected report for one condition."""
    return OrphanedCondition(kind=kind, owner=owner, field=taxonomy_field, value=value)


_POLICY_ORPHANS = [
    _orphan(
        ConfigurationKind.POLICY, "placement rule 'finance'", TaxonomyField.CATEGORY, "Documents"
    ),
    _orphan(ConfigurationKind.POLICY, "placement rule 'music'", TaxonomyField.SUBCATEGORY, "Music"),
    _orphan(
        ConfigurationKind.POLICY, "retention rule 'keep-docs'", TaxonomyField.CATEGORY, "Documents"
    ),
]
_TEMPLATE_ORPHANS = [
    _orphan(
        ConfigurationKind.TEMPLATES, "template 'documents'", TaxonomyField.CATEGORY, "Documents"
    ),
]


def test_the_seeds_orphan_nothing() -> None:
    """The seed policy and templates name only values the seed taxonomy lists."""
    assert orphans(SEED_TAXONOMY, SEED_POLICY, SEED_TEMPLATES) == []
    assert orphans(SEED_TAXONOMY, _POLICY, SEED_TEMPLATES) == []


def test_a_shrunken_taxonomy_orphans_rules_and_templates_that_name_what_it_dropped() -> None:
    """Placement and retention rules and templates are each reported, with kind and owner."""
    assert orphans(_SHRUNKEN, _POLICY, SEED_TEMPLATES) == _POLICY_ORPHANS + _TEMPLATE_ORPHANS


async def test_orphans_in_force_read_the_stored_policy_and_templates() -> None:
    """The active policy is read from the store; templates never stored read as the seed."""
    repository = _Repository({ConfigurationKind.POLICY: _DOCUMENT})

    found = await orphans_in_force(cast(ConfigurationRepository, repository), _SHRUNKEN)

    assert found == _POLICY_ORPHANS + _TEMPLATE_ORPHANS


async def test_orphans_in_force_read_a_stored_policy_carrying_a_retired_field() -> None:
    """A version stored with `source_counts_as_copy` still reads, without the field."""
    retention = [
        {"name": "keep-docs", "when": {"category": "Documents"}, "source_counts_as_copy": True}
    ]
    stored = _DOCUMENT | {"retention": retention}
    repository = _Repository({ConfigurationKind.POLICY: stored})

    found = await orphans_in_force(cast(ConfigurationRepository, repository), _SHRUNKEN)

    assert found == _POLICY_ORPHANS + _TEMPLATE_ORPHANS


async def test_nothing_stored_reads_as_the_seeds() -> None:
    """With no version stored, the seed taxonomy is in force and the seeds orphan nothing."""
    repository = cast(ConfigurationRepository, _Repository())

    assert await taxonomy_in_force(repository) == SEED_TAXONOMY
    assert await orphans_in_force(repository, SEED_TAXONOMY) == []


async def test_the_stored_taxonomy_is_the_one_in_force() -> None:
    """A stored taxonomy replaces the seed."""
    repository = _Repository({ConfigurationKind.TAXONOMY: _SHRUNKEN.model_dump(mode="json")})

    assert await taxonomy_in_force(cast(ConfigurationRepository, repository)) == _SHRUNKEN


@pytest.mark.parametrize(
    ("kind", "document"),
    [
        (
            ConfigurationKind.POLICY,
            {"destinations": [], "rules": [], "retention": [{"name": "x", "when": {"mood": "a"}}]},
        ),
        (ConfigurationKind.TEMPLATES, {"templates": [{"name": "x", "when": {"media_kind": "x"}}]}),
    ],
)
async def test_an_unreadable_version_in_force_is_reported_by_its_kind(
    kind: ConfigurationKind, document: Mapping[str, object]
) -> None:
    """The orphans cannot be computed, and the error names the kind that does not read."""
    repository = cast(ConfigurationRepository, _Repository({kind: document}))

    with pytest.raises(ConfigurationError, match=f"invalid {kind.value} configuration") as raised:
        await orphans_in_force(repository, SEED_TAXONOMY)

    assert raised.value.kind is kind
