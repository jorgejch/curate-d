"""Parsing stored or submitted configuration documents into their models.

Documents travel as JSON, so values arrive in their JSON forms (lists for tuples, strings
for enums) and are parsed leniently in type but strictly in shape: an unknown field is
still refused. The one exception is a field a stored version may carry from before it was
retired: stored documents are never edited in place, so their parser drops it.
"""

from collections.abc import Mapping
from typing import Final

from curate_d.domain import PlacementPolicy, Taxonomy

# Retention rule fields stored policy versions may still carry. `source_counts_as_copy`
# changed no outcome for either value: release always needed the required copies without
# the original.
_RETIRED_RETENTION_FIELDS: Final = frozenset({"source_counts_as_copy"})


def parse_taxonomy(document: Mapping[str, object]) -> Taxonomy:
    """Parse a taxonomy document.

    Raises:
        ValidationError: the document is not a valid taxonomy.
    """
    return Taxonomy.model_validate(dict(document), strict=False)


def parse_policy(document: Mapping[str, object]) -> PlacementPolicy:
    """Parse a submitted placement policy document.

    Raises:
        ValidationError: the document is not a valid policy, or carries a retired field.
    """
    return PlacementPolicy.model_validate(dict(document), strict=False)


def parse_stored_policy(document: Mapping[str, object]) -> PlacementPolicy:
    """Parse a stored placement policy version, dropping retired retention rule fields.

    Versions are never edited in place, so one stored before a field was retired keeps it;
    the field had no effect, and the version means the same without it.

    Raises:
        ValidationError: the document is not a valid policy.
    """
    current = dict(document)
    retention = current.get("retention")
    if isinstance(retention, list):
        current["retention"] = [
            {key: value for key, value in rule.items() if key not in _RETIRED_RETENTION_FIELDS}
            if isinstance(rule, Mapping)
            else rule
            for rule in retention
        ]
    return parse_policy(current)
