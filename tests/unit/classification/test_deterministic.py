"""The deterministic tier over a fixture set of photos, documents and audio."""

import io
from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pypdf.errors import DeprecationError, LimitReachedError

from curate_d.classification.deterministic import (
    ClassificationInput,
    TierResult,
    classify_deterministic,
    date_from_name,
    media_kind_of,
)
from curate_d.classification.metadata import (
    EmbeddedMetadata,
    extract_metadata,
    image_metadata,
    pdf_metadata,
)
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


def test_pdf_metadata_treats_deprecation_errors_as_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A PDF using a hard-deprecated feature contributes no metadata."""

    def reject_deprecated_filter(handle: object) -> None:
        raise DeprecationError("abbreviated filter")

    monkeypatch.setattr("curate_d.classification.metadata.PdfReader", reject_deprecated_filter)
    handle = io.BytesIO(b"%PDF-1.4")
    assert pdf_metadata(handle) == EmbeddedMetadata()
    assert handle.tell() == 0


class _Page:
    """A fake PDF page whose text extraction yields text or raises."""

    def __init__(self, text: str | Exception) -> None:
        self._text = text

    def extract_text(self) -> str:
        """Return the page's text, or raise the configured error."""
        if isinstance(self._text, Exception):
            raise self._text
        return self._text


def _fake_reader(pages: list[_Page]) -> object:
    """Return a PdfReader stand-in over `pages` with a title in its information dictionary."""

    class Reader:
        metadata = {"/Title": "Kept"}  # noqa: RUF012 - read-only test double

        def __init__(self, handle: object) -> None:
            self.pages = pages

    return Reader


@pytest.mark.parametrize(
    "error",
    [DeprecationError("abbreviated filter"), LimitReachedError("image buffer")],
)
def test_pdf_metadata_keeps_what_was_read_before_a_page_fails(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """A failure on a later page keeps the dictionary, page count and earlier text."""
    reader = _fake_reader([_Page("first page text"), _Page(error)])
    monkeypatch.setattr("curate_d.classification.metadata.PdfReader", reader)
    result = pdf_metadata(io.BytesIO(b"%PDF-1.4"))
    assert result.title == "Kept"
    assert result.page_count == 2
    assert result.text_sample == "first page text"


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


def _item(media_type: str, path: str, **metadata: object) -> ClassificationInput:
    """Build an input with `media_type` at `path`, with the given metadata fields set."""
    return ClassificationInput(
        content_object_id=ContentObjectId(uuid4()),
        media_type=media_type,
        size_bytes=1,
        paths=(path,),
        metadata=EmbeddedMetadata.model_validate(metadata),
    )


@pytest.mark.parametrize(
    ("item", "category", "subcategory", "confidence", "rule"),
    [
        (
            _item("image/png", "/a/Screenshot 1.png"),
            "Photos",
            "Screenshots",
            0.9,
            "screenshot name",
        ),
        (_item("image/jpeg", "/a/x.jpg", camera="Nikon"), "Photos", "Camera", 0.95, "camera exif"),
        (_item("image/jpeg", "/a/IMG_1.JPG"), "Photos", "Camera", 0.85, "camera name"),
        (_item("image/jpeg", "/a/holiday.jpg"), "Photos", None, 0.6, "image without provenance"),
        (_item("video/mp4", "/a/MVI_0001.mp4"), "Videos", "Camera", 0.85, "camera name"),
        (_item("video/mp4", "/a/clip.mp4"), "Videos", None, 0.7, "video without provenance"),
        (_item("audio/mpeg", "/a/Voice Memo 1.mp3"), "Audio", "Recordings", 0.85, "recording name"),
        (_item("audio/mpeg", "/a/Track 03.mp3"), "Audio", "Music", 0.75, "audio file"),
        (_item("application/zip", "/a/backup.zip"), "Archives", None, 0.9, "archive type"),
        (_item("application/pdf", "/a/x.pdf"), "Documents", None, 0.4, "document type"),
        (_item("application/octet-stream", "/a/blob.bin"), None, None, 0.2, "unrecognised"),
    ],
    ids=lambda value: value if isinstance(value, str) else "",
)
def test_each_rule_names_itself_in_the_evidence(
    item: ClassificationInput,
    category: str | None,
    subcategory: str | None,
    confidence: float,
    rule: str,
) -> None:
    """Every kind's rule fixes the category, the subcategory, the confidence and its name."""
    result = classify_deterministic(item, local_timezone=_ZONE)
    assert (result.facts.category, result.facts.subcategory) == (category, subcategory)
    assert result.confidence == confidence
    assert result.evidence["rule"] == rule


def test_a_screenshot_named_photo_with_exif_is_still_a_screenshot() -> None:
    """The screenshot name is checked before the EXIF camera, and that before the camera name."""
    both = _item("image/png", "/a/Screenshot IMG_1.png", camera="Nikon")
    assert classify_deterministic(both, local_timezone=_ZONE).evidence["rule"] == "screenshot name"
    exif_and_name = _item("image/jpeg", "/a/IMG_1.JPG", camera="Nikon")
    assert (
        classify_deterministic(exif_and_name, local_timezone=_ZONE).evidence["rule"]
        == "camera exif"
    )


def test_the_capture_time_source_is_recorded() -> None:
    """Metadata wins over a name date; a name date is named; no date leaves no source."""
    taken = datetime(2026, 1, 2, 10, 11, 12, tzinfo=UTC)
    from_metadata = _item("image/jpeg", "/a/IMG_20260102_101112.jpg", captured_at=taken)
    from_name = _item("image/jpeg", "/a/IMG_20260102_101112.jpg")
    from_nothing = _item("image/jpeg", "/a/holiday.jpg")
    assert (
        classify_deterministic(from_metadata, local_timezone=_ZONE).evidence["captured_at_source"]
        == "metadata"
    )
    assert (
        classify_deterministic(from_name, local_timezone=_ZONE).evidence["captured_at_source"]
        == "name"
    )
    assert (
        classify_deterministic(from_nothing, local_timezone=_ZONE).evidence["captured_at_source"]
        is None
    )


def test_a_pdf_date_of_a_bare_year_in_utc_is_the_first_of_january() -> None:
    """Every part after the year is optional; a zone alone places a bare year."""
    metadata = pdf_metadata(io.BytesIO(media.pdf(["x"], created="D:2026Z")))
    assert metadata.captured_at == datetime(2026, 1, 1, tzinfo=UTC)


def test_a_pdf_date_offset_is_read_without_its_quotes() -> None:
    """The apostrophes in the offset are optional, and a negative offset is applied as written."""
    metadata = pdf_metadata(io.BytesIO(media.pdf(["x"], created="D:20260301120000-0500")))
    assert metadata.captured_at == datetime(2026, 3, 1, 12, 0, tzinfo=timezone(timedelta(hours=-5)))
    assert metadata.captured_at == datetime(2026, 3, 1, 17, 0, tzinfo=UTC)
