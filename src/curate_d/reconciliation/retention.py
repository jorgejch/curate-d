"""Whether a content object's originals may be released: a policy outcome, never a default.

The original is retained unless a retention rule explicitly permits release, and even
then only while enough copies at one of the two strong verification rungs exist without
it. When the rule counts the original as one of the required copies, the original is
needed until the placements alone meet the requirement. Weaker copies never count. The
decision is re-derived from the placements as they stand on every evaluation, so a copy
whose level is later found weaker changes it.

Nothing here releases anything: the decision is recorded, with its inputs and rule.
"""

from collections.abc import Sequence
from enum import StrEnum

from pydantic import Field

from curate_d.domain import (
    AuditValue,
    DomainModel,
    Placement,
    PlacementState,
    RetentionRule,
    VerificationLevel,
)


class RetentionOutcome(StrEnum):
    """What may happen to a content object's originals."""

    RETAIN = "retain"
    RELEASE_PERMITTED = "release_permitted"


class RetentionDecision(DomainModel):
    """A retention outcome, why, and what it was decided from."""

    outcome: RetentionOutcome
    reason: str = Field(min_length=1)
    rule: str | None = None
    required_copies: int | None = None
    source_counts_as_copy: bool | None = None
    minimum_level: VerificationLevel | None = None
    strong_copies: tuple[str, ...] = Field(
        default=(), description="Destinations holding a copy that counts."
    )
    weak_copies: tuple[str, ...] = Field(
        default=(), description="Destinations holding a present copy too weakly verified to count."
    )

    def audit_details(self) -> dict[str, AuditValue]:
        """The decision as audit entry details."""
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "rule": self.rule,
            "required_copies": self.required_copies,
            "source_counts_as_copy": self.source_counts_as_copy,
            "minimum_level": None
            if self.minimum_level is None
            else self.minimum_level.name.lower(),
            "strong_copies": ", ".join(self.strong_copies) or None,
            "weak_copies": ", ".join(self.weak_copies) or None,
        }


def decide_retention(
    rule: RetentionRule | None, placements: Sequence[Placement]
) -> RetentionDecision:
    """Decide retention for one content object from its rule and its placements."""
    if rule is None:
        return RetentionDecision(
            outcome=RetentionOutcome.RETAIN,
            reason="no retention rule applies; originals are retained by default",
        )
    base = RetentionDecision(
        outcome=RetentionOutcome.RETAIN,
        reason=f"rule {rule.name!r} retains originals",
        rule=rule.name,
        required_copies=rule.required_copies,
        source_counts_as_copy=rule.source_counts_as_copy,
        minimum_level=rule.minimum_level,
    )
    if not rule.release_source:
        return base
    present = [p for p in placements if p.state is PlacementState.PRESENT]
    counts = [
        p
        for p in present
        if p.verification_level.permits_source_release
        and p.verification_level >= rule.minimum_level
    ]
    strong = tuple(sorted(p.destination_name for p in counts))
    weak = tuple(sorted(p.destination_name for p in present if p not in counts))
    decided = base.model_copy(update={"strong_copies": strong, "weak_copies": weak})
    if len(strong) >= rule.required_copies:
        return decided.model_copy(
            update={
                "outcome": RetentionOutcome.RELEASE_PERMITTED,
                "reason": f"{len(strong)} strongly verified copies meet the "
                f"{rule.required_copies} required without the original",
            }
        )
    reason = f"{len(strong)} of {rule.required_copies} required copies are strongly verified"
    if rule.source_counts_as_copy and len(strong) + 1 >= rule.required_copies:
        reason = (
            f"the original is one of the {rule.required_copies} required copies; releasing it "
            f"would leave {len(strong)}"
        )
    if weak:
        reason += f"; {len(weak)} weaker copies do not count ({', '.join(weak)})"
    return decided.model_copy(update={"reason": reason})
