"""Tier escalation: cheapest first, stop when confident, review when exhausted."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from curate_d.classification.deterministic import TierResult
from curate_d.classification.gating import (
    GateVerdict,
    decision_audit,
    gate,
    run_tiers,
    spend_period,
    within_cap,
)
from curate_d.domain import (
    SEED_TAXONOMY,
    AuditEntryType,
    ClassificationTier,
    ContentObjectId,
    Facts,
    MediaKind,
    ReviewReason,
)

_NOW = datetime(2026, 9, 29, tzinfo=UTC)


def _result(
    tier: ClassificationTier, confidence: float, *, category: str | None = "Documents"
) -> TierResult:
    """A tier's proposal."""
    return TierResult(
        tier=tier,
        facts=Facts(media_kind=MediaKind.DOCUMENT, category=category),
        confidence=confidence,
        evidence={"rule": "test"},
    )


class _Tier:
    """A tier that records whether it ran."""

    def __init__(self, result: TierResult) -> None:
        """Answer with `result`."""
        self.result = result
        self.calls = 0

    def __call__(self) -> TierResult:
        """Run once more."""
        self.calls += 1
        return self.result


def test_no_expensive_tier_runs_when_a_cheaper_one_is_confident() -> None:
    """The deterministic tier settles it; the local and remote tiers never run."""
    cheap = _Tier(_result(ClassificationTier.DETERMINISTIC, 0.95))
    local = _Tier(_result(ClassificationTier.LOCAL_INFERENCE, 0.99))
    decision, produced = run_tiers(
        [cheap, local], SEED_TAXONOMY, threshold=0.8, more_tiers_follow=True
    )
    assert decision.verdict is GateVerdict.ACCEPT
    assert decision.result.tier is ClassificationTier.DETERMINISTIC
    assert (cheap.calls, local.calls) == (1, 0)
    assert len(produced) == 1


def test_tiers_escalate_in_order_until_one_is_confident() -> None:
    """A weak deterministic result escalates to the local tier, which settles it."""
    cheap = _Tier(_result(ClassificationTier.DETERMINISTIC, 0.4))
    local = _Tier(_result(ClassificationTier.LOCAL_INFERENCE, 0.9))
    decision, produced = run_tiers(
        [cheap, local], SEED_TAXONOMY, threshold=0.8, more_tiers_follow=True
    )
    assert decision.result.tier is ClassificationTier.LOCAL_INFERENCE
    assert [r.tier for r in produced] == [
        ClassificationTier.DETERMINISTIC,
        ClassificationTier.LOCAL_INFERENCE,
    ]


def test_exhausting_the_cheap_tiers_escalates_when_a_remote_tier_follows() -> None:
    """Below threshold everywhere, with the remote tier still to come."""
    decision, _ = run_tiers(
        [_Tier(_result(ClassificationTier.DETERMINISTIC, 0.3))],
        SEED_TAXONOMY,
        threshold=0.8,
        more_tiers_follow=True,
    )
    assert decision.verdict is GateVerdict.ESCALATE


def test_exhausting_every_tier_routes_to_review() -> None:
    """With no tier left, low confidence is a person's call."""
    decision, _ = run_tiers(
        [_Tier(_result(ClassificationTier.DETERMINISTIC, 0.3))],
        SEED_TAXONOMY,
        threshold=0.8,
        more_tiers_follow=False,
    )
    assert decision.verdict is GateVerdict.REVIEW
    assert decision.reason is ReviewReason.LOW_CONFIDENCE


def test_an_out_of_taxonomy_value_goes_to_review_without_escalating() -> None:
    """A confident but out-of-taxonomy proposal is not handed to a costlier tier."""
    local = _Tier(_result(ClassificationTier.LOCAL_INFERENCE, 0.99))
    decision, _ = run_tiers(
        [_Tier(_result(ClassificationTier.DETERMINISTIC, 0.99, category="Receipts")), local],
        SEED_TAXONOMY,
        threshold=0.8,
        more_tiers_follow=True,
    )
    assert decision.verdict is GateVerdict.REVIEW
    assert decision.reason is ReviewReason.REJECTED_VALUE
    assert [r.value for r in decision.rejected] == ["Receipts"]
    assert local.calls == 0


def test_no_tiers_is_a_caller_error() -> None:
    """There is always at least the deterministic tier."""
    with pytest.raises(ValueError, match="at least one"):
        run_tiers([], SEED_TAXONOMY, threshold=0.8, more_tiers_follow=False)


def test_an_accepted_decision_is_audited_with_its_inputs_and_tier() -> None:
    """The entry names the decision, the tier, and what it was based on."""
    decision = gate(
        _result(ClassificationTier.DETERMINISTIC, 0.95),
        SEED_TAXONOMY,
        threshold=0.8,
        last_tier=False,
    )
    entry = decision_audit(
        ContentObjectId(uuid4()),
        decision,
        [decision.result],
        inputs={"media_type": "application/pdf"},
        now=_NOW,
    )
    assert entry.entry_type is AuditEntryType.CLASSIFICATION_DECIDED
    assert entry.decided_by == "classification:deterministic"
    assert entry.details["input_media_type"] == "application/pdf"
    assert entry.details["tier"] == "deterministic"
    assert entry.details["category"] == "Documents"
    assert entry.details["evidence_rule"] == "test"


def test_a_review_decision_is_audited_with_its_reason() -> None:
    """A rejection names why and which values."""
    decision = gate(
        _result(ClassificationTier.REMOTE_MODEL, 0.9, category="Receipts"),
        SEED_TAXONOMY,
        threshold=0.8,
        last_tier=True,
    )
    entry = decision_audit(
        ContentObjectId(uuid4()), decision, [decision.result], inputs={}, now=_NOW
    )
    assert entry.entry_type is AuditEntryType.CLASSIFICATION_REJECTED
    assert entry.details["review_reason"] == "rejected_value"
    assert entry.details["rejected_values"] == "Receipts"


def test_spend_is_counted_per_utc_calendar_month() -> None:
    """A late-evening call on the last day west of UTC counts in the next month."""
    from datetime import timedelta, timezone

    late = datetime(2026, 9, 30, 22, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert spend_period(late) == "2026-10"
    assert spend_period(_NOW) == "2026-09"


def test_a_call_fits_until_the_cap() -> None:
    """Up to and including the cap is within it."""
    assert within_cap(Decimal("9.5"), Decimal("0.5"), Decimal("10"))
    assert not within_cap(Decimal("9.6"), Decimal("0.5"), Decimal("10"))
