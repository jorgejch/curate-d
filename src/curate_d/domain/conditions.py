"""Conditions: the `when` clauses that select content for rules and templates.

A condition maps a fact to the value it must equal. Only `media_kind`, `category` and
`subcategory` can select, and matching is exact. Two checks keep a condition from being
one that can never match:

- `check_conditions` is static. A `media_kind` value must be a `MediaKind`, and a
  category or subcategory value must be well-formed. It runs whenever a policy or
  template set is parsed, stored or submitted.
- `orphaned` depends on the taxonomy, which is versioned on its own. It runs when a
  policy or template set is activated, against the taxonomy in force, and when a
  taxonomy is activated, to report the conditions it leaves without a value to match.
  Reading a stored document never runs it, so a taxonomy change cannot make a stored
  policy unreadable.
"""

from collections.abc import Mapping
from typing import Final

from curate_d.domain.facts import Facts, MediaKind
from curate_d.domain.taxonomy import Taxonomy, TaxonomyField, check_value

CONDITION_FIELDS: Final = frozenset({"media_kind", "category", "subcategory"})
_MEDIA_KINDS: Final = sorted(kind.value for kind in MediaKind)


def matches(when: Mapping[str, str], facts: Facts) -> bool:
    """Return whether every condition in `when` matches the facts."""
    values = facts.model_dump(mode="json")
    return all(values.get(field) == value for field, value in when.items())


def check_conditions(
    owner: str, when: Mapping[str, str], *, taxonomy: Taxonomy | None = None
) -> None:
    """Reject a condition that cannot match.

    Args:
        owner: What the conditions belong to, for the message ("placement rule 'x'").
        when: The conditions.
        taxonomy: The taxonomy in force, when a document is being activated. Without
            one, category and subcategory values are checked for form only.

    Raises:
        ValueError: a condition names a fact that cannot select, a `media_kind` value
            that is not a `MediaKind`, a malformed category or subcategory value, or one
            `taxonomy` does not list.
    """
    unknown = set(when) - CONDITION_FIELDS
    if unknown:
        raise ValueError(
            f"{owner} selects on {sorted(unknown)}; only {sorted(CONDITION_FIELDS)} can select"
        )
    media_kind = when.get("media_kind")
    if media_kind is not None and media_kind not in _MEDIA_KINDS:
        raise ValueError(f"{owner} selects media_kind {media_kind!r}; one of {_MEDIA_KINDS}")
    for field in (TaxonomyField.CATEGORY, TaxonomyField.SUBCATEGORY):
        value = when.get(field.value)
        if value is not None:
            try:
                check_value(value)
            except ValueError as malformed:
                raise ValueError(f"{owner} selects {field.value} {value!r}: {malformed}") from None
    if taxonomy is not None:
        unlisted = [f"{field.value} {when[field.value]!r}" for field in orphaned(when, taxonomy)]
        if unlisted:
            raise ValueError(
                f"{owner} selects {' and '.join(unlisted)}, which the taxonomy in force "
                "does not list"
            )


def orphaned(when: Mapping[str, str], taxonomy: Taxonomy) -> list[TaxonomyField]:
    """Return the taxonomy fields whose condition values `taxonomy` does not list.

    A category must be listed. A subcategory must be listed under the condition's
    category, or under some category when the condition names none. A subcategory under
    an unlisted category is not reported on its own: the category already is.
    """
    found: list[TaxonomyField] = []
    category = when.get(TaxonomyField.CATEGORY.value)
    subcategory = when.get(TaxonomyField.SUBCATEGORY.value)
    if category is not None and not taxonomy.accepts(TaxonomyField.CATEGORY, category):
        found.append(TaxonomyField.CATEGORY)
    elif subcategory is not None:
        listed = (
            taxonomy.accepts(TaxonomyField.SUBCATEGORY, subcategory, category=category)
            if category is not None
            else any(subcategory in subs for subs in taxonomy.categories.values())
        )
        if not listed:
            found.append(TaxonomyField.SUBCATEGORY)
    return found


def taxonomy_in(context: object) -> Taxonomy | None:
    """Return the taxonomy a validation context carries, or None.

    Parsers pass `{"taxonomy": taxonomy}` as the pydantic validation context when a
    document is being activated; reads pass none.
    """
    if isinstance(context, Mapping):
        taxonomy = context.get("taxonomy")
        if isinstance(taxonomy, Taxonomy):
            return taxonomy
    return None
