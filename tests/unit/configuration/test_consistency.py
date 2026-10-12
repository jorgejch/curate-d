"""A taxonomy change reports the policy and template conditions it leaves unmatched."""

from curate_d.configuration.consistency import OrphanedCondition, orphans
from curate_d.configuration.documents import parse_policy
from curate_d.domain import (
    SEED_POLICY,
    SEED_TAXONOMY,
    ConfigurationKind,
    Taxonomy,
    TaxonomyField,
)
from curate_d.rendering.templates import SEED_TEMPLATES

_POLICY = parse_policy(
    {
        "destinations": [
            {"name": "archive", "provider": "pcloud", "account": "1", "root_path": "/"}
        ],
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
)


def test_the_seeds_orphan_nothing() -> None:
    """The seed policy and templates name only values the seed taxonomy lists."""
    assert orphans(SEED_TAXONOMY, SEED_POLICY, SEED_TEMPLATES) == []
    assert orphans(SEED_TAXONOMY, _POLICY, SEED_TEMPLATES) == []


def test_a_shrunken_taxonomy_orphans_rules_and_templates_that_name_what_it_dropped() -> None:
    """Placement and retention rules and templates are each reported, with kind and owner."""
    shrunken = Taxonomy(categories={"Photos": ("Camera",), "Audio": ("Recordings",)})

    found = orphans(shrunken, _POLICY, SEED_TEMPLATES)

    def orphan(kind: ConfigurationKind, owner: str, field: TaxonomyField, value: str) -> object:
        """Return the expected report for one condition."""
        return OrphanedCondition(kind=kind, owner=owner, field=field, value=value)

    policy, templates = ConfigurationKind.POLICY, ConfigurationKind.TEMPLATES
    category, subcategory = TaxonomyField.CATEGORY, TaxonomyField.SUBCATEGORY
    assert found == [
        orphan(policy, "placement rule 'finance'", category, "Documents"),
        orphan(policy, "placement rule 'music'", subcategory, "Music"),
        orphan(policy, "retention rule 'keep-docs'", category, "Documents"),
        orphan(templates, "template 'documents'", category, "Documents"),
    ]
