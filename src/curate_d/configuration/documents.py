"""Parsing stored or submitted configuration documents into their models.

Documents travel as JSON, so values arrive in their JSON forms (lists for tuples, strings
for enums) and are parsed leniently in type but strictly in shape: an unknown field is
still refused.
"""

from collections.abc import Mapping

from curate_d.domain import PlacementPolicy, Taxonomy


def parse_taxonomy(document: Mapping[str, object]) -> Taxonomy:
    """Parse a taxonomy document.

    Raises:
        ValidationError: the document is not a valid taxonomy.
    """
    return Taxonomy.model_validate(dict(document), strict=False)


def parse_policy(document: Mapping[str, object]) -> PlacementPolicy:
    """Parse a placement policy document.

    Raises:
        ValidationError: the document is not a valid policy.
    """
    return PlacementPolicy.model_validate(dict(document), strict=False)
