"""Perceptual fingerprints for images.

A 64-bit DCT hash (pHash): the image is reduced to greyscale at a fixed small size, so
re-encoding and rescaling barely move it, while a different picture lands far away.
Compared by Hamming distance rather than equality.

Fingerprints are computed and stored here; acting on a perceptual match is deferred
(design: "CPU-only inference, with deduplication separated from semantics"). Decoding goes
through `content/decode.py`, so an image above the CPU worker's pixel budget is refused
rather than decoded, and keeps no perceptual fingerprint.
"""

from typing import BinaryIO, Final

import imagehash

from curate_d.content.decode import (
    DEFAULT_MAX_PIXELS,
    ImageTooLargeError,
    ImageUndecodableError,
    open_bounded,
)
from curate_d.domain import PERCEPTUAL_HASH_BITS, PerceptualFingerprint

__all__ = [
    "MATCH_THRESHOLD_BITS",
    "ImageTooLargeError",
    "ImageUndecodableError",
    "is_image",
    "perceptual_fingerprint",
    "same_picture",
]

# Bits two fingerprints may differ by and still be the same picture. pHash's own guidance
# puts re-encodings within a handful of bits and unrelated images near half of 64.
MATCH_THRESHOLD_BITS: Final = 10
# pHash reduces to 32x32, so a JPEG decoded with this edge or more keeps all it uses.
_DECODE_EDGE: Final = 512


def is_image(media_type: str) -> bool:
    """Whether content of this type gets a perceptual fingerprint."""
    return media_type.startswith("image/")


def perceptual_fingerprint(
    handle: BinaryIO, *, max_pixels: int = DEFAULT_MAX_PIXELS
) -> PerceptualFingerprint:
    """Compute the perceptual fingerprint of an image, leaving the handle at the start.

    Raises:
        ImageTooLargeError: the image would decode to more than `max_pixels` pixels.
        ImageUndecodableError: the bytes are not an image any installed decoder reads.
    """
    with open_bounded(handle, max_pixels=max_pixels, target_edge=_DECODE_EDGE) as image:
        unsigned = int(str(imagehash.phash(image, hash_size=8)), 16)
    # Stored signed so PostgreSQL's bigint round-trips it.
    signed = unsigned - (1 << PERCEPTUAL_HASH_BITS) if unsigned >> 63 else unsigned
    return PerceptualFingerprint(value=signed)


def same_picture(a: PerceptualFingerprint, b: PerceptualFingerprint) -> bool:
    """Whether two fingerprints are within the match threshold."""
    return a.distance_to(b) <= MATCH_THRESHOLD_BITS
