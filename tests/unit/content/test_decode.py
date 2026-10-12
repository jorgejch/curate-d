"""Bounded decoding: the pixel budget is enforced from the header, before any decode."""

import io

import pytest
from PIL import Image

from curate_d.content.decode import (
    DEFAULT_MAX_PIXELS,
    ImageTooLargeError,
    ImageUndecodableError,
    open_bounded,
)
from tests.stubs import media


def _encoded(size: tuple[int, int], fmt: str) -> io.BytesIO:
    """Return a plain grey RGB image of `size` saved in `fmt`."""
    buffer = io.BytesIO()
    Image.new("RGB", size, (128, 128, 128)).save(buffer, format=fmt)
    buffer.seek(0)
    return buffer


class _NoLoad:
    """Records whether `Image.load` ran."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Wrap `Image.Image.load` to count calls."""
        self.calls = 0
        original = Image.Image.load

        def counting(image: Image.Image) -> object:
            """Count, then load."""
            self.calls += 1
            return original(image)

        monkeypatch.setattr(Image.Image, "load", counting)


def test_a_header_beyond_pillows_ceiling_is_refused_as_too_large() -> None:
    """A 450 MP header is a refusal, never a DecompressionBombError escaping."""
    with (
        pytest.raises(ImageTooLargeError, match="Pillow's ceiling") as refused,
        open_bounded(io.BytesIO(media.png_header(30000, 15000)), max_pixels=DEFAULT_MAX_PIXELS),
    ):
        pass
    assert refused.value.pixels is None
    assert refused.value.max_pixels == DEFAULT_MAX_PIXELS


def test_a_header_in_pillows_warning_range_is_refused_before_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """300 MP: Pillow would only warn and decode it; the budget refuses it unread."""
    loads = _NoLoad(monkeypatch)
    with (
        pytest.warns(Image.DecompressionBombWarning),
        pytest.raises(ImageTooLargeError, match="300000000 pixels") as refused,
        open_bounded(io.BytesIO(media.png_header(20000, 15000)), max_pixels=DEFAULT_MAX_PIXELS),
    ):
        pass
    assert refused.value.pixels == 300_000_000
    assert loads.calls == 0


def test_a_jpeg_decodes_at_a_reduced_scale_within_the_budget() -> None:
    """12 MP with a 2 MP budget: draft mode decodes it at a quarter of the edge."""
    handle = _encoded((4000, 3000), "JPEG")
    with open_bounded(handle, max_pixels=2_000_000, target_edge=512) as image:
        assert image.size == (1000, 750)
        assert image.mode == "RGB"
    assert handle.tell() == 0


def test_the_same_picture_as_png_is_refused() -> None:
    """PNG has no reduced-scale decode, so the full size counts against the budget."""
    with (
        pytest.raises(ImageTooLargeError, match="12000000 pixels"),
        open_bounded(_encoded((4000, 3000), "PNG"), max_pixels=2_000_000, target_edge=512),
    ):
        pass


def test_a_greyscale_decode_of_a_jpeg_is_greyscale() -> None:
    """OCR reads luminance only and asks the decoder for nothing else."""
    with open_bounded(
        _encoded((800, 600), "JPEG"), max_pixels=DEFAULT_MAX_PIXELS, greyscale=True
    ) as image:
        assert image.mode == "L"
        assert image.size == (800, 600)


def test_an_image_within_the_budget_is_loaded_in_full() -> None:
    """No target edge, no reduction."""
    with open_bounded(_encoded((640, 480), "PNG"), max_pixels=640 * 480) as image:
        assert image.size == (640, 480)
        assert image.getpixel((0, 0)) == (128, 128, 128)


def test_bytes_that_are_not_an_image_are_undecodable() -> None:
    """No decoder accepts them."""
    handle = io.BytesIO(b"not an image at all")
    with (
        pytest.raises(ImageUndecodableError),
        open_bounded(handle, max_pixels=DEFAULT_MAX_PIXELS),
    ):
        pass
    assert handle.tell() == 0


def test_a_truncated_image_is_undecodable() -> None:
    """The header opens, the pixel data does not."""
    complete = _encoded((64, 64), "PNG").getvalue()
    with (
        pytest.raises(ImageUndecodableError),
        open_bounded(io.BytesIO(complete[:80]), max_pixels=DEFAULT_MAX_PIXELS),
    ):
        pass


@pytest.mark.parametrize(
    ("size", "expected"), [((8000, 2000), (1000, 250)), ((1500, 6000), (375, 1500))]
)
def test_a_wide_or_tall_jpeg_scales_by_its_long_edge(
    size: tuple[int, int], expected: tuple[int, int]
) -> None:
    """Only the long edge must cover the target, so a panorama scales as far as a square."""
    with open_bounded(
        _encoded(size, "JPEG"), max_pixels=DEFAULT_MAX_PIXELS, target_edge=1000
    ) as image:
        assert image.size == expected
