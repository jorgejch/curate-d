"""Content fingerprints.

Two independent notions of sameness. The strong fingerprint answers "are these the same
bytes"; the perceptual fingerprint answers "are these the same picture", which survives
re-encoding and rescaling that change every byte.
"""

import re
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from curate_d.domain.base import DomainModel


class HashAlgorithm(StrEnum):
    """Algorithms the system can compare across a source and a destination.

    pCloud reports SHA-1 in both regions, plus MD5 in the US region or SHA-256 in the EU
    region (rclone.org/pcloud); anything we hash ourselves in transit is SHA-256.
    """

    SHA256 = "sha256"
    SHA1 = "sha1"
    MD5 = "md5"


_DIGEST_LENGTH = {
    HashAlgorithm.SHA256: 64,
    HashAlgorithm.SHA1: 40,
    HashAlgorithm.MD5: 32,
}

_HEX = re.compile(r"\A[0-9a-f]+\Z")


class StrongFingerprint(DomainModel):
    """A cryptographic digest, carrying its algorithm so two are never compared blindly."""

    algorithm: HashAlgorithm
    digest: str = Field(description="Lowercase hex digest.")

    @model_validator(mode="after")
    def _check_digest(self) -> Self:
        """Reject a digest that isn't lowercase hex of the length its algorithm requires.

        Raises:
            ValueError: `digest` isn't lowercase hex, or its length doesn't match
                `algorithm`.
        """
        if not _HEX.fullmatch(self.digest):
            raise ValueError(f"digest must be lowercase hex, got {self.digest!r}")
        expected = _DIGEST_LENGTH[self.algorithm]
        if len(self.digest) != expected:
            raise ValueError(
                f"{self.algorithm} digest must be {expected} hex characters, got {len(self.digest)}"
            )
        return self

    def __str__(self) -> str:
        """Render as `algorithm:digest`, matching how digests are logged and compared."""
        return f"{self.algorithm}:{self.digest}"

    def matches(self, other: "StrongFingerprint") -> bool:
        """Return whether two fingerprints assert the same bytes.

        Returns False for different algorithms: that is "not comparable", which the
        verification ladder must handle by climbing to a rung that is.
        """
        return self.algorithm is other.algorithm and self.digest == other.digest


# A 64-bit perceptual hash. Stored as a signed integer so PostgreSQL bigint round-trips it.
PERCEPTUAL_HASH_BITS = 64
_PERCEPTUAL_MIN = -(2 ** (PERCEPTUAL_HASH_BITS - 1))
_PERCEPTUAL_MAX = 2 ** (PERCEPTUAL_HASH_BITS - 1) - 1


class PerceptualFingerprint(DomainModel):
    """A 64-bit image hash compared by Hamming distance rather than equality."""

    value: int = Field(ge=_PERCEPTUAL_MIN, le=_PERCEPTUAL_MAX)

    def distance_to(self, other: "PerceptualFingerprint") -> int:
        """Count differing bits between the two fingerprints; 0 means perceptually identical."""
        unsigned_self = self.value & 0xFFFFFFFFFFFFFFFF
        unsigned_other = other.value & 0xFFFFFFFFFFFFFFFF
        return (unsigned_self ^ unsigned_other).bit_count()
