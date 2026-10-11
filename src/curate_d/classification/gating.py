"""Deciding between tiers, and recording why.

Tiers run in ascending order of cost and the first confident one wins: a more expensive
tier never runs when a cheaper one has already settled the file. A proposal carrying a
value outside the taxonomy is not escalated — whether the taxonomy should grow is a
question for a person, not for a costlier model. Exhausting every tier sends the content
to review with the best proposal described.

Every decision, accepted or routed to review, leaves an audit entry naming the tier, its
confidence, the facts it proposed and the evidence it rested on.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final

from pydantic import AwareDatetime

from curate_d.classification.deterministic import TierResult
from curate_d.classification.validation import taxonomy_rejections
from curate_d.domain import (
    AuditEntry,
    AuditEntryType,
    AuditValue,
    ContentObjectId,
    DomainModel,
    RejectedValue,
    ReviewReason,
    Taxonomy,
)

DECIDED_BY: Final = "classification"


class GateVerdict(StrEnum):
    """What to do with a tier's result."""

    ACCEPT = "accept"
    ESCALATE = "escalate"
    REVIEW = "review"


class GateDecision(DomainModel):
    """A verdict and what it rests on."""

    verdict: GateVerdict
    result: TierResult
    reason: ReviewReason | None = None
    rejected: tuple[RejectedValue, ...] = ()
    detail: str | None = None


def gate(
    result: TierResult, taxonomy: Taxonomy, *, threshold: float, last_tier: bool
) -> GateDecision:
    """Accept, escalate past, or send to review one tier's result."""
    rejected = taxonomy_rejections(result.facts, taxonomy)
    if rejected:
        values = ", ".join(f"{r.field.value} {r.value!r}" for r in rejected)
        return GateDecision(
            verdict=GateVerdict.REVIEW,
            result=result,
            reason=ReviewReason.REJECTED_VALUE,
            rejected=rejected,
            detail=f"{result.tier.value} proposed values outside the taxonomy: {values}",
        )
    if result.confidence >= threshold:
        return GateDecision(verdict=GateVerdict.ACCEPT, result=result)
    if last_tier:
        return GateDecision(
            verdict=GateVerdict.REVIEW,
            result=result,
            reason=ReviewReason.LOW_CONFIDENCE,
            detail=(
                f"no tier reached confidence {threshold}; the best was "
                f"{result.tier.value} at {result.confidence}"
            ),
        )
    return GateDecision(verdict=GateVerdict.ESCALATE, result=result)


type TierRunner = Callable[[], TierResult]


def run_tiers(
    tiers: Sequence[TierRunner],
    taxonomy: Taxonomy,
    *,
    threshold: float,
    more_tiers_follow: bool,
) -> tuple[GateDecision, list[TierResult]]:
    """Run tiers in order until one is accepted or sent to review.

    Tiers are called lazily, so a tier after an accepted one never runs. When
    `more_tiers_follow`, the last tier here escalates rather than ending in review.

    Returns:
        The final decision and every tier result produced on the way.
    """
    if not tiers:
        raise ValueError("at least one tier is needed")
    produced: list[TierResult] = []
    decision: GateDecision | None = None
    for index, tier in enumerate(tiers):
        result = tier()
        produced.append(result)
        decision = gate(
            result,
            taxonomy,
            threshold=threshold,
            last_tier=index == len(tiers) - 1 and not more_tiers_follow,
        )
        if decision.verdict is not GateVerdict.ESCALATE:
            break
    assert decision is not None
    return decision, produced


def decision_audit(
    content_object_id: ContentObjectId,
    decision: GateDecision,
    attempts: Sequence[TierResult],
    *,
    inputs: dict[str, AuditValue],
    now: AwareDatetime,
) -> AuditEntry:
    """The audit entry for a classification decision: accepted, or sent to review."""
    facts = decision.result.facts
    details: dict[str, AuditValue] = {
        **{f"input_{key}": value for key, value in inputs.items()},
        "tier": decision.result.tier.value,
        "confidence": decision.result.confidence,
        "tiers_run": ",".join(attempt.tier.value for attempt in attempts),
        "media_kind": facts.media_kind.value,
        "category": facts.category,
        "subcategory": facts.subcategory,
        "model_id": decision.result.model_id,
        **{f"evidence_{key}": value for key, value in decision.result.evidence.items()},
    }
    if decision.verdict is GateVerdict.REVIEW:
        details["review_reason"] = None if decision.reason is None else decision.reason.value
        details["rejected_values"] = ", ".join(r.value for r in decision.rejected) or None
        details["detail"] = decision.detail
    return AuditEntry(
        entry_type=(
            AuditEntryType.CLASSIFICATION_DECIDED
            if decision.verdict is GateVerdict.ACCEPT
            else AuditEntryType.CLASSIFICATION_REJECTED
        ),
        occurred_at=now,
        decided_by=f"{DECIDED_BY}:{decision.result.tier.value}",
        details=details,
        content_object_id=content_object_id,
    )


def spend_period(now: datetime) -> str:
    """The spend period a moment falls in: its calendar month in UTC."""
    return now.astimezone(UTC).strftime("%Y-%m")


def within_cap(spent: Decimal, estimate: Decimal, cap: Decimal) -> bool:
    """Whether a call estimated at `estimate` fits in what remains of `cap`."""
    return spent + estimate <= cap
