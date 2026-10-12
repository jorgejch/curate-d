"""Image decoding within the CPU worker's memory budget.

Every decode of untrusted image bytes on the CPU profile goes through `open_bounded`. It
reads the dimensions from the header, lets a JPEG decode at a reduced scale (Pillow's draft
mode) when the caller only needs a smaller picture, and refuses anything still above the
pixel budget before a single pixel is decoded. The budget is `cpu_decode_max_pixels`;
`docs/cpu-worker-budget.md` derives it and records what a decode at the budget costs.

`Image.MAX_IMAGE_PIXELS` stays set as a process-wide backstop for any decode that does
not come through here.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import BinaryIO, Final

import pillow_heif
from PIL import Image, UnidentifiedImageError

# Pillow warns above this and raises above twice it. The budget below is lower; this only
# catches a decode that bypasses `open_bounded`.
Image.MAX_IMAGE_PIXELS = 200_000_000

pillow_heif.register_heif_opener()

# Pixels one decode may hold unless configured otherwise (`cpu_decode_max_pixels`). A 1 GiB
# CPU worker idles near 200 MiB and runs up to four decodes at once, so each gets about
# 200 MiB; a decode that cannot draft peaks near 5.7 bytes a pixel on Linux, measured
# (docs/cpu-worker-budget.md), which leaves 32 MP with a margin.
DEFAULT_MAX_PIXELS: Final = 32_000_000

# Formats whose decoder can scale down while decoding.
_DRAFTABLE: Final = frozenset({"JPEG", "MPO"})


class ImageUndecodableError(ValueError):
    """The content claims to be an image but no decoder accepts it."""


class ImageTooLargeError(ValueError):
    """An image would decode to more pixels than the CPU worker's budget allows."""

    def __init__(self, pixels: int | None, max_pixels: int) -> None:
        """Record the image's decoded size, when the header gave it, and the budget."""
        size = (
            f"{pixels} pixels"
            if pixels is not None
            else f"over Pillow's ceiling of {2 * (Image.MAX_IMAGE_PIXELS or 0)} pixels"
        )
        super().__init__(f"image is {size}; the decode budget is {max_pixels} pixels")
        self.pixels = pixels
        self.max_pixels = max_pixels


def _draft_size(size: tuple[int, int], target_edge: int | None) -> tuple[int, int] | None:
    """Return the size to ask a draft decode for, so its long edge still covers the target.

    Pillow keeps both requested dimensions covered; asking for the target on the long edge
    and the proportional length on the short one lets a wide image scale down as far as a
    square one would.
    """
    if target_edge is None:
        return None
    width, height = size
    if width >= height:
        return target_edge, max(1, target_edge * height // width)
    return max(1, target_edge * width // height), target_edge


@contextmanager
def open_bounded(
    handle: BinaryIO,
    *,
    max_pixels: int,
    target_edge: int | None = None,
    greyscale: bool = False,
) -> Iterator[Image.Image]:
    """Yield a loaded image, decoded at the smallest scale the caller can use.

    The handle is read from the start and left there afterwards.

    Args:
        handle: The image bytes.
        max_pixels: Most pixels the decoded image may hold.
        target_edge: The long edge the caller needs. A JPEG then decodes at the smallest
            of 1/1, 1/2, 1/4 or 1/8 scale whose long edge still covers it; other formats
            decode in full.
        greyscale: Decode a JPEG straight to greyscale, for callers that only read
            luminance.

    Raises:
        ImageTooLargeError: the decoded image would exceed `max_pixels`.
        ImageUndecodableError: no installed decoder reads the bytes.
    """
    handle.seek(0)
    try:
        try:
            image = Image.open(handle)
        except Image.DecompressionBombError:
            # Above Pillow's own ceiling: it refuses before exposing the size.
            raise ImageTooLargeError(None, max_pixels) from None
        except (UnidentifiedImageError, OSError, ValueError) as error:
            raise ImageUndecodableError(f"image could not be decoded: {error}") from None
        with image:
            if image.format in _DRAFTABLE and (target_edge is not None or greyscale):
                image.draft("L" if greyscale else None, _draft_size(image.size, target_edge))
            pixels = image.width * image.height
            if pixels > max_pixels:
                raise ImageTooLargeError(pixels, max_pixels)
            try:
                image.load()
            except (OSError, ValueError, Image.DecompressionBombError) as error:
                raise ImageUndecodableError(f"image could not be decoded: {error}") from None
            yield image
    finally:
        handle.seek(0)
