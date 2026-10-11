"""The local inference tier: text extraction and keyword categorization, on CPU.

What a document is lives in its words. This tier gets the words — from a PDF's text layer
where it has one, by OCR where it is a scan — and matches them against keyword rules for
each document subcategory. It is deliberately modest: a clear majority of matches is
confident, a split or a thin match is not, and anything it cannot settle escalates to the
remote model rather than being filed on a weak guess.

An image whose OCR turns up a page's worth of text is a scanned document, not a photo, and
is classified as one.
"""

import io
import re
from collections.abc import Callable, Mapping
from enum import StrEnum
from typing import BinaryIO, Final

import pytesseract
from PIL import Image, UnidentifiedImageError
from pydantic import Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from curate_d.classification.deterministic import ClassificationInput, TierResult
from curate_d.domain import AuditValue, ClassificationTier, DomainModel, Facts, MediaKind

type OcrEngine = Callable[[Image.Image], str]

_MAX_TEXT: Final = 20_000
_OCR_PAGES: Final = 3
# Words of OCR text that make an image a scanned document rather than a photo.
SCAN_WORD_THRESHOLD: Final = 25


class TextSource(StrEnum):
    """Where extracted text came from."""

    TEXT_LAYER = "text_layer"
    OCR = "ocr"
    PLAIN = "plain"
    NONE = "none"


class ExtractedText(DomainModel):
    """The text a file yielded, and how."""

    text: str
    source: TextSource

    @property
    def words(self) -> list[str]:
        """Lowercased word tokens."""
        return re.findall(r"[\w']+", self.text.lower())


def tesseract(image: Image.Image) -> str:
    """OCR with Tesseract's LSTM engine, one thread (the worker pins OMP_THREAD_LIMIT)."""
    return str(pytesseract.image_to_string(image, config="--oem 1 --psm 3"))


def _ocr_image(handle: BinaryIO, ocr: OcrEngine) -> str:
    """OCR an image file; an image no decoder opens yields no text."""
    handle.seek(0)
    try:
        with Image.open(handle) as image:
            image.load()
            return ocr(image.convert("L"))
    except (UnidentifiedImageError, OSError, ValueError):
        return ""
    finally:
        handle.seek(0)


def _ocr_pdf(handle: BinaryIO, ocr: OcrEngine) -> str:
    """OCR the images embedded in a PDF's first pages: a scanned PDF is pictures of pages."""
    handle.seek(0)
    text: list[str] = []
    try:
        reader = PdfReader(handle)
        for page in reader.pages[:_OCR_PAGES]:
            for embedded in page.images:
                with Image.open(io.BytesIO(embedded.data)) as image:
                    text.append(ocr(image.convert("L")))
    except (PdfReadError, UnidentifiedImageError, OSError, ValueError, KeyError):
        pass
    finally:
        handle.seek(0)
    return "\n".join(text)


def extract_text(
    handle: BinaryIO, item: ClassificationInput, *, ocr: OcrEngine = tesseract
) -> ExtractedText:
    """The text of a document or scan, from the cheapest source that has any."""
    media_type = item.media_type
    if media_type == "application/pdf":
        if item.metadata.text_sample:
            return ExtractedText(text=item.metadata.text_sample, source=TextSource.TEXT_LAYER)
        text = _ocr_pdf(handle, ocr)
        return ExtractedText(
            text=text[:_MAX_TEXT], source=TextSource.OCR if text.strip() else TextSource.NONE
        )
    if media_type.startswith("text/"):
        handle.seek(0)
        text = handle.read(_MAX_TEXT).decode("utf-8", errors="replace")
        handle.seek(0)
        return ExtractedText(text=text, source=TextSource.PLAIN)
    if media_type.startswith("image/"):
        text = _ocr_image(handle, ocr)
        return ExtractedText(
            text=text[:_MAX_TEXT], source=TextSource.OCR if text.strip() else TextSource.NONE
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
            evidence={"text_source": extracted.source.value, "applied": False},
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
