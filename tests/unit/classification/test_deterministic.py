"""The deterministic tier over a fixture set of photos, documents and audio."""

import io
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from curate_d.classification.deterministic import (
    ClassificationInput,
    TierResult,
    classify_deterministic,
    date_from_name,
    media_kind_of,
)
from curate_d.classification.metadata import extract_metadata, image_metadata, pdf_metadata
from curate_d.content.sniff import sniff_bytes
from curate_d.domain import ContentObjectId, MediaKind
from tests.stubs import media

_ZONE = "Europe/Berlin"


def _classify(content: bytes, path: str, *, zone: str | None = _ZONE) -> TierResult:
    """Sniff, extract and classify `content` found at `path`."""
    media_type = sniff_bytes(content[:4096])
    item = ClassificationInput(
        content_object_id=ContentObjectId(uuid4()),
        media_type=media_type,
        size_bytes=len(content),
        paths=(path,),
        metadata=extract_metadata(io.BytesIO(content), media_type, local_timezone=zone),
    )
    return classify_deterministic(item, local_timezone=zone)


def test_a_camera_photo_is_settled_from_its_exif() -> None:
    """Camera, capture time with its offset, and coordinates, all from EXIF."""
    content = media.photo(
        taken=datetime(2026, 7, 14, 10, 11, 12),  # noqa: DTZ001 - EXIF is naive
        offset="+01:00",
        gps=(38.7223, -9.1393),
    )
    result = _classify(content, "/Photos/IMG_4211.JPG")
    assert result.confidence >= 0.9
    facts = result.facts
    assert (facts.media_kind, facts.category, facts.subcategory) == (
        MediaKind.PHOTO,
        "Photos",
        "Camera",
    )
    assert facts.captured_at == datetime(2026, 7, 14, 9, 11, 12, tzinfo=UTC)
    assert facts.place is not None
    assert facts.place.latitude == pytest.approx(38.7223, abs=1e-3)
    assert facts.place.longitude == pytest.approx(-9.1393, abs=1e-3)


def test_a_screenshot_is_recognised_by_name() -> None:
    """No EXIF, but the name says what it is."""
    result = _classify(media.screenshot(), "/Screenshots/Screenshot 2026-02-03 at 10.11.12.png")
    facts = result.facts
    assert facts.subcategory == "Screenshots"
    assert facts.captured_at == datetime(2026, 2, 3, 10, 11, 12, tzinfo=ZoneInfo(_ZONE))
    assert result.confidence >= 0.8


def test_a_naive_timestamp_is_left_unplaced_without_a_configured_zone() -> None:
    """No offset, no GPS time, no configured zone: no capture time, rather than a guess."""
    content = media.photo(taken=datetime(2026, 7, 14, 10, 11, 12))  # noqa: DTZ001
    facts = _classify(content, "/IMG_1.JPG", zone=None).facts
    assert facts.captured_at is None


def test_a_naive_timestamp_is_read_in_the_configured_zone() -> None:
    """The configured zone places a naive EXIF time."""
    content = media.photo(taken=datetime(2026, 1, 14, 10, 0, 0))  # noqa: DTZ001
    facts = _classify(content, "/IMG_1.JPG").facts
    assert facts.captured_at == datetime(2026, 1, 14, 10, 0, tzinfo=ZoneInfo(_ZONE))


def test_a_document_is_typed_but_escalates_for_its_category() -> None:
    """Title, author and creation time are read; the category needs the text."""
    content = media.pdf(
        ["Invoice 2026-001", "Total due: 42.00 EUR"],
        title="Invoice March",
        author="Electric Co",
        created="D:20260301120000+01'00'",
    )
    result = _classify(content, "/Docs/invoice.pdf")
    facts = result.facts
    assert facts.media_kind is MediaKind.DOCUMENT
    assert facts.title == "Invoice March"
    assert facts.correspondent == "Electric Co"
    assert facts.captured_at == datetime(2026, 3, 1, 11, 0, tzinfo=UTC)
    assert result.confidence < 0.8


def test_a_voice_recording_and_a_song_are_told_apart_by_name() -> None:
    """Audio is filed as a recording when named like one, otherwise as music."""
    recording = _classify(media.mp3(), "/Audio/Voice Memo 12.mp3").facts
    song = _classify(media.mp3(), "/Music/Track 03.mp3").facts
    assert (recording.media_kind, recording.subcategory) == (MediaKind.AUDIO, "Recordings")
    assert song.subcategory == "Music"


def test_unrecognised_content_is_barely_trusted() -> None:
    """An octet stream with no clues leaves nothing confident."""
    result = _classify(b"\x00\x01\x02 binary", "/misc/blob.bin")
    assert result.facts.media_kind is MediaKind.OTHER
    assert result.confidence < 0.5


@pytest.mark.parametrize(
    ("media_type", "path", "kind"),
    [
        ("application/zip", "/a/report.docx", MediaKind.DOCUMENT),
        ("application/zip", "/a/backup.zip", MediaKind.ARCHIVE),
        ("text/plain", "/a/notes.txt", MediaKind.DOCUMENT),
        ("video/mp4", "/a/clip.mp4", MediaKind.VIDEO),
    ],
)
def test_office_containers_are_documents_not_archives(
    media_type: str, path: str, kind: MediaKind
) -> None:
    """A .docx is a zip file, and still a document."""
    assert media_kind_of(media_type, (path,)) is kind


def test_gps_time_places_a_photo_without_an_offset() -> None:
    """The GPS fix time is UTC by definition, so it needs no configured zone."""
    from PIL import Image
    from PIL.TiffImagePlugin import IFDRational

    image = Image.new("RGB", (8, 8))
    exif = Image.Exif()
    gps = exif.get_ifd(0x8825)
    gps[29] = "2026:05:06"
    gps[7] = (IFDRational(13, 1), IFDRational(14, 1), IFDRational(15, 1))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif.tobytes())
    metadata = image_metadata(io.BytesIO(buffer.getvalue()), local_timezone=None)
    assert metadata.captured_at == datetime(2026, 5, 6, 13, 14, 15, tzinfo=UTC)


def test_a_pdf_date_without_an_offset_is_not_guessed() -> None:
    """PDF creation times carry their own offset or are dropped."""
    metadata = pdf_metadata(io.BytesIO(media.pdf(["x"], created="D:20260301120000")))
    assert metadata.captured_at is None


def test_broken_files_yield_empty_metadata() -> None:
    """A file that claims a type but does not parse contributes nothing."""
    assert image_metadata(io.BytesIO(b"\xff\xd8\xff junk"), local_timezone=None).camera is None
    assert pdf_metadata(io.BytesIO(b"%PDF-1.4 junk")).page_count is None


def test_names_carry_dates_in_several_conventions() -> None:
    """Camera, phone and screenshot naming conventions all yield a time."""
    zone = ZoneInfo(_ZONE)
    assert date_from_name("IMG_20260102_101112.jpg", _ZONE) == datetime(
        2026, 1, 2, 10, 11, 12, tzinfo=zone
    )
    assert date_from_name("PXL_20260102_101112345.jpg", _ZONE) == datetime(
        2026, 1, 2, 10, 11, 12, tzinfo=zone
    )
    assert date_from_name("2026-01-02 10.11.12.jpg", _ZONE) == datetime(
        2026, 1, 2, 10, 11, 12, tzinfo=zone
    )
    assert date_from_name("holiday.jpg", _ZONE) is None
    assert date_from_name("IMG_20260102.jpg", None) is None
    assert date_from_name("IMG_20261340.jpg", _ZONE) is None


def test_the_fixture_offset_is_what_the_capture_time_carries() -> None:
    """An EXIF offset is applied as written, not converted to the configured zone."""
    content = media.photo(taken=datetime(2026, 7, 14, 10, 0, 0), offset="-03:00")  # noqa: DTZ001
    metadata = image_metadata(io.BytesIO(content), local_timezone=_ZONE)
    assert metadata.captured_at is not None
    assert metadata.captured_at.utcoffset() == timedelta(hours=-3)
    assert metadata.captured_at == datetime(
        2026, 7, 14, 10, 0, tzinfo=timezone(timedelta(hours=-3))
    )
