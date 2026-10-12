"""The CPU profile's part of classification: the cheap tiers, and the remote tier's input.

Everything that decodes content happens here, on the profile built to decode untrusted
bytes: metadata, OCR, and the downscaled image the remote model is shown. The I/O profile,
which makes the remote call beside the provider credentials, only ever receives what this
produces.

Every decode stays within the CPU worker's pixel budget. Content a limit kept the cheap
tiers from reading, and that they could not settle, goes to review as `resource_limit`
when the remote tier would be shown neither text nor an image of it: a remote call that
sees only names is not spent on content nobody could look at.
"""

import base64
import io
from typing import BinaryIO, Final

from pydantic import Field

from curate_d.classification.deterministic import (
    ClassificationInput,
    TierResult,
    classify_deterministic,
)
from curate_d.classification.gating import GateDecision, GateVerdict, run_tiers
from curate_d.classification.local import (
    ExtractedText,
    ExtractionLimit,
    OcrEngine,
    TextSource,
    classify_local,
    extract_text,
    tesseract,
)
from curate_d.classification.metadata import EmbeddedMetadata, extract_metadata
from curate_d.classification.remote import RemoteRequest
from curate_d.content.decode import (
    DEFAULT_MAX_PIXELS,
    ImageTooLargeError,
    ImageUndecodableError,
    open_bounded,
)
from curate_d.domain import AuditValue, ContentObjectId, DomainModel, ReviewReason, Taxonomy

THUMBNAIL_EDGE: Final = 1024
_EXCERPT_CHARS: Final = 3000


class Prepared(DomainModel):
    """What the cheap tiers decided, and the remote tier's input if they escalated."""

    decision: GateDecision
    attempts: tuple[TierResult, ...]
    inputs: dict[str, AuditValue] = Field(default_factory=dict)
    remote: RemoteRequest | None = None


def thumbnail_jpeg(handle: BinaryIO, *, max_pixels: int = DEFAULT_MAX_PIXELS) -> str | None:
    """Return a downscaled JPEG of an image, base64-encoded.

    Returns:
        The JPEG, or None if the image does not decode or would decode to more than
        `max_pixels` pixels.
    """
    try:
        with open_bounded(handle, max_pixels=max_pixels, target_edge=THUMBNAIL_EDGE) as image:
            image.thumbnail((THUMBNAIL_EDGE, THUMBNAIL_EDGE))
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="JPEG", quality=80)
    except (ImageTooLargeError, ImageUndecodableError, OSError, ValueError):
        return None
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _resource_limited(decision: GateDecision, limit: ExtractionLimit) -> GateDecision:
    """Return `decision` sent to review because a CPU worker limit kept the content unread."""
    return GateDecision(
        verdict=GateVerdict.REVIEW,
        result=decision.result,
        reason=ReviewReason.RESOURCE_LIMIT,
        detail=(
            f"a CPU worker limit ({limit.value}) kept the content from being read, and the "
            f"cheap tiers reached only {decision.result.tier.value} at "
            f"{decision.result.confidence}"
        ),
    )


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
    max_pixels: int = DEFAULT_MAX_PIXELS,
) -> Prepared:
    """Run the deterministic and local tiers and decide what happens next.

    `handle` is None when the staged content is no longer available: the tiers then work
    from the media type and names alone, and the evidence says so. `max_pixels` bounds
    every image decode. Synchronous and CPU-bound throughout; callers on an event loop run
    it in a thread.
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
            extract_text(handle, item, ocr=ocr, max_pixels=max_pixels)
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
    limited = extracted[0].limited if extracted else None
    refused = limited is not None and extracted[0].refused
    if limited is not None:
        inputs["content_limited"] = limited.value
    if decision.verdict is not GateVerdict.ESCALATE:
        if limited is not None and refused and decision.reason is ReviewReason.LOW_CONFIDENCE:
            decision = _resource_limited(decision, limited)
        return Prepared(decision=decision, attempts=tuple(attempts), inputs=inputs)
    text_excerpt = (extracted[0].text[:_EXCERPT_CHARS] or None) if extracted else None
    image = (
        thumbnail_jpeg(handle, max_pixels=max_pixels)
        if handle is not None and media_type.startswith("image/")
        else None
    )
    if limited is not None and refused and image is None:
        return Prepared(
            decision=_resource_limited(decision, limited),
            attempts=tuple(attempts),
            inputs=inputs,
        )
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
        text_excerpt=text_excerpt,
        image_jpeg_base64=image,
    )
    return Prepared(decision=decision, attempts=tuple(attempts), inputs=inputs, remote=remote)
