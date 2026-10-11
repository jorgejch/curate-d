"""Checking a proposed classification against the facts schema and the taxonomy.

Two different refusals, kept apart because they lead to different places. A field the
schema does not define, or a value of the wrong type, is an error: the output is unusable
and nothing in it is trusted. A closed-set value the taxonomy does not contain is a
rejected value: the rest of the facts may be fine, and the value itself is recorded so a
person can decide whether the taxonomy should grow to include it.
"""

from collections.abc import Mapping

from pydantic import Field, ValidationError

from curate_d.domain import DomainModel, Facts, RejectedValue, Taxonomy, TaxonomyField


class FactsValidation(DomainModel):
    """A validated proposal: facts, or why none could be accepted."""

    facts: Facts | None = None
    rejected: tuple[RejectedValue, ...] = ()
    errors: tuple[str, ...] = Field(default=(), description="Schema violations, one per line.")

    @property
    def accepted(self) -> bool:
        """Whether the facts may be recorded as they are."""
        return self.facts is not None and not self.rejected and not self.errors


def taxonomy_rejections(facts: Facts, taxonomy: Taxonomy) -> tuple[RejectedValue, ...]:
    """The closed-set values in `facts` that `taxonomy` does not contain.

    A subcategory is judged within its category, so a subcategory offered without one,
    or under a rejected one, is itself rejected.
    """
    rejected: list[RejectedValue] = []
    if facts.category is not None and not taxonomy.accepts(TaxonomyField.CATEGORY, facts.category):
        rejected.append(RejectedValue(field=TaxonomyField.CATEGORY, value=facts.category))
    if facts.subcategory is not None and not taxonomy.accepts(
        TaxonomyField.SUBCATEGORY, facts.subcategory, category=facts.category
    ):
        rejected.append(
            RejectedValue(
                field=TaxonomyField.SUBCATEGORY,
                value=facts.subcategory,
                category=facts.category,
            )
        )
    if facts.place_label is not None and not taxonomy.accepts(
        TaxonomyField.PLACE_LABEL, facts.place_label
    ):
        rejected.append(RejectedValue(field=TaxonomyField.PLACE_LABEL, value=facts.place_label))
    return tuple(rejected)


def validate_facts(raw: object, taxonomy: Taxonomy) -> FactsValidation:
    """Validate an untrusted proposal, such as a model's JSON answer.

    Values are coerced from their JSON forms (an ISO timestamp string, an enum's value);
    anything beyond that is refused.
    """
    if not isinstance(raw, Mapping):
        return FactsValidation(errors=(f"facts must be an object, got {type(raw).__name__}",))
    try:
        facts = Facts.model_validate(dict(raw), strict=False)
    except ValidationError as invalid:
        return FactsValidation(
            errors=tuple(
                f"{'.'.join(str(part) for part in error['loc']) or '(root)'}: {error['msg']}"
                for error in invalid.errors()
            )
        )
    return FactsValidation(facts=facts, rejected=taxonomy_rejections(facts, taxonomy))
