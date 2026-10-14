"""Parsing stored or submitted configuration documents into their models.

Documents travel as JSON, so values arrive in their JSON forms (lists for tuples, strings
for enums) and are parsed leniently in type but strictly in shape: an unknown field is
still refused.

A policy can also be parsed against a taxonomy, which activation does: every category and
subcategory condition must then be one the taxonomy lists. Reads parse without one, so a
stored policy stays readable whatever the taxonomy becomes.
"""

from collections.abc import Mapping

from curate_d.domain import PlacementPolicy, Taxonomy


def parse_taxonomy(document: Mapping[str, object]) -> Taxonomy:
    """Parse a taxonomy document.

    Raises:
        ValidationError: the document is not a valid taxonomy.
    """
    return Taxonomy.model_validate(dict(document), strict=False)


def parse_policy(
    document: Mapping[str, object], *, taxonomy: Taxonomy | None = None
) -> PlacementPolicy:
    """Parse a placement policy document.

    Args:
        document: The document, in its JSON form.
        taxonomy: The taxonomy in force, when the policy is being activated: every
            category and subcategory condition must be one it lists. Reads pass none.

    Raises:
        ValidationError: the document is not a valid policy.
    """
    return PlacementPolicy.model_validate(
        dict(document), strict=False, context={"taxonomy": taxonomy}
    )
