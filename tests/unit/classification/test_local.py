"""The local tier: text from documents and scans, and keyword categorization.

OCR is substituted: what is tested here is which text source is used and what the words
lead to. Real Tesseract runs in the integration suite.
"""

import io
from uuid import uuid4

from PIL import Image

from curate_d.classification.deterministic import (
    ClassificationInput,
    TierResult,
    classify_deterministic,
)
from curate_d.classification.local import (
    ExtractedText,
    TextSource,
    categorize,
    classify_local,
    extract_text,
)
from curate_d.classification.metadata import extract_metadata
from curate_d.content.sniff import sniff_bytes
from curate_d.domain import ContentObjectId, MediaKind
from tests.stubs import media

_INVOICE = [
    "ELECTRIC CO - INVOICE 2026-001",
    "Statement date 1 March 2026",
    "Total amount due: 42.00 EUR",
    "Payment by bank transfer to IBAN PT50 0000",
    "VAT included",
]


def _prepare(content: bytes, path: str) -> tuple[ClassificationInput, TierResult]:
    """The input and deterministic result for `content` at `path`."""
    media_type = sniff_bytes(content[:4096])
    item = ClassificationInput(
        content_object_id=ContentObjectId(uuid4()),
        media_type=media_type,
        size_bytes=len(content),
        paths=(path,),
        metadata=extract_metadata(io.BytesIO(content), media_type, local_timezone=None),
    )
    return item, classify_deterministic(item, local_timezone=None)


def _no_ocr(image: Image.Image) -> str:
    """An OCR engine that must not be reached."""
    raise AssertionError("OCR ran where a text layer was available")


def test_a_pdf_with_a_text_layer_is_read_without_ocr() -> None:
    """The text layer is the cheapest source and is used first."""
    content = media.pdf(_INVOICE)
    item, _ = _prepare(content, "/invoice.pdf")
    extracted = extract_text(io.BytesIO(content), item, ocr=_no_ocr)
    assert extracted.source is TextSource.TEXT_LAYER
    assert "amount due" in extracted.text.lower()


def test_an_invoice_is_confidently_filed_under_finance() -> None:
    """Many finance words and no competing subcategory: confident."""
    content = media.pdf(_INVOICE)
    item, deterministic = _prepare(content, "/invoice.pdf")
    result = classify_local(item, deterministic, extract_text(io.BytesIO(content), item))
    assert (result.facts.category, result.facts.subcategory) == ("Documents", "Finance")
    assert result.confidence >= 0.8
    assert result.evidence["best_rule"] == "Finance"


def test_a_scanned_image_of_a_document_is_reclassified_as_a_document() -> None:
    """An image whose OCR finds a page of text is a scan, not a photo."""
    content = media.scanned_document(_INVOICE)
    item, deterministic = _prepare(content, "/scan0001.png")
    assert deterministic.facts.media_kind is MediaKind.PHOTO

    def ocr(image: Image.Image) -> str:
        """Read back the fixture's lines, as Tesseract would."""
        return "\n".join(_INVOICE * 2)

    extracted = extract_text(io.BytesIO(content), item, ocr=ocr)
    result = classify_local(item, deterministic, extracted)

    assert extracted.source is TextSource.OCR
    assert result.facts.media_kind is MediaKind.DOCUMENT
    assert result.facts.subcategory == "Finance"
    assert result.evidence["scanned_image"] is True


def test_a_photo_with_a_little_text_stays_a_photo() -> None:
    """A street sign in a holiday photo does not make it a document."""
    content = media.photo(camera=None)
    item, deterministic = _prepare(content, "/holiday.jpg")
    extracted = extract_text(io.BytesIO(content), item, ocr=lambda image: "EXIT")
    result = classify_local(item, deterministic, extracted)
    assert result.facts.media_kind is MediaKind.PHOTO
    assert result.confidence == deterministic.confidence


def test_an_ambiguous_document_is_not_confident_and_escalates() -> None:
    """Finance and home words in equal measure: the tier stays below the threshold."""
    extracted = ExtractedText(
        text="rent invoice lease payment tenant total", source=TextSource.TEXT_LAYER
    )
    rule, confidence, _ = categorize(extracted)
    assert rule is not None
    assert confidence < 0.8


def test_a_document_with_no_matching_words_has_no_category_confidence() -> None:
    """Nothing matched is zero confidence, not a default subcategory."""
    extracted = ExtractedText(text="lorem ipsum dolor sit amet", source=TextSource.TEXT_LAYER)
    rule, confidence, _ = categorize(extracted)
    assert rule is None
    assert confidence == 0.0


def test_keywords_match_whole_words_only() -> None:
    """ "totally" is not "total", and "gasket" is not "gas"."""
    extracted = ExtractedText(text="totally gasket", source=TextSource.PLAIN)
    rule, _, _ = categorize(extracted)
    assert rule is None


def test_plain_text_is_read_directly() -> None:
    """A text file's own bytes are its text."""
    content = b"Patient prescription from the clinic"
    item, _ = _prepare(content, "/note.txt")
    extracted = extract_text(io.BytesIO(content), item, ocr=_no_ocr)
    assert extracted.source is TextSource.PLAIN
    rule, _, _ = categorize(extracted)
    assert rule is not None and rule.subcategory == "Medical"


def test_content_with_no_text_source_yields_none() -> None:
    """Audio has no text to extract."""
    content = media.mp3()
    item, _ = _prepare(content, "/song.mp3")
    assert extract_text(io.BytesIO(content), item, ocr=_no_ocr).source is TextSource.NONE


def test_a_scanned_pdf_is_read_by_ocr_of_its_page_images() -> None:
    """A PDF with no text layer is OCRed page by page."""
    scan = Image.open(io.BytesIO(media.scanned_document(["x"]))).convert("RGB")
    buffer = io.BytesIO()
    scan.save(buffer, format="PDF")
    content = buffer.getvalue()
    item, _ = _prepare(content, "/scan.pdf")
    extracted = extract_text(io.BytesIO(content), item, ocr=lambda image: "recibo total iban")
    assert extracted.source is TextSource.OCR
    assert "recibo" in extracted.text
