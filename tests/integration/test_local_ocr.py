"""The local tier with real Tesseract, on a scanned-document fixture."""

import io
from uuid import uuid4

import pytest
from PIL import Image

from curate_d.classification import local
from curate_d.classification.deterministic import ClassificationInput, classify_deterministic
from curate_d.classification.local import (
    ExtractionLimit,
    OcrTimeoutError,
    TextSource,
    classify_local,
    extract_text,
    tesseract,
)
from curate_d.classification.metadata import extract_metadata
from curate_d.content.sniff import sniff_bytes
from curate_d.domain import ContentObjectId, MediaKind
from tests.stubs import media

pytestmark = pytest.mark.integration

_INVOICE = [
    "ELECTRIC COMPANY INVOICE",
    "Statement date 1 March 2026",
    "Total amount due 42.00 EUR",
    "Payment by bank transfer",
    "IBAN PT50 0000 0000 0000",
    "VAT included in the total",
    "Thank you for your payment",
]


def test_a_scanned_invoice_is_read_and_filed_as_a_finance_document() -> None:
    """Tesseract reads the scan; the words make it a confident finance document."""
    content = media.scanned_document(_INVOICE)
    media_type = sniff_bytes(content[:4096])
    item = ClassificationInput(
        content_object_id=ContentObjectId(uuid4()),
        media_type=media_type,
        size_bytes=len(content),
        paths=("/scans/scan0001.png",),
        metadata=extract_metadata(io.BytesIO(content), media_type, local_timezone=None),
    )
    deterministic = classify_deterministic(item, local_timezone=None)

    extracted = extract_text(io.BytesIO(content), item, ocr=tesseract)
    result = classify_local(item, deterministic, extracted)

    assert extracted.source is TextSource.OCR
    assert "invoice" in extracted.text.lower()
    assert result.facts.media_kind is MediaKind.DOCUMENT
    assert (result.facts.category, result.facts.subcategory) == ("Documents", "Finance")
    assert result.confidence > deterministic.confidence


def test_a_long_scanned_pdf_is_read_from_its_first_pages_only() -> None:
    """Real Tesseract on a ten-page scan: three pages read, and the cut is recorded."""
    pages = [
        Image.open(io.BytesIO(media.scanned_document([f"Page {n} of the invoice", *_INVOICE])))
        for n in range(1, 11)
    ]
    buffer = io.BytesIO()
    pages[0].save(buffer, format="PDF", save_all=True, append_images=pages[1:])
    content = buffer.getvalue()
    item = ClassificationInput(
        content_object_id=ContentObjectId(uuid4()),
        media_type="application/pdf",
        size_bytes=len(content),
        paths=("/scans/long.pdf",),
        metadata=extract_metadata(io.BytesIO(content), "application/pdf", local_timezone=None),
    )
    calls: list[int] = []

    def counted(image: Image.Image) -> str:
        """Run Tesseract and count the call."""
        calls.append(image.width * image.height)
        return tesseract(image)

    extracted = extract_text(io.BytesIO(content), item, ocr=counted)

    assert len(calls) == 3
    assert extracted.source is TextSource.OCR
    assert extracted.limited is ExtractionLimit.PAGE_CAP
    assert "page 3" in extracted.text.lower()
    assert "page 4" not in extracted.text.lower()


def test_tesseract_running_past_its_limit_is_killed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The time limit reaches the real process: past it, OCR raises OcrTimeoutError."""
    monkeypatch.setattr(local, "_OCR_TIMEOUT_SECONDS", 0.01)
    scan = Image.open(io.BytesIO(media.scanned_document(_INVOICE * 4)))
    with pytest.raises(OcrTimeoutError):
        tesseract(scan)
