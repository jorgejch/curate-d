"""The local inference tier: text extraction and keyword categorization, on CPU.

What a document is lives in its words. This tier gets the words — from a PDF's text layer
where it has one, by OCR where it is a scan — and matches them against keyword rules for
each document subcategory. It is deliberately modest: a clear majority of matches is
confident, a split or a thin match is not, and anything it cannot settle escalates to the
remote model rather than being filed on a weak guess.

An image whose OCR turns up a page's worth of text is a scanned document, not a photo, and
is classified as one.

Extraction is bounded for the CPU worker's budget: at most `_MAX_TEXT` characters, the
images of the first `_OCR_PAGES` pages of a PDF and no more than `_OCR_IMAGES` of them,
`_OCR_TIMEOUT_SECONDS` of Tesseract per image, and every decode within the pixel budget.
Whichever limit cut the text short is recorded as `limited` and carried into the evidence,
so a truncation is never silent.
"""

import re
from collections.abc import Callable, Iterator, Mapping
from enum import StrEnum
from typing import BinaryIO, Final

import pytesseract
from PIL import Image
from pydantic import Field
from pypdf import PageObject, PdfReader
from pypdf.errors import PdfReadError
from pypdf.generic import DictionaryObject, StreamObject

from curate_d.classification.deterministic import ClassificationInput, TierResult
from curate_d.content.decode import (
    DEFAULT_MAX_PIXELS,
    ImageTooLargeError,
    ImageUndecodableError,
    open_bounded,
)
from curate_d.domain import AuditValue, ClassificationTier, DomainModel, Facts, MediaKind

type OcrEngine = Callable[[Image.Image], str]

_MAX_TEXT: Final = 20_000
_OCR_PAGES: Final = 3
# Embedded images OCRed per PDF, across its pages: a scan is one image a page, so this
# leaves room for a split page without letting a PDF of many small images run long.
_OCR_IMAGES: Final = 6
# Seconds Tesseract may spend on one image before it is killed.
_OCR_TIMEOUT_SECONDS: Final = 60
# Long edge OCR reads at: an A4 page at about 350 dpi, legible and within Tesseract's memory.
_OCR_EDGE: Final = 4096
# Form XObjects nested deeper than this are not searched for page images.
_FORM_DEPTH: Final = 2
# Words of OCR text that make an image a scanned document rather than a photo.
SCAN_WORD_THRESHOLD: Final = 25


class OcrTimeoutError(RuntimeError):
    """The OCR engine ran past its time limit on one image."""


class TextSource(StrEnum):
    """Where extracted text came from."""

    TEXT_LAYER = "text_layer"
    OCR = "ocr"
    PLAIN = "plain"
    NONE = "none"


class ExtractionLimit(StrEnum):
    """Which CPU worker limit cut text extraction short."""

    TEXT_CAP = "text_cap"
    """The text reached the character cap; what followed was not read."""

    PAGE_CAP = "page_cap"
    """The PDF had more pages than are OCRed."""

    IMAGE_CAP = "image_cap"
    """The PDF had more embedded images than are OCRed."""

    OCR_TIMEOUT = "ocr_timeout"
    """OCR of an image ran past its time limit."""

    DECODE_BUDGET = "decode_budget"
    """An image would decode to more pixels than the budget allows, so it was not read."""


class ExtractedText(DomainModel):
    """The text a file yielded, how, and which limit, if any, cut it short."""

    text: str
    source: TextSource
    limited: ExtractionLimit | None = None

    @property
    def words(self) -> list[str]:
        """Return the lowercased word tokens."""
        return re.findall(r"[\w']+", self.text.lower())

    @property
    def refused(self) -> bool:
        """Return whether a CPU worker limit kept the tier from reading any text at all.

        Only a decode refused for the pixel budget, or OCR killed at its time limit, counts:
        the caps on text, pages and images apply after something has been read.
        """
        return (
            self.limited in {ExtractionLimit.DECODE_BUDGET, ExtractionLimit.OCR_TIMEOUT}
            and not self.text.strip()
        )


def tesseract(image: Image.Image) -> str:
    """OCR with Tesseract's LSTM engine, one thread (the worker pins OMP_THREAD_LIMIT).

    Raises:
        OcrTimeoutError: Tesseract ran past `_OCR_TIMEOUT_SECONDS` and was killed.
    """
    try:
        return str(
            pytesseract.image_to_string(
                image, config="--oem 1 --psm 3", timeout=_OCR_TIMEOUT_SECONDS
            )
        )
    except RuntimeError as error:
        # pytesseract signals its own timeout with this exact message and no subclass.
        if str(error) == "Tesseract process timeout":
            raise OcrTimeoutError(f"OCR ran past {_OCR_TIMEOUT_SECONDS} s") from None
        raise


def _for_ocr(image: Image.Image) -> Image.Image:
    """Return the image in greyscale, its long edge at most `_OCR_EDGE`.

    Tesseract's memory grows with the pixels it is handed, and it runs beside the decode
    that produced them; a page scanned finer than that edge reads as well at it.
    """
    grey = image.convert("L")
    if max(grey.size) > _OCR_EDGE:
        grey.thumbnail((_OCR_EDGE, _OCR_EDGE))
    return grey


def _ocr_image(
    handle: BinaryIO, ocr: OcrEngine, max_pixels: int
) -> tuple[str, ExtractionLimit | None]:
    """OCR an image file; an image no decoder opens yields no text.

    Returns:
        The text, and the limit that cut it short, if one did.
    """
    try:
        with open_bounded(
            handle, max_pixels=max_pixels, target_edge=_OCR_EDGE, greyscale=True
        ) as image:
            grey = _for_ocr(image)
        # The full decode is released before Tesseract starts beside it.
        return ocr(grey), None
    except ImageTooLargeError:
        return "", ExtractionLimit.DECODE_BUDGET
    except ImageUndecodableError:
        return "", None
    except OcrTimeoutError:
        return "", ExtractionLimit.OCR_TIMEOUT


def _page_images(resources: object, depth: int = 0) -> Iterator[StreamObject]:
    """Yield the image XObjects a page's resources draw, through nested forms."""
    if not isinstance(resources, DictionaryObject):
        return
    xobjects = resources.get("/XObject")
    xobjects = xobjects.get_object() if xobjects is not None else None
    if not isinstance(xobjects, DictionaryObject):
        return
    for reference in xobjects.values():
        xobject = reference.get_object()
        if not isinstance(xobject, StreamObject):
            continue
        subtype = xobject.get("/Subtype")
        if subtype == "/Image":
            yield xobject
        elif subtype == "/Form" and depth < _FORM_DEPTH:
            nested = xobject.get("/Resources")
            yield from _page_images(nested.get_object() if nested is not None else None, depth + 1)


def _pixels(xobject: StreamObject) -> int:
    """Return the pixel count an image XObject declares, without decoding it.

    An image declaring no usable size counts as none: pypdf cannot decode it either.
    """
    try:
        width, height = xobject["/Width"], xobject["/Height"]
    except KeyError:
        return 0
    # pypdf's NumberObject is an int; anything else is no usable size.
    if not isinstance(width, int) or not isinstance(height, int):
        return 0
    return width * height


def _ocr_pdf(
    handle: BinaryIO, ocr: OcrEngine, max_pixels: int
) -> tuple[str, ExtractionLimit | None]:
    """OCR the images embedded in a PDF's first pages: a scanned PDF is pictures of pages.

    Stops once the text reaches the cap, after `_OCR_IMAGES` images, or at the first OCR
    timeout. An image whose declared size is over the pixel budget is skipped before its
    stream is decoded.

    Returns:
        The text, and the limit that cut it short, if one did.
    """
    handle.seek(0)
    text: list[str] = []
    length = 0
    read = 0
    limited: ExtractionLimit | None = None

    def images(reader: PdfReader) -> Iterator[StreamObject]:
        """Yield the image XObjects of the first pages, in page order."""
        page: PageObject
        for page in reader.pages[:_OCR_PAGES]:
            resources = page.get("/Resources")
            yield from _page_images(resources.get_object() if resources is not None else None)

    try:
        reader = PdfReader(handle)
        if len(reader.pages) > _OCR_PAGES:
            limited = ExtractionLimit.PAGE_CAP
        for xobject in images(reader):
            if length >= _MAX_TEXT:
                limited = ExtractionLimit.TEXT_CAP
                break
            if read >= _OCR_IMAGES:
                limited = ExtractionLimit.IMAGE_CAP
                break
            if _pixels(xobject) > max_pixels:
                limited = ExtractionLimit.DECODE_BUDGET
                continue
            read += 1
            try:
                decoded: Image.Image | None = xobject.decode_as_image()
                if decoded is None:
                    continue
                with decoded as image:
                    grey = _for_ocr(image)
                page_text = ocr(grey)
            except OcrTimeoutError:
                limited = ExtractionLimit.OCR_TIMEOUT
                break
            except (PdfReadError, OSError, ValueError, KeyError, TypeError, NotImplementedError):
                # One image pypdf or Pillow cannot decode does not end the others' OCR.
                continue
            text.append(page_text)
            length += len(page_text) + 1
    except (PdfReadError, OSError, ValueError, KeyError, TypeError, AttributeError):
        # A malformed PDF yields what was read before it broke, never an error.
        pass
    finally:
        handle.seek(0)
    joined = "\n".join(text)
    if len(joined) > _MAX_TEXT:
        limited = ExtractionLimit.TEXT_CAP
    return joined[:_MAX_TEXT], limited


def extract_text(
    handle: BinaryIO,
    item: ClassificationInput,
    *,
    ocr: OcrEngine = tesseract,
    max_pixels: int = DEFAULT_MAX_PIXELS,
) -> ExtractedText:
    """Return the text of a document or scan, from the cheapest source that has any.

    Args:
        handle: The content.
        item: What is known about it.
        ocr: The OCR engine.
        max_pixels: Most pixels one image decode for OCR may hold.
    """
    media_type = item.media_type
    if media_type == "application/pdf":
        if item.metadata.text_sample:
            return ExtractedText(text=item.metadata.text_sample, source=TextSource.TEXT_LAYER)
        text, limited = _ocr_pdf(handle, ocr, max_pixels)
        return ExtractedText(
            text=text,
            source=TextSource.OCR if text.strip() else TextSource.NONE,
            limited=limited,
        )
    if media_type.startswith("text/"):
        handle.seek(0)
        raw = handle.read(_MAX_TEXT + 1)
        handle.seek(0)
        return ExtractedText(
            text=raw[:_MAX_TEXT].decode("utf-8", errors="replace"),
            source=TextSource.PLAIN,
            limited=ExtractionLimit.TEXT_CAP if len(raw) > _MAX_TEXT else None,
        )
    if media_type.startswith("image/"):
        text, limited = _ocr_image(handle, ocr, max_pixels)
        if len(text) > _MAX_TEXT:
            limited = ExtractionLimit.TEXT_CAP
        return ExtractedText(
            text=text[:_MAX_TEXT],
            source=TextSource.OCR if text.strip() else TextSource.NONE,
            limited=limited,
        )
    return ExtractedText(text="", source=TextSource.NONE)


class KeywordRule(DomainModel):
    """Words whose presence points at one document subcategory."""

    category: str
    subcategory: str
    keywords: frozenset[str] = Field(description="Lowercase words or short phrases.")


# English and Portuguese keywords.
DOCUMENT_RULES: Final = (
    KeywordRule(
        category="Documents",
        subcategory="Finance",
        keywords=frozenset(
            {
                "invoice",
                "receipt",
                "statement",
                "bank",
                "iban",
                "payment",
                "total",
                "amount due",
                "tax",
                "vat",
                "balance",
                "fatura",
                "recibo",
                "pagamento",
                "imposto",
                "extrato",
                "nif",
            }
        ),
    ),
    KeywordRule(
        category="Documents",
        subcategory="Identity",
        keywords=frozenset(
            {
                "passport",
                "identity card",
                "driver's license",
                "driving licence",
                "birth certificate",
                "nationality",
                "date of birth",
                "passaporte",
                "cartão de cidadão",
                "certidão de nascimento",
                "nacionalidade",
            }
        ),
    ),
    KeywordRule(
        category="Documents",
        subcategory="Medical",
        keywords=frozenset(
            {
                "patient",
                "diagnosis",
                "prescription",
                "hospital",
                "clinic",
                "doctor",
                "dosage",
                "laboratory",
                "paciente",
                "receita",
                "diagnóstico",
                "médico",
                "análises",
            }
        ),
    ),
    KeywordRule(
        category="Documents",
        subcategory="Work",
        keywords=frozenset(
            {
                "employment",
                "employee",
                "employer",
                "contract",
                "salary",
                "payslip",
                "meeting",
                "agenda",
                "curriculum",
                "resume",
                "contrato",
                "salário",
                "reunião",
                "empresa",
            }
        ),
    ),
    KeywordRule(
        category="Documents",
        subcategory="Home",
        keywords=frozenset(
            {
                "lease",
                "rent",
                "tenant",
                "landlord",
                "mortgage",
                "electricity",
                "water",
                "gas",
                "utility",
                "insurance",
                "arrendamento",
                "renda",
                "condomínio",
                "eletricidade",
                "seguro",
            }
        ),
    ),
)


def _matches(text: str, rule: KeywordRule) -> set[str]:
    """The rule's keywords present in `text` as whole words or phrases."""
    return {
        keyword
        for keyword in rule.keywords
        if re.search(rf"(?<![\w]){re.escape(keyword)}(?![\w])", text)
    }


def categorize(
    extracted: ExtractedText, rules: tuple[KeywordRule, ...] = DOCUMENT_RULES
) -> tuple[KeywordRule | None, float, Mapping[str, AuditValue]]:
    """The best-matching rule, a confidence, and the evidence for the audit trail.

    Confidence grows with the number of distinct keywords matched and falls when a second
    subcategory matches nearly as well: a document that is equally a bank statement and a
    lease is not one the tier should decide.
    """
    lowered = extracted.text.lower()
    scored = sorted(
        ((len(_matches(lowered, rule)), rule) for rule in rules),
        key=lambda pair: (-pair[0], pair[1].subcategory),
    )
    best_hits, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0
    evidence: dict[str, AuditValue] = {
        "text_source": extracted.source.value,
        "text_limited": None if extracted.limited is None else extracted.limited.value,
        "words": len(extracted.words),
        "best_rule": best.subcategory if best_hits else None,
        "best_hits": best_hits,
        "runner_up_hits": runner_up,
    }
    if best_hits == 0:
        return None, 0.0, evidence
    margin = best_hits - runner_up
    confidence = min(0.95, 0.45 + 0.1 * best_hits + 0.05 * margin)
    if margin < 2:
        confidence = min(confidence, 0.6)
    return best, round(confidence, 3), evidence


def classify_local(
    item: ClassificationInput, deterministic: TierResult, extracted: ExtractedText
) -> TierResult:
    """Refine the deterministic facts with what the content's text says."""
    base = deterministic.facts
    is_scan = (
        base.media_kind is MediaKind.PHOTO
        and extracted.source is TextSource.OCR
        and len(extracted.words) >= SCAN_WORD_THRESHOLD
    )
    if base.media_kind is not MediaKind.DOCUMENT and not is_scan:
        # Nothing this tier knows how to judge: no better than what it was handed.
        return TierResult(
            tier=ClassificationTier.LOCAL_INFERENCE,
            facts=base,
            confidence=deterministic.confidence,
            evidence={
                "text_source": extracted.source.value,
                "text_limited": None if extracted.limited is None else extracted.limited.value,
                "applied": False,
            },
        )
    rule, confidence, evidence = categorize(extracted)
    facts = Facts.model_validate(
        base.model_dump()
        | {
            "media_kind": MediaKind.DOCUMENT,
            "category": "Documents",
            "subcategory": None if rule is None else rule.subcategory,
        }
    )
    return TierResult(
        tier=ClassificationTier.LOCAL_INFERENCE,
        facts=facts,
        confidence=confidence,
        evidence={**evidence, "scanned_image": is_scan},
    )
