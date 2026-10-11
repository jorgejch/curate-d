"""Media type from content, not from the name.

A file's extension is a claim made by whoever named it; its leading bytes are what a
decoder will actually be handed. The signatures below cover the formats a personal
library is made of. Anything unrecognised is `application/octet-stream`, never a guess.
"""

from typing import BinaryIO, Final

OCTET_STREAM: Final = "application/octet-stream"
_HEAD_BYTES: Final = 4096

# Brands in an ISO base media file's `ftyp` box, which HEIF images and MP4/QuickTime
# video share as a container.
_FTYP_BRANDS: Final = {
    b"heic": "image/heic",
    b"heix": "image/heic",
    b"heim": "image/heic",
    b"heis": "image/heic",
    b"mif1": "image/heif",
    b"msf1": "image/heif",
    b"avif": "image/avif",
    b"qt  ": "video/quicktime",
    b"M4A ": "audio/mp4",
}

_PREFIXES: Final = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"BM", "image/bmp"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/zip"),
    (b"\x1f\x8b", "application/gzip"),
    (b"7z\xbc\xaf\x27\x1c", "application/x-7z-compressed"),
    (b"fLaC", "audio/flac"),
    (b"OggS", "audio/ogg"),
    (b"ID3", "audio/mpeg"),
    (b"\x1a\x45\xdf\xa3", "video/x-matroska"),
)

_RIFF_FORMS: Final = {b"WAVE": "audio/wav", b"AVI ": "video/x-msvideo", b"WEBP": "image/webp"}


def sniff_bytes(head: bytes) -> str:
    """Return the media type the leading bytes of a file declare."""
    for prefix, media_type in _PREFIXES:
        if head.startswith(prefix):
            return media_type
    if head.startswith(b"RIFF") and len(head) >= 12:
        return _RIFF_FORMS.get(head[8:12], OCTET_STREAM)
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return _FTYP_BRANDS.get(head[8:12], "video/mp4")
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return "audio/mpeg"
    if _is_text(head):
        return "text/plain"
    return OCTET_STREAM


def _is_text(head: bytes) -> bool:
    """Whether the bytes are plausibly UTF-8 text: decodable and free of control bytes."""
    if not head:
        return False
    try:
        decoded = head.decode("utf-8")
    except UnicodeDecodeError:
        # A multi-byte character cut at the sample boundary is still text.
        try:
            decoded = head[:-3].decode("utf-8")
        except UnicodeDecodeError:
            return False
    return all(ch in "\t\n\r\f" or ch >= " " for ch in decoded)


def sniff(handle: BinaryIO) -> str:
    """Sniff an open file, leaving it positioned at the start."""
    handle.seek(0)
    head = handle.read(_HEAD_BYTES)
    handle.seek(0)
    return sniff_bytes(head)
