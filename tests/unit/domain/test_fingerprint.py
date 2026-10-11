"""Fingerprint comparison and validation."""

import pytest
from pydantic import ValidationError

from curate_d.domain import HashAlgorithm, PerceptualFingerprint, StrongFingerprint


def test_rejects_non_hex_digest() -> None:
    """A digest containing non-hex characters is rejected."""
    with pytest.raises(ValidationError, match="lowercase hex"):
        StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="Z" * 64)


def test_rejects_digest_of_the_wrong_length() -> None:
    """A digest of the wrong length for its algorithm is rejected."""
    with pytest.raises(ValidationError, match="must be 64 hex characters"):
        StrongFingerprint(algorithm=HashAlgorithm.SHA256, digest="ab")


def test_matching_digests_of_the_same_algorithm_match() -> None:
    """Two fingerprints with equal algorithm and digest match."""
    left = StrongFingerprint(algorithm=HashAlgorithm.SHA1, digest="b" * 40)
    right = StrongFingerprint(algorithm=HashAlgorithm.SHA1, digest="b" * 40)
    assert left.matches(right)


def test_same_digest_string_under_different_algorithms_does_not_match() -> None:
    """Different algorithms are not comparable; treating them as equal would be a lie."""
    sha1 = StrongFingerprint(algorithm=HashAlgorithm.SHA1, digest="c" * 40)
    md5 = StrongFingerprint(algorithm=HashAlgorithm.MD5, digest="c" * 32)
    assert not sha1.matches(md5)


def test_string_form_names_its_algorithm() -> None:
    """`str()` renders as `algorithm:digest`."""
    assert str(StrongFingerprint(algorithm=HashAlgorithm.MD5, digest="d" * 32)) == f"md5:{'d' * 32}"


def test_identical_perceptual_hashes_are_zero_distance() -> None:
    """Two equal perceptual fingerprints have Hamming distance zero."""
    assert PerceptualFingerprint(value=99).distance_to(PerceptualFingerprint(value=99)) == 0


def test_perceptual_distance_counts_differing_bits_across_the_sign_boundary() -> None:
    """Stored signed for bigint, so distance must be computed on the unsigned bits."""
    all_ones = PerceptualFingerprint(value=-1)
    all_zeroes = PerceptualFingerprint(value=0)
    assert all_ones.distance_to(all_zeroes) == 64


def test_rejects_perceptual_value_outside_64_bits() -> None:
    """A value outside the signed 64-bit range is rejected."""
    with pytest.raises(ValidationError):
        PerceptualFingerprint(value=2**63)
