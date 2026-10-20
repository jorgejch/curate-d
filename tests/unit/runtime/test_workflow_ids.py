"""Workflow identifier derivation."""

import pytest

from curate_d.domain import (
    ContentObjectId,
    DestinationConfig,
    HashAlgorithm,
    Provider,
    StrongFingerprint,
    derive_content_object_id,
)
from curate_d.runtime.workflow_ids import (
    classification_workflow_id,
    content_placement_workflow_id,
    destination_check_workflow_id,
    full_rescan_workflow_id,
    placement_attempt_workflow_id,
    placement_workflow_id,
    reconcile_workflow_id,
    scan_workflow_id,
)


def _destination(name: str, account: str = "acct-1", root: str = "/Library") -> DestinationConfig:
    """Return a pCloud destination named `name` at `root` of `account`."""
    return DestinationConfig(name=name, provider=Provider.PCLOUD, account=account, root_path=root)


_ARCHIVE = _destination("archive", root="/Archive").id
_WORKING = _destination("working", root="/Working").id


def _fingerprint(char: str = "a") -> StrongFingerprint:
    """Build a SHA-256 fingerprint by repeating `char` to a valid digest length."""
    return StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest=char * 64)


def test_the_same_placement_derives_the_same_identifier() -> None:
    """This is the deduplication mechanism: same id, one execution."""
    assert placement_workflow_id(_fingerprint(), _ARCHIVE) == placement_workflow_id(
        _fingerprint(), _ARCHIVE
    )


def test_different_destinations_derive_different_identifiers() -> None:
    """Multiple destinations per content object must not collapse into one transfer."""
    assert placement_workflow_id(_fingerprint(), _ARCHIVE) != placement_workflow_id(
        _fingerprint(), _WORKING
    )


def test_different_content_derives_different_identifiers() -> None:
    """Derive different identifiers for placements of two different fingerprints."""
    assert placement_workflow_id(_fingerprint("a"), _ARCHIVE) != placement_workflow_id(
        _fingerprint("b"), _ARCHIVE
    )


def test_the_same_digest_under_a_different_algorithm_is_a_different_identifier() -> None:
    """Digests of different algorithms are not comparable and must not be conflated."""
    sha1 = StrongFingerprint(algorithm=HashAlgorithm.SHA1, digest="c" * 40)
    md5 = StrongFingerprint(algorithm=HashAlgorithm.MD5, digest="c" * 32)
    assert placement_workflow_id(sha1, _ARCHIVE) != placement_workflow_id(md5, _ARCHIVE)


def test_placement_and_classification_identifiers_never_collide() -> None:
    """Derive distinct identifiers for a placement and a classification of the same content."""
    assert placement_workflow_id(_fingerprint(), _ARCHIVE) != classification_workflow_id(
        _fingerprint()
    )


def test_a_component_containing_the_separator_is_rejected() -> None:
    """Otherwise two distinct work items could collapse and one would vanish silently."""
    with pytest.raises(ValueError, match="silently dropped"):
        scan_workflow_id("pcloud", "acct:extra", "/", "c1")


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
    first = placement_attempt_workflow_id(fp, _ARCHIVE, "/a/x.jpg", 0)
    assert first == placement_attempt_workflow_id(fp, _ARCHIVE, "/a/x.jpg", 0)
    assert first.startswith(placement_workflow_id(fp, _ARCHIVE))
    assert first != placement_attempt_workflow_id(fp, _ARCHIVE, "/b/x.jpg", 0)
    assert first != placement_attempt_workflow_id(fp, _ARCHIVE, "/a/x.jpg", 1)


def test_reconciliation_parts_are_distinct_and_repeatable() -> None:
    """A run hands over part by part; a repeated part collapses."""
    assert reconcile_workflow_id("sweep-1", 0) == reconcile_workflow_id("sweep-1", 0)
    assert reconcile_workflow_id("sweep-1", 0) != reconcile_workflow_id("sweep-1", 1)


def test_one_name_in_two_accounts_is_two_destinations_of_work() -> None:
    """Placement, attempt and check identities follow the destination identity, not its name."""
    here = _destination("library", account="acct-a").id
    there = _destination("library", account="acct-b").id
    fp = _fingerprint()
    content = ContentObjectId(derive_content_object_id(str(fp)))
    assert placement_workflow_id(fp, here) != placement_workflow_id(fp, there)
    assert placement_attempt_workflow_id(fp, here, "/x", 0) != placement_attempt_workflow_id(
        fp, there, "/x", 0
    )
    assert content_placement_workflow_id(content, here) != content_placement_workflow_id(
        content, there
    )
    assert destination_check_workflow_id(here, "c1") != destination_check_workflow_id(there, "c1")


def test_a_rename_keeps_the_work_identity() -> None:
    """Renaming a destination without moving it is not new work."""
    before = _destination("library").id
    after = _destination("renamed").id
    assert placement_workflow_id(_fingerprint(), before) == placement_workflow_id(
        _fingerprint(), after
    )


def test_destination_identities_hold_no_separator() -> None:
    """Every identifier keyed on a destination identity joins cleanly."""
    destination = _destination("library", account="user:with:colons")
    assert ":" not in str(destination.id)
    assert destination_check_workflow_id(destination.id, "c1").count(":") == 2
