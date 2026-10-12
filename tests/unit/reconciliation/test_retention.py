"""Retention decisions: retain by default, the original as a copy, the verification gate."""

from uuid import uuid4

from curate_d.domain import (
    ContentObjectId,
    DestinationIdentity,
    Placement,
    PlacementState,
    Provider,
    RetentionRule,
    VerificationLevel,
)
from curate_d.reconciliation.retention import RetentionOutcome, decide_retention

_CONTENT = ContentObjectId(uuid4())


def _copy(
    destination: str,
    level: VerificationLevel = VerificationLevel.STRONG_COMMON_HASH,
    state: PlacementState = PlacementState.PRESENT,
    *,
    account: str = "acct-1",
) -> Placement:
    """A placement of the content at `destination`, in `account`."""
    return Placement.build(
        content_object_id=_CONTENT,
        destination_id=_identity(destination, account).id,
        destination_name=destination,
        locator=f"/{destination}/a.jpg",
        template_version="seed-1",
    ).model_copy(update={"state": state, "verification_level": level})


def _identity(destination: str, account: str = "acct-1") -> DestinationIdentity:
    """Return the identity a destination named `destination` points at in `account`."""
    return DestinationIdentity(
        provider=Provider.PCLOUD, account=account, root_path=f"/{destination}"
    )


def _releasing(required: int = 2, *, counts: bool = True) -> RetentionRule:
    """A rule that permits release once `required` copies exist."""
    return RetentionRule(
        name="archive",
        release_source=True,
        required_copies=required,
        source_counts_as_copy=counts,
    )


def test_originals_are_retained_when_no_rule_applies() -> None:
    """However many copies exist."""
    decision = decide_retention(None, [_copy("a"), _copy("b"), _copy("c")])
    assert decision.outcome is RetentionOutcome.RETAIN
    assert "by default" in decision.reason


def test_a_rule_that_does_not_permit_release_retains() -> None:
    """The rule is named in the decision."""
    decision = decide_retention(RetentionRule(name="keep"), [_copy("a"), _copy("b")])
    assert (decision.outcome, decision.rule) == (RetentionOutcome.RETAIN, "keep")


def test_the_original_is_kept_while_it_is_one_of_the_required_copies() -> None:
    """Two required, one placement: the original is the second copy."""
    decision = decide_retention(_releasing(2), [_copy("a")])
    assert decision.outcome is RetentionOutcome.RETAIN
    assert decision.reason == (
        "the original is one of the 2 required copies; releasing it would leave 1"
    )
    assert decision.strong_copies == ("a",)


def test_enough_strong_copies_without_the_original_permit_release() -> None:
    """Common-hash and in-transit copies both count."""
    decision = decide_retention(
        _releasing(2),
        [_copy("a"), _copy("b", VerificationLevel.STRONG_IN_TRANSIT)],
    )
    assert decision.outcome is RetentionOutcome.RELEASE_PERMITTED
    assert decision.strong_copies == ("a", "b")


def test_weakly_verified_copies_do_not_satisfy_the_gate() -> None:
    """Retained, and the reason names the copies that did not count."""
    decision = decide_retention(
        _releasing(2, counts=False),
        [
            _copy("a"),
            _copy("b", VerificationLevel.WEAK),
            _copy("c", VerificationLevel.UNVERIFIABLE),
        ],
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
    )
    assert decision.outcome is RetentionOutcome.RETAIN
    assert decision.strong_copies == decision.weak_copies == ()


def test_a_downgraded_copy_changes_the_decision() -> None:
    """Re-evaluated against the corrected level, release is withdrawn."""
    rule = _releasing(2)
    before = decide_retention(rule, [_copy("a"), _copy("b")])
    after = decide_retention(rule, [_copy("a"), _copy("b", VerificationLevel.WEAK)])
    assert before.outcome is RetentionOutcome.RELEASE_PERMITTED
    assert after.outcome is RetentionOutcome.RETAIN
    assert "weaker copies do not count (b)" in after.reason


def test_the_rules_minimum_level_is_respected() -> None:
    """A rule demanding the common-hash rung does not count in-transit copies."""
    rule = _releasing(1).model_copy(update={"minimum_level": VerificationLevel.STRONG_COMMON_HASH})
    decision = decide_retention(rule, [_copy("a", VerificationLevel.STRONG_IN_TRANSIT)])
    assert decision.outcome is RetentionOutcome.RETAIN
    assert decision.audit_details()["minimum_level"] == "strong_common_hash"


def test_copies_are_named_with_their_identity() -> None:
    """A decision says where each copy is, not only the name it was placed under."""
    copies = [_copy("a"), _copy("b", VerificationLevel.WEAK)]
    identities = {p.destination_id: _identity(p.destination_name) for p in copies}
    decision = decide_retention(_releasing(2), copies, identities)
    assert decision.strong_copies == ("a [pcloud:acct-1:/a]",)
    assert decision.weak_copies == ("b [pcloud:acct-1:/b]",)


def test_two_copies_under_one_name_are_listed_apart() -> None:
    """One name moved to another account holds two copies; the decision tells them apart."""
    copies = [_copy("lib"), _copy("lib", account="acct-2")]
    identities = {
        copies[0].destination_id: _identity("lib"),
        copies[1].destination_id: _identity("lib", "acct-2"),
    }
    decision = decide_retention(_releasing(2), copies, identities)
    assert decision.strong_copies == ("lib [pcloud:acct-1:/lib]", "lib [pcloud:acct-2:/lib]")
