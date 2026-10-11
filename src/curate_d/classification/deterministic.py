"""The deterministic tier: facts from media type, extension, metadata and file names.

The cheapest tier and the one that should settle most of a personal library: a camera
photo says it is one in its EXIF, a screenshot is named like one, an archive is an
archive. Each rule states how confident its conclusion is; a document, whose category
lives in its text, gets a low score here and escalates.
"""

import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import PurePosixPath
from typing import Final
from zoneinfo import ZoneInfo

from pydantic import Field

from curate_d.classification.metadata import EmbeddedMetadata
from curate_d.domain import (
    AuditValue,
    ClassificationTier,
    ContentObjectId,
    DomainModel,
    Facts,
    MediaKind,
)


class ClassificationInput(DomainModel):
    """Everything the cheaper tiers see about one content object."""

    content_object_id: ContentObjectId
    media_type: str
    size_bytes: int = Field(ge=0)
    paths: tuple[str, ...] = Field(description="Source paths this content was found at.")
    metadata: EmbeddedMetadata = Field(default_factory=EmbeddedMetadata)


class TierResult(DomainModel):
    """One tier's proposal and how sure it is."""

    tier: ClassificationTier
    facts: Facts
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: Mapping[str, AuditValue] = Field(
        default_factory=dict, description="What the conclusion rests on, for the audit trail."
    )
    model_id: str | None = None


_DOCUMENT_TYPES: Final = frozenset(
    {
        "application/pdf",
        "application/msword",
        "application/rtf",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }
)
_ARCHIVE_TYPES: Final = frozenset(
    {"application/zip", "application/gzip", "application/x-7z-compressed", "application/x-tar"}
)
# Office formats are zip containers; their extension is what separates them from archives.
_DOCUMENT_EXTENSIONS: Final = frozenset(
    {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".pages", ".numbers", ".key"}
)

_SCREENSHOT: Final = re.compile(r"(?i)^(screenshot|screen shot|captura de tela|bildschirmfoto)")
_CAMERA_NAME: Final = re.compile(r"(?i)^(img|dsc|dscn|dscf|pxl|p\d{7}|gopr|mvi|vid)[_-]?\d")
_RECORDING: Final = re.compile(r"(?i)(recording|voice memo|voice note|gravação|memo)")
# Dates in names: IMG_20260102_101112, PXL_20260102_101112345, 2026-01-02 10.11.12,
# Screenshot 2026-01-02 at 10.11.12.
_NAME_DATE: Final = re.compile(
    r"(?P<y>20\d{2}|19\d{2})[-_.]?(?P<m>[01]\d)[-_.]?(?P<d>[0-3]\d)"
    r"(?:[ _T-]*(?:at )?(?P<H>[0-2]\d)[-_.:]?(?P<M>[0-5]\d)[-_.:]?(?P<S>[0-5]\d))?"
)


def media_kind_of(media_type: str, paths: tuple[str, ...]) -> MediaKind:
    """The coarse kind, from the sniffed type and, for containers, the extension."""
    extensions = {PurePosixPath(p).suffix.lower() for p in paths}
    if media_type.startswith("image/"):
        return MediaKind.PHOTO
    if media_type.startswith("video/"):
        return MediaKind.VIDEO
    if media_type.startswith("audio/"):
        return MediaKind.AUDIO
    if media_type in _DOCUMENT_TYPES or media_type.startswith("text/"):
        return MediaKind.DOCUMENT
    if media_type in _ARCHIVE_TYPES:
        return MediaKind.DOCUMENT if extensions & _DOCUMENT_EXTENSIONS else MediaKind.ARCHIVE
    return MediaKind.OTHER


def date_from_name(name: str, local_timezone: str | None) -> datetime | None:
    """A capture time written into a file name, in the configured local zone; else None."""
    if local_timezone is None:
        return None
    match = _NAME_DATE.search(name)
    if match is None:
        return None
    try:
        return datetime(
            int(match["y"]),
            int(match["m"]),
            int(match["d"]),
            int(match["H"] or 0),
            int(match["M"] or 0),
            int(match["S"] or 0),
            tzinfo=ZoneInfo(local_timezone),
        )
    except ValueError:
        return None


def classify_deterministic(item: ClassificationInput, *, local_timezone: str | None) -> TierResult:
    """Facts established from type, extension, metadata and names alone."""
    names = [PurePosixPath(p).name for p in item.paths]
    first_name = names[0] if names else ""
    kind = media_kind_of(item.media_type, item.paths)
    metadata = item.metadata
    captured = metadata.captured_at or date_from_name(first_name, local_timezone)
    evidence: dict[str, AuditValue] = {
        "media_type": item.media_type,
        "name": first_name,
        "camera": metadata.camera,
        "captured_at_source": (
            "metadata" if metadata.captured_at else "name" if captured else None
        ),
    }

    category: str | None = None
    subcategory: str | None = None
    confidence = 0.2
    rule = "unrecognised"
    if kind is MediaKind.PHOTO:
        category = "Photos"
        if any(_SCREENSHOT.search(name) for name in names):
            subcategory, confidence, rule = "Screenshots", 0.9, "screenshot name"
        elif metadata.camera:
            subcategory, confidence, rule = "Camera", 0.95, "camera exif"
        elif any(_CAMERA_NAME.search(name) for name in names):
            subcategory, confidence, rule = "Camera", 0.85, "camera name"
        else:
            confidence, rule = 0.6, "image without provenance"
    elif kind is MediaKind.VIDEO:
        category = "Videos"
        if any(_CAMERA_NAME.search(name) for name in names):
            subcategory, confidence, rule = "Camera", 0.85, "camera name"
        else:
            confidence, rule = 0.7, "video without provenance"
    elif kind is MediaKind.AUDIO:
        category = "Audio"
        if any(_RECORDING.search(name) for name in names):
            subcategory, confidence, rule = "Recordings", 0.85, "recording name"
        else:
            subcategory, confidence, rule = "Music", 0.75, "audio file"
    elif kind is MediaKind.ARCHIVE:
        category, confidence, rule = "Archives", 0.9, "archive type"
    elif kind is MediaKind.DOCUMENT:
        # What a document is lives in its text; only a later tier can say.
        category, confidence, rule = "Documents", 0.4, "document type"
    evidence["rule"] = rule

    return TierResult(
        tier=ClassificationTier.DETERMINISTIC,
        facts=Facts(
            media_kind=kind,
            category=category,
            subcategory=subcategory,
            title=metadata.title,
            captured_at=captured,
            place=metadata.place,
            correspondent=metadata.author,
        ),
        confidence=confidence,
        evidence=evidence,
    )
