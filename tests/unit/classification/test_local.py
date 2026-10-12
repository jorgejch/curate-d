"""The local tier: text from documents and scans, and keyword categorization.

OCR is substituted: what is tested here is which text source is used and what the words
lead to. Real Tesseract runs in the integration suite.
"""

import io
import zlib
from uuid import uuid4

import pytesseract
import pytest
from PIL import Image
from pypdf.generic import NameObject, NumberObject, StreamObject, TextStringObject

from curate_d.classification import local
from curate_d.classification.deterministic import (
    ClassificationInput,
    TierResult,
    classify_deterministic,
)
from curate_d.classification.local import (
    ExtractedText,
    ExtractionLimit,
    OcrEngine,
    OcrTimeoutError,
    TextSource,
    categorize,
    classify_local,
    extract_text,
    tesseract,
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


class _CountingOcr:
    """An OCR engine that answers from a script of results and counts its calls."""

    def __init__(self, *answers: str | Exception) -> None:
        """Answer the calls in order with `answers`, repeating the last."""
        self.answers = answers
        self.calls = 0

    def __call__(self, image: Image.Image) -> str:
        """Return or raise the next scripted answer."""
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        if isinstance(answer, Exception):
            raise answer
        return answer


def _scanned_pdf(pages: int, size: tuple[int, int] = (300, 400)) -> bytes:
    """Return a PDF of `pages` pages, each one embedded image and no text layer."""
    images = [Image.new("L", size, 255 - page) for page in range(pages)]
    buffer = io.BytesIO()
    images[0].save(buffer, format="PDF", save_all=True, append_images=images[1:])
    return buffer.getvalue()


def _extract(content: bytes, path: str, ocr: OcrEngine, **options: int) -> ExtractedText:
    """Extract the text of `content` found at `path` with `ocr`."""
    item, _ = _prepare(content, path)
    return extract_text(io.BytesIO(content), item, ocr=ocr, **options)


def test_pdf_ocr_stops_once_the_text_reaches_the_cap() -> None:
    """Two long pages fill the cap; the third is never OCRed."""
    ocr = _CountingOcr("word " * 3000)
    extracted = _extract(_scanned_pdf(3), "/scan.pdf", ocr)
    assert ocr.calls == 2
    assert len(extracted.text) == 20_000
    assert extracted.limited is ExtractionLimit.TEXT_CAP


def test_pdf_ocr_reads_only_the_first_pages() -> None:
    """A long scan is judged by its first three pages, and the evidence says so."""
    ocr = _CountingOcr("recibo")
    extracted = _extract(_scanned_pdf(10), "/scan.pdf", ocr)
    assert ocr.calls == 3
    assert extracted.limited is ExtractionLimit.PAGE_CAP


def test_pdf_ocr_reads_no_more_than_the_image_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the cap on embedded images, OCR stops."""
    monkeypatch.setattr(local, "_OCR_IMAGES", 2)
    ocr = _CountingOcr("recibo")
    extracted = _extract(_scanned_pdf(3), "/scan.pdf", ocr)
    assert ocr.calls == 2
    assert extracted.limited is ExtractionLimit.IMAGE_CAP


def test_a_pdf_ocr_timeout_keeps_the_text_read_so_far() -> None:
    """The first page's text survives; OCR stops at the image that ran too long."""
    ocr = _CountingOcr("recibo total", OcrTimeoutError("too long"))
    extracted = _extract(_scanned_pdf(3), "/scan.pdf", ocr)
    assert ocr.calls == 2
    assert extracted.text == "recibo total"
    assert extracted.source is TextSource.OCR
    assert extracted.limited is ExtractionLimit.OCR_TIMEOUT
    assert not extracted.refused


def test_pdf_page_images_over_the_pixel_budget_are_not_decoded() -> None:
    """Declared dimensions are checked before a stream is decoded or OCRed."""
    ocr = _CountingOcr("recibo")
    extracted = _extract(_scanned_pdf(2), "/scan.pdf", ocr, max_pixels=300 * 400 - 1)
    assert ocr.calls == 0
    assert extracted.source is TextSource.NONE
    assert extracted.limited is ExtractionLimit.DECODE_BUDGET
    assert extracted.refused


def test_an_image_over_the_pixel_budget_is_not_ocred() -> None:
    """A refused decode yields no text, a recorded limit, and no OCR call."""
    ocr = _CountingOcr("recibo")
    extracted = _extract(media.scanned_document(_INVOICE), "/scan.png", ocr, max_pixels=1000)
    assert ocr.calls == 0
    assert extracted.limited is ExtractionLimit.DECODE_BUDGET
    assert extracted.refused


def test_an_image_ocr_timeout_is_recorded() -> None:
    """Tesseract killed at its limit leaves no text and says why."""
    ocr = _CountingOcr(OcrTimeoutError("too long"))
    extracted = _extract(media.scanned_document(_INVOICE), "/scan.png", ocr)
    assert extracted.source is TextSource.NONE
    assert extracted.limited is ExtractionLimit.OCR_TIMEOUT


def test_image_ocr_beyond_the_text_cap_is_cut_and_says_so() -> None:
    """Text past the cap is dropped, never silently."""
    extracted = _extract(media.scanned_document(["x"]), "/scan.png", _CountingOcr("a" * 25_000))
    assert len(extracted.text) == 20_000
    assert extracted.limited is ExtractionLimit.TEXT_CAP


def test_plain_text_beyond_the_cap_is_cut_and_says_so() -> None:
    """A long text file is read to the cap and recorded as cut."""
    extracted = _extract(b"words " * 5000, "/long.txt", _no_ocr)
    assert len(extracted.text) == 20_000
    assert extracted.limited is ExtractionLimit.TEXT_CAP
    assert _extract(b"short", "/short.txt", _no_ocr).limited is None


def test_the_evidence_records_which_limit_cut_the_text_short() -> None:
    """Both the applied and the not-applied local tier carry the limit."""
    document = media.pdf(["Minutes"])
    item, deterministic = _prepare(document, "/doc.pdf")
    capped = ExtractedText(text="invoice", source=TextSource.OCR, limited=ExtractionLimit.TEXT_CAP)
    assert classify_local(item, deterministic, capped).evidence["text_limited"] == "text_cap"
    photo, photo_result = _prepare(media.photo(), "/IMG_1.JPG")
    refused = ExtractedText(text="", source=TextSource.NONE, limited=ExtractionLimit.DECODE_BUDGET)
    evidence = classify_local(photo, photo_result, refused).evidence
    assert evidence["text_limited"] == "decode_budget"
    assert evidence["applied"] is False


def test_tesseract_is_bounded_in_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """The time limit is passed down, and pytesseract's timeout becomes OcrTimeoutError."""
    seen: dict[str, object] = {}

    def timing_out(image: Image.Image, **options: object) -> str:
        """Record the options and time out."""
        seen.update(options)
        raise RuntimeError("Tesseract process timeout")

    monkeypatch.setattr(pytesseract, "image_to_string", timing_out)
    with pytest.raises(OcrTimeoutError):
        tesseract(Image.new("L", (10, 10)))
    assert seen["timeout"] == local._OCR_TIMEOUT_SECONDS


def test_other_tesseract_failures_are_not_mistaken_for_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only pytesseract's own timeout message is translated."""

    def failing(image: Image.Image, **options: object) -> str:
        """Fail some other way."""
        raise RuntimeError("tesseract is not installed")

    monkeypatch.setattr(pytesseract, "image_to_string", failing)
    with pytest.raises(RuntimeError, match="not installed"):
        tesseract(Image.new("L", (10, 10)))


def test_ocr_is_handed_at_most_the_ocr_edge() -> None:
    """A large scan is reduced before Tesseract sees it, whatever its format."""
    buffer = io.BytesIO()
    Image.new("L", (6000, 1500), 255).save(buffer, format="PNG")
    sizes: list[tuple[int, int]] = []

    def sizing(image: Image.Image) -> str:
        """Record the size OCR was handed."""
        sizes.append(image.size)
        return ""

    _extract(buffer.getvalue(), "/wide.png", sizing)
    assert sizes == [(4096, 1024)]


def test_a_pdf_image_counts_only_the_size_it_declares_as_numbers() -> None:
    """Missing or non-numeric dimensions count as no pixels; pypdf cannot decode them."""
    stream = StreamObject()
    stream[NameObject("/Width")] = NumberObject(10)
    assert local._pixels(stream) == 0
    stream[NameObject("/Height")] = NumberObject(20)
    assert local._pixels(stream) == 200
    stream[NameObject("/Height")] = TextStringObject("20")
    assert local._pixels(stream) == 0


def test_a_pdf_jpeg_is_sized_by_its_own_header_not_its_declaration() -> None:
    """A 120 MP JPEG declared as 10 x 10 is refused before decode, not decoded in full."""
    content = media.pdf_with_image_stream(
        media.jpeg_header(12_000, 10_000), image_filter="/DCTDecode", width=10, height=10
    )
    ocr = _CountingOcr("recibo")
    extracted = _extract(content, "/scan.pdf", ocr)
    assert ocr.calls == 0
    assert extracted.limited is ExtractionLimit.DECODE_BUDGET
    assert extracted.refused


def test_a_pdf_jpeg_past_pillows_ceiling_is_refused_not_raised() -> None:
    """A JPEG stating 450 MP inside a PDF ends as the decode budget, never an error."""
    content = media.pdf_with_image_stream(
        media.jpeg_header(30_000, 15_000), image_filter="/DCTDecode", width=10, height=10
    )
    ocr = _CountingOcr("recibo")
    extracted = _extract(content, "/scan.pdf", ocr)
    assert ocr.calls == 0
    assert extracted.limited is ExtractionLimit.DECODE_BUDGET


def test_pillows_ceiling_reached_during_a_pdf_decode_is_the_decode_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A size `_pixels` could not see, refused by Pillow while decoding, is recorded too."""
    monkeypatch.setattr(local, "_pixels", lambda xobject: 0)
    content = media.pdf_with_image_stream(
        media.jpeg_header(30_000, 15_000), image_filter="/DCTDecode", width=10, height=10
    )
    ocr = _CountingOcr("recibo")
    extracted = _extract(content, "/scan.pdf", ocr)
    assert ocr.calls == 0
    assert extracted.limited is ExtractionLimit.DECODE_BUDGET


@pytest.mark.parametrize(
    ("stream", "image_filter"),
    [
        # Inflates past pypdf's output limit: LimitReachedError.
        pytest.param(b"", "/FlateDecode", id="flate-bomb"),
        # Needs jbig2dec, which the worker image does not ship: DependencyError.
        pytest.param(b"\x00" * 64, "/JBIG2Decode", id="jbig2"),
    ],
)
def test_a_pdf_image_pypdf_refuses_yields_no_text_rather_than_an_error(
    stream: bytes, image_filter: str
) -> None:
    """pypdf's own refusals skip the image like any undecodable one."""
    if image_filter == "/FlateDecode":
        stream = zlib.compress(b"\x00" * 80_000_000)
    content = media.pdf_with_image_stream(
        stream, image_filter=image_filter, width=2000, height=2000
    )
    ocr = _CountingOcr("recibo")
    extracted = _extract(content, "/scan.pdf", ocr)
    assert ocr.calls == 0
    assert extracted.source is TextSource.NONE
    assert extracted.limited is None


def test_a_pdf_whose_text_pypdf_refuses_to_inflate_yields_no_metadata() -> None:
    """A content stream past pypdf's decompression limit reads as a broken PDF."""
    content = media.pdf_with_image_stream(
        b"",
        image_filter="/DCTDecode",
        width=1,
        height=1,
        content=zlib.compress(b" " * 80_000_000),
        content_filter="/FlateDecode",
    )
    metadata = extract_metadata(io.BytesIO(content), "application/pdf", local_timezone=None)
    assert metadata.page_count is None
    assert metadata.text_sample is None
