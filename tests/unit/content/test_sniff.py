"""Media types come from the bytes."""

import io

import pytest

from curate_d.content.sniff import OCTET_STREAM, sniff, sniff_bytes


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"\xff\xd8\xff\xe0rest", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\nrest", "image/png"),
        (b"GIF89a", "image/gif"),
        (b"II*\x00", "image/tiff"),
        (b"%PDF-1.7", "application/pdf"),
        (b"PK\x03\x04", "application/zip"),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"RIFF\x00\x00\x00\x00XXXX", OCTET_STREAM),
        (b"\x00\x00\x00\x18ftypheic", "image/heic"),
        (b"\x00\x00\x00\x18ftypmif1", "image/heif"),
        (b"\x00\x00\x00\x18ftypqt  ", "video/quicktime"),
        (b"\x00\x00\x00\x18ftypisom", "video/mp4"),
        (b"ID3\x04", "audio/mpeg"),
        (b"\xff\xfb\x90\x00", "audio/mpeg"),
        (b"fLaC", "audio/flac"),
        (b"\x1a\x45\xdf\xa3", "video/x-matroska"),
        ("Invoice 2026 — total €12\n".encode(), "text/plain"),
        (b"\x00\x01\x02binary", OCTET_STREAM),
        (b"", OCTET_STREAM),
    ],
)
def test_signatures_decide_the_media_type(head: bytes, expected: str) -> None:
    """Each recognised signature maps to its media type; the rest are octet streams."""
    assert sniff_bytes(head) == expected


def test_text_cut_mid_character_is_still_text() -> None:
    """A sample ending inside a multi-byte character is not mistaken for binary."""
    assert sniff_bytes("abc€".encode()[:-1]) == "text/plain"


def test_sniffing_leaves_the_handle_at_the_start() -> None:
    """Sniffing a file does not consume it."""
    handle = io.BytesIO(b"%PDF-1.4 body")
    handle.seek(5)
    assert sniff(handle) == "application/pdf"
    assert handle.tell() == 0
