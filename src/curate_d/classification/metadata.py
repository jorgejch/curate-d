"""Embedded metadata: what a file says about itself.

Read on the CPU profile from a verified snapshot. EXIF for images, the document
information dictionary for PDFs. Nothing here guesses: a timestamp without a timezone is
only placed in time through an explicitly configured one, and a malformed field is
dropped rather than repaired.
"""

import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import BinaryIO, Final
from zoneinfo import ZoneInfo

from PIL import Image, UnidentifiedImageError
from pydantic import AwareDatetime, Field
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from curate_d.content import perceptual  # noqa: F401 - registers the HEIF opener
from curate_d.domain import DomainModel, GeoPoint

# EXIF tag numbers (CIPA DC-008).
_EXIF_IFD: Final = 0x8769
_GPS_IFD: Final = 0x8825
_MAKE: Final = 0x010F
_MODEL: Final = 0x0110
_SOFTWARE: Final = 0x0131
_DATETIME: Final = 0x0132
_DATETIME_ORIGINAL: Final = 0x9003
_OFFSET_ORIGINAL: Final = 0x9011
_GPS_LAT_REF, _GPS_LAT, _GPS_LON_REF, _GPS_LON = 1, 2, 3, 4
_GPS_TIME, _GPS_DATE = 7, 29

_EXIF_TIME: Final = "%Y:%m:%d %H:%M:%S"
_OFFSET: Final = re.compile(r"\A([+-])(\d{2}):(\d{2})\Z")
_PDF_DATE: Final = re.compile(
    r"\AD:(\d{4})(\d{2})?(\d{2})?(\d{2})?(\d{2})?(\d{2})?(Z|[+-]\d{2}'?\d{2}'?)?"
)


class EmbeddedMetadata(DomainModel):
    """What a file's own metadata establishes. Every field is optional."""

    captured_at: AwareDatetime | None = None
    place: GeoPoint | None = None
    camera: str | None = Field(default=None, description="EXIF make and model.")
    software: str | None = None
    title: str | None = None
    author: str | None = None
    page_count: int | None = Field(default=None, ge=0)
    text_sample: str | None = Field(
        default=None, description="Leading text from a document's text layer, if it has one."
    )


def _text(value: object) -> str | None:
    """A trimmed non-empty string, or None."""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    if not isinstance(value, str):
        return None
    cleaned = value.strip("\x00 \t\r\n")
    return cleaned or None


def _offset(value: object) -> tzinfo | None:
    """An EXIF offset string such as "+02:00" as a timezone."""
    text = _text(value)
    match = _OFFSET.fullmatch(text) if text else None
    if match is None:
        return None
    sign = 1 if match.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(match.group(2)), minutes=int(match.group(3))))


def _rational(value: object) -> float | None:
    """A PIL rational or number as a float."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _gps_coordinate(parts: object, reference: object, negative: str) -> float | None:
    """Degrees, minutes, seconds and a hemisphere as signed decimal degrees."""
    if not isinstance(parts, tuple) or len(parts) != 3:
        return None
    values = [_rational(part) for part in parts]
    if any(v is None for v in values):
        return None
    degrees, minutes, seconds = (v for v in values if v is not None)
    decimal = degrees + minutes / 60 + seconds / 3600
    return -decimal if _text(reference) == negative else decimal


def _gps_time(gps: dict[int, object]) -> datetime | None:
    """The GPS fix time, which EXIF records in UTC."""
    date, clock = _text(gps.get(_GPS_DATE)), gps.get(_GPS_TIME)
    if date is None or not isinstance(clock, tuple) or len(clock) != 3:
        return None
    hms = [_rational(part) for part in clock]
    if any(v is None for v in hms):
        return None
    try:
        day = datetime.strptime(date, "%Y:%m:%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    hours, minutes, seconds = (v for v in hms if v is not None)
    return day + timedelta(hours=hours, minutes=minutes, seconds=seconds)


def _exif_time(value: object, offset: tzinfo | None, local: tzinfo | None) -> datetime | None:
    """An EXIF timestamp, placed in time by its offset or the configured local zone."""
    text = _text(value)
    if text is None:
        return None
    try:
        naive = datetime.strptime(text, _EXIF_TIME)  # noqa: DTZ007 - zone applied below
    except ValueError:
        return None
    zone = offset or local
    return None if zone is None else naive.replace(tzinfo=zone)


def image_metadata(handle: BinaryIO, *, local_timezone: str | None) -> EmbeddedMetadata:
    """EXIF metadata of an image. A file no decoder opens yields nothing."""
    handle.seek(0)
    local = ZoneInfo(local_timezone) if local_timezone else None
    try:
        with Image.open(handle) as image:
            exif = image.getexif()
    except (UnidentifiedImageError, OSError, ValueError):
        return EmbeddedMetadata()
    finally:
        handle.seek(0)
    details = dict(exif.get_ifd(_EXIF_IFD))
    gps = dict(exif.get_ifd(_GPS_IFD))
    make, model = _text(exif.get(_MAKE)), _text(exif.get(_MODEL))
    camera = " ".join(part for part in (make, model) if part) or None
    captured = (
        _exif_time(details.get(_DATETIME_ORIGINAL), _offset(details.get(_OFFSET_ORIGINAL)), local)
        or _gps_time(gps)
        or _exif_time(exif.get(_DATETIME), None, local)
    )
    latitude = _gps_coordinate(gps.get(_GPS_LAT), gps.get(_GPS_LAT_REF), "S")
    longitude = _gps_coordinate(gps.get(_GPS_LON), gps.get(_GPS_LON_REF), "W")
    place = None
    if latitude is not None and longitude is not None:
        try:
            place = GeoPoint(latitude=latitude, longitude=longitude)
        except ValueError:
            place = None
    return EmbeddedMetadata(
        captured_at=captured,
        place=place,
        camera=camera,
        software=_text(exif.get(_SOFTWARE)),
    )


def _pdf_date(value: object) -> datetime | None:
    """A PDF date string (D:YYYYMMDDHHmmSS+HH'mm') with its offset; None without one."""
    text = _text(value)
    match = _PDF_DATE.match(text) if text else None
    if match is None or match.group(7) is None:
        return None
    year, month, day, hour, minute, second = (
        int(g or default)
        for g, default in zip(match.groups()[:6], ("0", "1", "1", "0", "0", "0"), strict=True)
    )
    zone_text = match.group(7).replace("'", "")
    if zone_text == "Z":
        zone: tzinfo = UTC
    else:
        sign = 1 if zone_text[0] == "+" else -1
        zone = timezone(sign * timedelta(hours=int(zone_text[1:3]), minutes=int(zone_text[3:5])))
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=zone)
    except ValueError:
        return None


_TEXT_SAMPLE_CHARS: Final = 4000


def pdf_metadata(handle: BinaryIO) -> EmbeddedMetadata:
    """The information dictionary and leading text of a PDF. A broken PDF yields nothing."""
    handle.seek(0)
    try:
        reader = PdfReader(handle)
        info: Mapping[str, object] = reader.metadata or {}
        pages = len(reader.pages)
        sample = ""
        for page in reader.pages[:3]:
            sample += page.extract_text() or ""
            if len(sample) >= _TEXT_SAMPLE_CHARS:
                break
    except (PdfReadError, OSError, ValueError, KeyError):
        return EmbeddedMetadata()
    finally:
        handle.seek(0)
    return EmbeddedMetadata(
        captured_at=_pdf_date(info.get("/CreationDate")),
        title=_text(info.get("/Title")),
        author=_text(info.get("/Author")),
        software=_text(info.get("/Producer")),
        page_count=pages,
        text_sample=_text(sample[:_TEXT_SAMPLE_CHARS]),
    )


def extract_metadata(
    handle: BinaryIO, media_type: str, *, local_timezone: str | None
) -> EmbeddedMetadata:
    """Whatever metadata the file's type carries."""
    if media_type.startswith("image/"):
        return image_metadata(handle, local_timezone=local_timezone)
    if media_type == "application/pdf":
        return pdf_metadata(handle)
    return EmbeddedMetadata()
