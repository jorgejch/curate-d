"""Each rung is chosen from recorded evidence alone, and every disagreement is a mismatch."""

import pytest

from curate_d.domain import HashAlgorithm, StrongFingerprint, VerificationLevel
from curate_d.placement.rclone import corrupted_transfer, out_of_space
from curate_d.placement.verification import (
    CorruptTransfer,
    SideEvidence,
    TransferEvidence,
    Verdict,
    assess,
    expected_level,
    source_changed,
)

_SHA1_A = StrongFingerprint(algorithm=HashAlgorithm.SHA1, digest="a" * 40)
_SHA1_B = StrongFingerprint(algorithm=HashAlgorithm.SHA1, digest="b" * 40)
_SHA256_A = StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="a" * 64)
_SHA256_B = StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="b" * 64)
_MD5_A = StrongFingerprint(algorithm=HashAlgorithm.MD5, digest="a" * 32)


def _evidence(source: SideEvidence, destination: SideEvidence, **extra: object) -> TransferEvidence:
    """Evidence of one copy."""
    return TransferEvidence(source=source, destination=destination, **extra)  # type: ignore[arg-type]


def test_a_common_hash_that_agrees_is_the_strongest_rung() -> None:
    """Both sides report SHA-1 and it matches."""
    result = assess(
        _evidence(
            SideEvidence(size_bytes=5, reported=(_SHA1_A,)),
            SideEvidence(size_bytes=5, reported=(_SHA1_A,)),
        )
    )
    assert (result.verdict, result.level) == (
        Verdict.VERIFIED,
        VerificationLevel.STRONG_COMMON_HASH,
    )
    assert result.compared == "sha1"


def test_the_strongest_shared_algorithm_is_compared() -> None:
    """With SHA-1 and SHA-256 on both sides, SHA-256 decides."""
    result = assess(
        _evidence(
            SideEvidence(reported=(_SHA1_A, _SHA256_A)),
            SideEvidence(reported=(_SHA1_A, _SHA256_B)),
        )
    )
    assert result.verdict is Verdict.MISMATCH
    assert result.compared == "sha256"


def test_a_common_hash_that_differs_is_a_mismatch_with_both_values() -> None:
    """The copy is not the source."""
    result = assess(_evidence(SideEvidence(reported=(_SHA1_A,)), SideEvidence(reported=(_SHA1_B,))))
    assert result.verdict is Verdict.MISMATCH
    assert (result.source_value, result.destination_value) == ("a" * 40, "b" * 40)


def test_without_a_common_hash_a_read_back_digest_gives_the_in_transit_rung() -> None:
    """The destination reports MD5 only; SHA-1 computed over the copy matches the source."""
    result = assess(
        _evidence(
            SideEvidence(reported=(_SHA1_A,)),
            SideEvidence(reported=(_MD5_A,), computed=(_SHA1_A,)),
        )
    )
    assert (result.verdict, result.level) == (Verdict.VERIFIED, VerificationLevel.STRONG_IN_TRANSIT)
    assert result.compared == "sha1 read back"


def test_a_read_back_digest_that_differs_is_a_mismatch() -> None:
    """Computed bytes disagree with the source's digest."""
    result = assess(_evidence(SideEvidence(reported=(_SHA1_A,)), SideEvidence(computed=(_SHA1_B,))))
    assert result.verdict is Verdict.MISMATCH


def test_only_sizes_give_the_weak_rung_with_the_reason() -> None:
    """Nothing hashable on either side: weak, and why."""
    result = assess(
        _evidence(
            SideEvidence(size_bytes=9, unavailable=("source reports no hashes",)),
            SideEvidence(size_bytes=9, unavailable=("read-back failed: timeout",)),
        )
    )
    assert (result.verdict, result.level) == (Verdict.VERIFIED, VerificationLevel.WEAK)
    assert "read-back failed" in (result.reason or "")


def test_differing_sizes_are_a_mismatch() -> None:
    """Even the weakest comparison can refuse a copy."""
    result = assess(_evidence(SideEvidence(size_bytes=9), SideEvidence(size_bytes=8)))
    assert result.verdict is Verdict.MISMATCH


def test_nothing_comparable_is_unverifiable() -> None:
    """No size, no digest: the lowest rung."""
    result = assess(_evidence(SideEvidence(), SideEvidence()))
    assert (result.verdict, result.level) == (Verdict.VERIFIED, VerificationLevel.UNVERIFIABLE)


def test_the_transfer_services_rejection_is_a_mismatch() -> None:
    """rclone deleted a corrupt copy; that is recorded, not re-assessed."""
    corrupt = CorruptTransfer(what="sha1", source="aa", destination="bb")
    result = assess(_evidence(SideEvidence(), SideEvidence(), corrupt=corrupt))
    assert result.verdict is Verdict.MISMATCH
    assert (result.compared, result.source_value, result.destination_value) == (
        "transfer service sha1 check",
        "aa",
        "bb",
    )


def test_a_source_whose_digest_changed_since_fingerprinting_is_sent_back() -> None:
    """The copy may verify against itself, but it is not the classified content."""
    evidence = _evidence(SideEvidence(reported=(_SHA1_B,)), SideEvidence(reported=(_SHA1_B,)))
    result = source_changed(evidence, fingerprinted_digests=(_SHA1_A,), fingerprinted_marker=None)
    assert result is not None
    assert result.verdict is Verdict.SOURCE_CHANGED
    assert (result.source_value, result.destination_value) == ("b" * 40, "a" * 40)


def test_a_source_whose_marker_changed_is_sent_back() -> None:
    """The version marker moved on."""
    evidence = _evidence(SideEvidence(), SideEvidence(), source_version_marker="v2")
    result = source_changed(evidence, fingerprinted_digests=(), fingerprinted_marker="v1")
    assert result is not None and result.compared == "version_marker"


def test_an_unchanged_source_passes() -> None:
    """Same digest and marker: nothing to report."""
    evidence = _evidence(
        SideEvidence(reported=(_SHA1_A,)), SideEvidence(), source_version_marker="v1"
    )
    assert (
        source_changed(evidence, fingerprinted_digests=(_SHA1_A,), fingerprinted_marker="v1")
        is None
    )


@pytest.mark.parametrize(
    ("source", "destination", "level"),
    [
        (
            {HashAlgorithm.SHA1},
            {HashAlgorithm.SHA1, HashAlgorithm.MD5},
            VerificationLevel.STRONG_COMMON_HASH,
        ),
        ({HashAlgorithm.SHA1}, {HashAlgorithm.MD5}, VerificationLevel.STRONG_IN_TRANSIT),
        (set(), {HashAlgorithm.MD5}, VerificationLevel.WEAK),
    ],
)
def test_the_expected_level_follows_advertised_hashes(
    source: set[HashAlgorithm], destination: set[HashAlgorithm], level: VerificationLevel
) -> None:
    """Dry runs predict the rung from what each provider advertises."""
    assert expected_level(frozenset(source), frozenset(destination)) is level


@pytest.mark.parametrize(
    ("error", "what", "source", "destination"),
    [
        ("corrupted on transfer: sizes differ 10 vs 11", "size", "10", "11"),
        ("corrupted on transfer: sizes differ src(a:) 10 vs dst(b:) 11", "size", "10", "11"),
        (
            'corrupted on transfer: sha1 hashes differ src(a:) "AA11" vs dst(b:) "bb22"',
            "sha1",
            "aa11",
            "bb22",
        ),
        ('corrupted on transfer: md5 hash differ "aa" vs "bb"', "md5", "aa", "bb"),
    ],
)
def test_rclones_post_copy_rejection_is_parsed(
    error: str, what: str, source: str, destination: str
) -> None:
    """Both values come back from the error text, in either message form."""
    parsed = corrupted_transfer(error)
    assert parsed is not None
    assert (parsed.what, parsed.source, parsed.destination) == (what, source, destination)


def test_other_errors_are_not_corruption() -> None:
    """A network error is not a mismatch."""
    assert corrupted_transfer("couldn't connect: timeout") is None


def test_scratch_exhaustion_is_recognised() -> None:
    """The disk-full error is told apart from others."""
    assert out_of_space("write /tmp/x: no space left on device")
    assert not out_of_space("permission denied")
