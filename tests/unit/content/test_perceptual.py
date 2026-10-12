"""Perceptual fingerprints survive re-encoding and rescaling, and separate different pictures."""

import io

import pytest
from PIL import Image, ImageDraw

from curate_d.content.perceptual import (
    MATCH_THRESHOLD_BITS,
    ImageTooLargeError,
    ImageUndecodableError,
    is_image,
    perceptual_fingerprint,
    same_picture,
)


def _scene(seed: int, size: tuple[int, int] = (640, 480)) -> Image.Image:
    """A deterministic picture: shapes placed by `seed`."""
    image = Image.new("RGB", size, (20 + seed * 7 % 200, 40, 90))
    draw = ImageDraw.Draw(image)
    width, height = size
    for i in range(12):
        x = (seed * 97 + i * 53) % width
        y = (seed * 61 + i * 29) % height
        shade = (seed * 31 + i * 17) % 255
        draw.ellipse((x, y, x + width // 5, y + height // 4), fill=(shade, 255 - shade, i * 20))
    draw.rectangle((width // 3, height // 3, width // 2, height // 2), fill=(250, 250, 250))
    return image


def _encoded(image: Image.Image, fmt: str, **options: int) -> io.BytesIO:
    """The image saved in `fmt`."""
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **options)
    buffer.seek(0)
    return buffer


def test_the_same_picture_at_different_encodings_and_scales_matches() -> None:
    """PNG, low-quality JPEG and a half-size JPEG of one scene stay within threshold."""
    scene = _scene(3)
    reference = perceptual_fingerprint(_encoded(scene, "PNG"))
    variants = [
        _encoded(scene, "JPEG", quality=40),
        _encoded(scene.resize((320, 240)), "JPEG", quality=85),
        _encoded(scene.resize((1280, 960)), "WEBP", quality=60),
    ]
    for variant in variants:
        fingerprint = perceptual_fingerprint(variant)
        assert same_picture(reference, fingerprint), reference.distance_to(fingerprint)


def test_unrelated_pictures_do_not_match() -> None:
    """Different scenes land well outside the threshold."""
    fingerprints = [perceptual_fingerprint(_encoded(_scene(seed), "PNG")) for seed in (1, 5, 9)]
    for i, a in enumerate(fingerprints):
        for b in fingerprints[i + 1 :]:
            assert a.distance_to(b) > MATCH_THRESHOLD_BITS


def test_the_handle_is_left_at_the_start() -> None:
    """Callers can read the content again without seeking."""
    handle = _encoded(_scene(2), "PNG")
    perceptual_fingerprint(handle)
    assert handle.tell() == 0


def test_the_fingerprint_fits_a_signed_bigint() -> None:
    """Every value stores in PostgreSQL's bigint."""
    for seed in range(8):
        value = perceptual_fingerprint(_encoded(_scene(seed), "PNG")).value
        assert -(2**63) <= value < 2**63


def test_bytes_that_are_not_an_image_are_refused() -> None:
    """A decoder refusing the content is reported as undecodable."""
    with pytest.raises(ImageUndecodableError):
        perceptual_fingerprint(io.BytesIO(b"\xff\xd8\xff not really a jpeg"))


def test_a_jpeg_decoded_at_a_reduced_scale_matches_its_full_decode() -> None:
    """Draft mode does not move the fingerprint out of the match threshold."""
    encoded = _encoded(_scene(4, (4000, 3000)), "JPEG", quality=90)
    reduced = perceptual_fingerprint(encoded)
    full = perceptual_fingerprint(_encoded(Image.open(encoded).convert("RGB"), "PNG"))
    assert same_picture(reduced, full), reduced.distance_to(full)


def test_an_image_over_the_pixel_budget_is_refused() -> None:
    """A PNG has no reduced decode; over the budget it gets no fingerprint."""
    with pytest.raises(ImageTooLargeError):
        perceptual_fingerprint(_encoded(_scene(1), "PNG"), max_pixels=640 * 480 - 1)


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [("image/jpeg", True), ("image/heic", True), ("application/pdf", False), ("text/plain", False)],
)
def test_only_images_get_a_perceptual_fingerprint(media_type: str, expected: bool) -> None:
    """Content with no visual form has none."""
    assert is_image(media_type) is expected
