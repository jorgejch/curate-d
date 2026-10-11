"""Perceptual fingerprints for images.

A 64-bit DCT hash (pHash): the image is reduced to greyscale at a fixed small size, so
re-encoding and rescaling barely move it, while a different picture lands far away.
Compared by Hamming distance rather than equality.

Fingerprints are computed and stored here; acting on a perceptual match is deferred
(design: "CPU-only inference, with deduplication separated from semantics").
"""

from typing import BinaryIO, Final

import imagehash
import pillow_heif
from PIL import Image, UnidentifiedImageError

from curate_d.domain import PERCEPTUAL_HASH_BITS, PerceptualFingerprint

# Bits two fingerprints may differ by and still be the same picture. pHash's own guidance
# puts re-encodings within a handful of bits and unrelated images near half of 64.
MATCH_THRESHOLD_BITS: Final = 10

# Refuse decompression bombs rather than hand the decoder unbounded memory.
Image.MAX_IMAGE_PIXELS = 200_000_000

pillow_heif.register_heif_opener()


class ImageUndecodableError(ValueError):
    """The content claims to be an image but no decoder accepts it."""


def is_image(media_type: str) -> bool:
    """Whether content of this type gets a perceptual fingerprint."""
    return media_type.startswith("image/")


def perceptual_fingerprint(handle: BinaryIO) -> PerceptualFingerprint:
    """Compute the perceptual fingerprint of an image, leaving the handle at the start.

    Raises:
        ImageUndecodableError: the bytes are not an image any installed decoder reads.
    """
    handle.seek(0)
    try:
        with Image.open(handle) as image:
            image.load()
            unsigned = int(str(imagehash.phash(image, hash_size=8)), 16)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as error:
        raise ImageUndecodableError(f"image could not be decoded: {error}") from None
    finally:
        handle.seek(0)
    # Stored signed so PostgreSQL's bigint round-trips it.
    signed = unsigned - (1 << PERCEPTUAL_HASH_BITS) if unsigned >> 63 else unsigned
    return PerceptualFingerprint(value=signed)


def same_picture(a: PerceptualFingerprint, b: PerceptualFingerprint) -> bool:
    """Whether two fingerprints are within the match threshold."""
    return a.distance_to(b) <= MATCH_THRESHOLD_BITS
