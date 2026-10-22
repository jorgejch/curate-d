"""Retention decisions: retain by default, copies besides the original, the verification gate.

Only copies at destinations the policy still defines count.
"""

from uuid import uuid4

from curate_d.domain import (
    ContentObjectId,
    Placement,
    PlacementState,
    RetentionRule,
    VerificationLevel,
)
from curate_d.reconciliation.retention import RetentionOutcome, decide_retention

_CONTENT = ContentObjectId(uuid4())
_ALL = frozenset({"a", "b", "c"})


def _copy(
    destination: str,
    level: VerificationLevel = VerificationLevel.STRONG_COMMON_HASH,
    state: PlacementState = PlacementState.PRESENT,
) -> Placement:
    """A placement of the content at `destination`."""
    return Placement.build(
        content_object_id=_CONTENT,
        destination_name=destination,
        locator=f"/{destination}/a.jpg",
        template_version="seed-1",
    ).model_copy(update={"state": state, "verification_level": level})


def _releasing(required: int = 2) -> RetentionRule:
    """Return a rule that permits release once `required` copies exist."""
    return RetentionRule(name="archive", release_source=True, required_copies=required)


def test_originals_are_retained_when_no_rule_applies() -> None:
    """However many copies exist."""
    decision = decide_retention(None, [_copy("a"), _copy("b"), _copy("c")], destinations=_ALL)
    assert decision.outcome is RetentionOutcome.RETAIN
    assert "by default" in decision.reason


def test_a_rule_that_does_not_permit_release_retains() -> None:
    """The rule is named in the decision."""
    decision = decide_retention(
        RetentionRule(name="keep"), [_copy("a"), _copy("b")], destinations=_ALL
    )
    assert (decision.outcome, decision.rule) == (RetentionOutcome.RETAIN, "keep")


def test_the_original_is_kept_until_the_required_copies_exist_without_it() -> None:
    """Two required, one placement: the original and one copy are not enough."""
    decision = decide_retention(_releasing(2), [_copy("a")], destinations=_ALL)
    assert decision.outcome is RetentionOutcome.RETAIN
    assert decision.reason == "1 of 2 required copies are strongly verified"
    assert decision.strong_copies == ("a",)
    assert "source_counts_as_copy" not in decision.audit_details()


def test_the_original_never_counts_toward_required_copies() -> None:
    """One required: no placement retains, one strong placement permits release."""
    rule = _releasing(1)
    alone = decide_retention(rule, [], destinations=_ALL)
    copied = decide_retention(rule, [_copy("a")], destinations=_ALL)
    assert alone.outcome is RetentionOutcome.RETAIN
    assert alone.reason == "0 of 1 required copies are strongly verified"
    assert copied.outcome is RetentionOutcome.RELEASE_PERMITTED
    assert "source_counts_as_copy" not in copied.audit_details()


def test_enough_strong_copies_without_the_original_permit_release() -> None:
    """Common-hash and in-transit copies both count."""
    decision = decide_retention(
        _releasing(2),
        [_copy("a"), _copy("b", VerificationLevel.STRONG_IN_TRANSIT)],
        destinations=_ALL,
    )
    assert decision.outcome is RetentionOutcome.RELEASE_PERMITTED
    assert decision.strong_copies == ("a", "b")


def test_weakly_verified_copies_do_not_satisfy_the_gate() -> None:
    """Retained, and the reason names the copies that did not count."""
    decision = decide_retention(
        _releasing(2),
        [
            _copy("a"),
            _copy("b", VerificationLevel.WEAK),
            _copy("c", VerificationLevel.UNVERIFIABLE),
        ],
        destinations=_ALL,
    )
    assert decision.outcome is RetentionOutcome.RETAIN
    assert decision.reason == (
        "1 of 2 required copies are strongly verified; 2 weaker copies do not count (b, c)"
    )
    assert decision.weak_copies == ("b", "c")


def test_copies_that_are_not_present_do_not_count() -> None:
    """Failed, untrusted, absent or in-flight copies are no copies."""
    decision = decide_retention(
        _releasing(1),
        [
            _copy("a", state=PlacementState.UNTRUSTED),
            _copy("b", state=PlacementState.ABSENT),
            _copy("c", state=PlacementState.COPYING),
        ],
        destinations=_ALL,
    )
    assert decision.outcome is RetentionOutcome.RETAIN
    assert decision.strong_copies == decision.weak_copies == ()


def test_a_downgraded_copy_changes_the_decision() -> None:
    """Re-evaluated against the corrected level, release is withdrawn."""
    rule = _releasing(2)
    before = decide_retention(rule, [_copy("a"), _copy("b")], destinations=_ALL)
    after = decide_retention(
        rule, [_copy("a"), _copy("b", VerificationLevel.WEAK)], destinations=_ALL
    )
    assert before.outcome is RetentionOutcome.RELEASE_PERMITTED
    assert after.outcome is RetentionOutcome.RETAIN
    assert "weaker copies do not count (b)" in after.reason


def test_the_rules_minimum_level_is_respected() -> None:
    """A rule demanding the common-hash rung does not count in-transit copies."""
    rule = _releasing(1).model_copy(update={"minimum_level": VerificationLevel.STRONG_COMMON_HASH})
    decision = decide_retention(
        rule, [_copy("a", VerificationLevel.STRONG_IN_TRANSIT)], destinations=_ALL
    )
    assert decision.outcome is RetentionOutcome.RETAIN
    assert decision.audit_details()["minimum_level"] == "strong_common_hash"


def test_the_audit_details_carry_the_policy_version() -> None:
    """The version the decision applied is recorded, and null when unknown."""
    decision = decide_retention(_releasing(2), [_copy("a")], destinations=_ALL)
    assert decision.audit_details(policy_version=7)["policy_version"] == 7
    assert decision.audit_details()["policy_version"] is None


def test_a_copy_at_a_destination_no_longer_in_the_policy_does_not_count() -> None:
    """Two strong copies, one of them where nothing checks any more: retained, and named."""
    decision = decide_retention(_releasing(2), [_copy("a"), _copy("b")], destinations={"a"})
    assert decision.outcome is RetentionOutcome.RETAIN
    assert (decision.strong_copies, decision.unchecked_copies) == (("a",), ("b",))
    assert decision.reason == (
        "1 of 2 required copies are strongly verified; "
        "1 copies at destinations no longer in the policy do not count (b)"
    )


def test_a_dropped_destination_is_named_even_when_release_stays_permitted() -> None:
    """The reason changes, so the decision is recorded again."""
    decision = decide_retention(_releasing(1), [_copy("a"), _copy("b")], destinations={"a"})
    assert decision.outcome is RetentionOutcome.RELEASE_PERMITTED
    assert decision.unchecked_copies == ("b",)
    assert decision.reason.endswith("no longer in the policy do not count (b)")


def test_a_weak_copy_at_a_dropped_destination_is_listed_as_unchecked_only() -> None:
    """It is not listed among the weaker copies as well."""
    decision = decide_retention(
        _releasing(2),
        [_copy("a"), _copy("b", VerificationLevel.WEAK)],
        destinations={"a"},
    )
    assert (decision.weak_copies, decision.unchecked_copies) == ((), ("b",))
    assert "weaker copies" not in decision.reason


def test_absent_copies_at_dropped_destinations_are_not_listed() -> None:
    """Only present copies are named: a copy known to be gone is no copy anywhere."""
    decision = decide_retention(
        _releasing(1),
        [_copy("a"), _copy("b", state=PlacementState.ABSENT)],
        destinations={"a"},
    )
    assert decision.outcome is RetentionOutcome.RELEASE_PERMITTED
    assert decision.unchecked_copies == ()
    assert "no longer in the policy" not in decision.reason


def test_a_rule_that_does_not_permit_release_lists_no_copies() -> None:
    """Whatever the policy defines, retention needs no count."""
    decision = decide_retention(RetentionRule(name="keep"), [_copy("b")], destinations=())
    assert decision.unchecked_copies == decision.strong_copies == ()


def test_audit_details_carry_the_unchecked_copies() -> None:
    """Joined like the other copy lists, and None when there are none."""
    rule = _releasing(1)
    dropped = decide_retention(rule, [_copy("a"), _copy("b"), _copy("c")], destinations={"a"})
    kept = decide_retention(rule, [_copy("a")], destinations=_ALL)
    assert dropped.audit_details()["unchecked_copies"] == "b, c"
    assert kept.audit_details()["unchecked_copies"] is None
