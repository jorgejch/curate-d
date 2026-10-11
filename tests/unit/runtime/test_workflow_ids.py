"""Workflow identifier derivation."""

import pytest

from curate_d.domain import HashAlgorithm, StrongFingerprint
from curate_d.runtime.workflow_ids import (
    classification_workflow_id,
    full_rescan_workflow_id,
    placement_attempt_workflow_id,
    placement_workflow_id,
    reconcile_workflow_id,
    scan_workflow_id,
)


def _fingerprint(char: str = "a") -> StrongFingerprint:
    """Build a SHA-256 fingerprint by repeating `char` to a valid digest length."""
    return StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest=char * 64)


def test_the_same_placement_derives_the_same_identifier() -> None:
    """This is the deduplication mechanism: same id, one execution."""
    assert placement_workflow_id(_fingerprint(), "archive") == placement_workflow_id(
        _fingerprint(), "archive"
    )


def test_different_destinations_derive_different_identifiers() -> None:
    """Multiple destinations per content object must not collapse into one transfer."""
    assert placement_workflow_id(_fingerprint(), "archive") != placement_workflow_id(
        _fingerprint(), "working"
    )


def test_different_content_derives_different_identifiers() -> None:
    """Derive different identifiers for placements of two different fingerprints."""
    assert placement_workflow_id(_fingerprint("a"), "archive") != placement_workflow_id(
        _fingerprint("b"), "archive"
    )


def test_the_same_digest_under_a_different_algorithm_is_a_different_identifier() -> None:
    """Digests of different algorithms are not comparable and must not be conflated."""
    sha1 = StrongFingerprint(algorithm=HashAlgorithm.SHA1, digest="c" * 40)
    md5 = StrongFingerprint(algorithm=HashAlgorithm.MD5, digest="c" * 32)
    assert placement_workflow_id(sha1, "archive") != placement_workflow_id(md5, "archive")


def test_placement_and_classification_identifiers_never_collide() -> None:
    """Derive distinct identifiers for a placement and a classification of the same content."""
    assert placement_workflow_id(_fingerprint(), "archive") != classification_workflow_id(
        _fingerprint()
    )


def test_a_component_containing_the_separator_is_rejected() -> None:
    """Otherwise two distinct work items could collapse and one would vanish silently."""
    with pytest.raises(ValueError, match="silently dropped"):
        placement_workflow_id(_fingerprint(), "archive:extra")


def test_a_scan_carries_a_cycle_so_it_can_run_again() -> None:
    """Unlike a placement, a scan is supposed to repeat."""
    first = scan_workflow_id("pcloud", "acct", "/Photos", "2026-09-07")
    second = scan_workflow_id("pcloud", "acct", "/Photos", "2026-09-08")
    assert first != second
    assert first == scan_workflow_id("pcloud", "acct", "/Photos", "2026-09-07")


def test_scan_and_full_rescan_identifiers_are_distinct() -> None:
    """They run on independent intervals and must not collapse into one another."""
    assert scan_workflow_id("pcloud", "acct", "/", "c1") != full_rescan_workflow_id(
        "pcloud", "acct", "c1"
    )


def test_subtree_boundaries_cannot_be_confused_by_concatenation() -> None:
    """Keep account and path from concatenating into an equivalent identifier."""
    assert scan_workflow_id("pcloud", "acct", "/ab", "c") != scan_workflow_id(
        "pcloud", "acc", "t/ab", "c"
    )


def test_a_placement_attempt_is_keyed_on_its_locator_and_attempt() -> None:
    """A re-render or a retry is new work; a repeat of either is not."""
    fp = _fingerprint()
    first = placement_attempt_workflow_id(fp, "archive", "/a/x.jpg", 0)
    assert first == placement_attempt_workflow_id(fp, "archive", "/a/x.jpg", 0)
    assert first.startswith(placement_workflow_id(fp, "archive"))
    assert first != placement_attempt_workflow_id(fp, "archive", "/b/x.jpg", 0)
    assert first != placement_attempt_workflow_id(fp, "archive", "/a/x.jpg", 1)


def test_reconciliation_parts_are_distinct_and_repeatable() -> None:
    """A run hands over part by part; a repeated part collapses."""
    assert reconcile_workflow_id("sweep-1", 0) == reconcile_workflow_id("sweep-1", 0)
    assert reconcile_workflow_id("sweep-1", 0) != reconcile_workflow_id("sweep-1", 1)
