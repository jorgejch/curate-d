"""The CPU profile's part of classification: the cheap tiers, and the remote tier's input.

Everything that decodes content happens here, on the profile built to decode untrusted
bytes: metadata, OCR, and the downscaled image the remote model is shown. The I/O profile,
which makes the remote call beside the provider credentials, only ever receives what this
produces.
"""

import base64
import io
from typing import BinaryIO, Final

from PIL import Image, UnidentifiedImageError
from pydantic import Field

from curate_d.classification.deterministic import (
    ClassificationInput,
    TierResult,
    classify_deterministic,
)
from curate_d.classification.gating import GateDecision, GateVerdict, run_tiers
from curate_d.classification.local import (
    ExtractedText,
    OcrEngine,
    TextSource,
    classify_local,
    extract_text,
    tesseract,
)
from curate_d.classification.metadata import EmbeddedMetadata, extract_metadata
from curate_d.classification.remote import RemoteRequest
from curate_d.domain import AuditValue, ContentObjectId, DomainModel, Taxonomy

THUMBNAIL_EDGE: Final = 1024
_EXCERPT_CHARS: Final = 3000


class Prepared(DomainModel):
    """What the cheap tiers decided, and the remote tier's input if they escalated."""

    decision: GateDecision
    attempts: tuple[TierResult, ...]
    inputs: dict[str, AuditValue] = Field(default_factory=dict)
    remote: RemoteRequest | None = None


def thumbnail_jpeg(handle: BinaryIO) -> str | None:
    """A downscaled JPEG of an image, base64-encoded; None if it does not decode."""
    handle.seek(0)
    try:
        with Image.open(handle) as image:
            image.thumbnail((THUMBNAIL_EDGE, THUMBNAIL_EDGE))
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="JPEG", quality=80)
    except (UnidentifiedImageError, OSError, ValueError):
        return None
    finally:
        handle.seek(0)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def prepare(
    handle: BinaryIO | None,
    *,
    content_object_id: ContentObjectId,
    media_type: str,
    size_bytes: int,
    paths: tuple[str, ...],
    taxonomy: Taxonomy,
    threshold: float,
    local_timezone: str | None,
    remote_available: bool,
    ocr: OcrEngine = tesseract,
) -> Prepared:
    """Run the deterministic and local tiers and decide what happens next.

    `handle` is None when the staged content is no longer available: the tiers then work
    from the media type and names alone, and the evidence says so. Synchronous and
    CPU-bound throughout; callers on an event loop run it in a thread.
    """
    metadata = (
        extract_metadata(handle, media_type, local_timezone=local_timezone)
        if handle is not None
        else EmbeddedMetadata()
    )
    item = ClassificationInput(
        content_object_id=content_object_id,
        media_type=media_type,
        size_bytes=size_bytes,
        paths=paths,
        metadata=metadata,
    )
    extracted: list[ExtractedText] = []

    def deterministic() -> TierResult:
        """The deterministic tier."""
        return classify_deterministic(item, local_timezone=local_timezone)

    def local() -> TierResult:
        """The local tier, over the content's text."""
        text = (
            extract_text(handle, item, ocr=ocr)
            if handle is not None
            else ExtractedText(text="", source=TextSource.NONE)
        )
        extracted.append(text)
        return classify_local(item, deterministic(), text)

    decision, attempts = run_tiers(
        [deterministic, local],
        taxonomy,
        threshold=threshold,
        more_tiers_follow=remote_available,
    )
    inputs: dict[str, AuditValue] = {
        "media_type": media_type,
        "names": ", ".join(paths),
        "content_available": handle is not None,
    }
    remote = None
    if decision.verdict is GateVerdict.ESCALATE:
        best = decision.result.facts
        remote = RemoteRequest(
            content_object_id=content_object_id,
            media_type=media_type,
            paths=paths,
            hints={
                "media_kind": best.media_kind.value,
                "category": best.category,
                "subcategory": best.subcategory,
                "title": best.title,
                "captured_at": None if best.captured_at is None else best.captured_at.isoformat(),
            },
            text_excerpt=(extracted[0].text[:_EXCERPT_CHARS] or None) if extracted else None,
            image_jpeg_base64=(
                thumbnail_jpeg(handle)
                if handle is not None and media_type.startswith("image/")
                else None
            ),
        )
    return Prepared(decision=decision, attempts=tuple(attempts), inputs=inputs, remote=remote)
