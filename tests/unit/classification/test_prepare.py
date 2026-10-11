"""The CPU profile's classification step over fixture content."""

import base64
import io
from datetime import datetime
from uuid import uuid4

from PIL import Image

from curate_d.classification.gating import GateVerdict
from curate_d.classification.prepare import THUMBNAIL_EDGE, Prepared, prepare
from curate_d.content.sniff import sniff_bytes
from curate_d.domain import SEED_TAXONOMY, ClassificationTier, ContentObjectId, ReviewReason
from tests.stubs import media


def _run(
    content: bytes | None, path: str, *, remote: bool = True, media_type: str | None = None
) -> Prepared:
    """Prepare `content` found at `path`."""
    return prepare(
        None if content is None else io.BytesIO(content),
        content_object_id=ContentObjectId(uuid4()),
        media_type=media_type or sniff_bytes((content or b"")[:4096]),
        size_bytes=len(content or b""),
        paths=(path,),
        taxonomy=SEED_TAXONOMY,
        threshold=0.8,
        local_timezone="UTC",
        remote_available=remote,
        ocr=lambda image: "",
    )


def test_a_camera_photo_is_settled_by_the_deterministic_tier() -> None:
    """Accepted without the local tier running."""
    prepared = _run(
        media.photo(taken=datetime(2026, 1, 1, 12, 0)),  # noqa: DTZ001 - EXIF is naive
        "/IMG_1.JPG",
    )
    assert prepared.decision.verdict is GateVerdict.ACCEPT
    assert [a.tier for a in prepared.attempts] == [ClassificationTier.DETERMINISTIC]
    assert prepared.remote is None


def test_an_invoice_is_settled_by_the_local_tier() -> None:
    """The deterministic tier escalates; the text settles it."""
    content = media.pdf(["INVOICE", "Total amount due", "Payment by bank transfer", "IBAN", "VAT"])
    prepared = _run(content, "/doc.pdf")
    assert prepared.decision.verdict is GateVerdict.ACCEPT
    assert prepared.decision.result.tier is ClassificationTier.LOCAL_INFERENCE


def test_an_unsettled_document_is_prepared_for_the_remote_tier() -> None:
    """The remote request carries the text excerpt and what the cheap tiers found."""
    content = media.pdf(["Minutes of the neighbourhood association picnic"])
    prepared = _run(content, "/doc.pdf")
    assert prepared.decision.verdict is GateVerdict.ESCALATE
    remote = prepared.remote
    assert remote is not None
    assert "picnic" in (remote.text_excerpt or "")
    assert remote.hints["media_kind"] == "document"
    assert remote.image_jpeg_base64 is None


def test_an_image_bound_for_the_remote_tier_carries_a_downscaled_jpeg() -> None:
    """Decoding and resizing happen here, not beside the provider credentials."""
    content = media.photo(camera=None, size=(3000, 2000))
    prepared = _run(content, "/holiday.jpg")
    remote = prepared.remote
    assert remote is not None
    assert remote.image_jpeg_base64 is not None
    with Image.open(io.BytesIO(base64.b64decode(remote.image_jpeg_base64))) as thumbnail:
        assert max(thumbnail.size) == THUMBNAIL_EDGE
        assert thumbnail.format == "JPEG"


def test_without_a_remote_tier_an_unsettled_item_goes_to_review() -> None:
    """Nothing left to escalate to."""
    prepared = _run(media.pdf(["picnic"]), "/doc.pdf", remote=False)
    assert prepared.decision.verdict is GateVerdict.REVIEW
    assert prepared.decision.reason is ReviewReason.LOW_CONFIDENCE


def test_missing_content_is_classified_from_names_and_says_so() -> None:
    """Evicted content degrades to what the names and type establish."""
    prepared = _run(None, "/Screenshot 2026-01-02 at 10.11.12.png", media_type="image/png")
    assert prepared.inputs["content_available"] is False
    assert prepared.decision.result.facts.subcategory == "Screenshots"
