"""Whether a content object's originals may be released: a policy outcome, never a default.

The original is retained unless a retention rule explicitly permits release, and even
then only while the rule's required copies exist without it, each at one of the two strong
verification rungs. The original never counts toward them. Weaker copies never count. The
decision is re-derived from the placements as they stand on every evaluation, so a copy
whose level is later found weaker changes it.

Only copies at destination identities the policy in force defines count, because only
those are checked: a copy at a destination dropped from the policy, or left behind when the
policy pointed its name at another account or root, is never verified again, so it may be
gone without the ledger knowing. Such copies are named in the decision instead, by the
name they were placed under and their location. A counted copy is named by the policy's
current name for its identity, so a renamed destination reads under its new name.

Nothing here releases anything: the decision is recorded, with its inputs and rule.
"""

from collections.abc import Mapping, Sequence
from enum import StrEnum

from pydantic import Field

from curate_d.domain import (
    AuditValue,
    DestinationId,
    DestinationIdentity,
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
    minimum_level: VerificationLevel | None = None
    strong_copies: tuple[str, ...] = Field(
        default=(), description="Destinations holding a copy that counts."
    )
    weak_copies: tuple[str, ...] = Field(
        default=(), description="Destinations holding a present copy too weakly verified to count."
    )
    unchecked_copies: tuple[str, ...] = Field(
        default=(),
        description="Present copies at destination identities the policy no longer defines, "
        "each as the name it was placed under and its location; nothing checks them, so "
        "they do not count.",
    )

    def audit_details(self, *, policy_version: int | None = None) -> dict[str, AuditValue]:
        """Return the decision as audit entry details, with the policy version it applied."""
        return {
            "policy_version": policy_version,
            "outcome": self.outcome.value,
            "reason": self.reason,
            "rule": self.rule,
            "required_copies": self.required_copies,
            "minimum_level": None
            if self.minimum_level is None
            else self.minimum_level.name.lower(),
            "strong_copies": ", ".join(self.strong_copies) or None,
            "weak_copies": ", ".join(self.weak_copies) or None,
            "unchecked_copies": ", ".join(self.unchecked_copies) or None,
        }


def decide_retention(
    rule: RetentionRule | None,
    placements: Sequence[Placement],
    *,
    destinations: Mapping[DestinationId, str],
    locations: Mapping[DestinationId, DestinationIdentity] | None = None,
) -> RetentionDecision:
    """Decide retention for one content object from its rule and its placements.

    Args:
        rule: The retention rule governing the content, or None.
        placements: The content's placements, at any destination.
        destinations: The destination identities the policy in force defines, each mapped
            to its name there. Present copies elsewhere do not count; they are listed in
            `unchecked_copies`.
        locations: Where the placements at identities outside `destinations` are, to name
            them by; one missing is named by its identifier.
    """
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
        minimum_level=rule.minimum_level,
    )
    if not rule.release_source:
        return base
    present = [p for p in placements if p.state is PlacementState.PRESENT]
    checked = [p for p in present if p.destination_id in destinations]
    unchecked = tuple(
        sorted(
            _located(p, locations or {}) for p in present if p.destination_id not in destinations
        )
    )
    counts = [
        p
        for p in checked
        if p.verification_level.permits_source_release
        and p.verification_level >= rule.minimum_level
    ]
    strong = tuple(sorted(destinations[p.destination_id] for p in counts))
    weak = tuple(sorted(destinations[p.destination_id] for p in checked if p not in counts))
    decided = base.model_copy(
        update={"strong_copies": strong, "weak_copies": weak, "unchecked_copies": unchecked}
    )
    dropped = (
        f"; {len(unchecked)} copies at destinations no longer in the policy do not count: "
        f"{', '.join(unchecked)}"
        if unchecked
        else ""
    )
    if len(strong) >= rule.required_copies:
        return decided.model_copy(
            update={
                "outcome": RetentionOutcome.RELEASE_PERMITTED,
                "reason": f"{len(strong)} strongly verified copies meet the "
                f"{rule.required_copies} required without the original{dropped}",
            }
        )
    reason = f"{len(strong)} of {rule.required_copies} required copies are strongly verified"
    if weak:
        reason += f"; {len(weak)} weaker copies do not count ({', '.join(weak)})"
    return decided.model_copy(update={"reason": reason + dropped})


def _located(placement: Placement, locations: Mapping[DestinationId, DestinationIdentity]) -> str:
    """Return an unchecked copy as "name (provider:account:root)".

    The bare name would read as a contradiction when the policy still has it, pointed at
    another account: the location says which copy did not count.
    """
    location = locations.get(placement.destination_id)
    where = location.label() if location is not None else str(placement.destination_id)
    return f"{placement.destination_name} ({where})"
