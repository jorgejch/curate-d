"""Media fixtures built in memory: photos with EXIF, PDFs with a text layer, scans, audio.

Built rather than checked in so every property a test relies on — a camera model, a
capture time, a coordinate, a sentence of text — is visible where the test states it.
"""

import io
import struct
import zlib
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont
from PIL.TiffImagePlugin import IFDRational

_EXIF_IFD = 0x8769
_GPS_IFD = 0x8825


def _dms(value: float) -> tuple[IFDRational, IFDRational, IFDRational]:
    """Decimal degrees as EXIF degrees, minutes and seconds."""
    value = abs(value)
    degrees = int(value)
    minutes = int((value - degrees) * 60)
    seconds = round(((value - degrees) * 60 - minutes) * 60 * 1000)
    return IFDRational(degrees, 1), IFDRational(minutes, 1), IFDRational(seconds, 1000)


def photo(
    *,
    camera: tuple[str, str] | None = ("Canon", "EOS R6"),
    taken: datetime | None = None,
    offset: str | None = None,
    gps: tuple[float, float] | None = None,
    seed: int = 1,
    size: tuple[int, int] = (160, 120),
) -> bytes:
    """A JPEG carrying the given EXIF."""
    image = Image.new("RGB", size, (seed * 37 % 255, 90, 140))
    ImageDraw.Draw(image).rectangle((10, 10, 60 + seed % 40, 70), fill=(250, 200, 30))
    exif = Image.Exif()
    if camera is not None:
        exif[0x010F], exif[0x0110] = camera
    details = exif.get_ifd(_EXIF_IFD)
    if taken is not None:
        details[0x9003] = taken.strftime("%Y:%m:%d %H:%M:%S")
    if offset is not None:
        details[0x9011] = offset
    if gps is not None:
        latitude, longitude = gps
        location = exif.get_ifd(_GPS_IFD)
        location[1] = "N" if latitude >= 0 else "S"
        location[2] = _dms(latitude)
        location[3] = "E" if longitude >= 0 else "W"
        location[4] = _dms(longitude)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def screenshot() -> bytes:
    """A PNG with no EXIF, as a phone's screenshot tool writes it."""
    buffer = io.BytesIO()
    Image.new("RGB", (120, 240), (240, 240, 240)).save(buffer, format="PNG")
    return buffer.getvalue()


def _pdf_string(text: str) -> str:
    """Escape text for a PDF literal string."""
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def pdf(
    lines: list[str],
    *,
    title: str | None = None,
    author: str | None = None,
    created: str | None = None,
) -> bytes:
    """A one-page PDF with a real text layer and an information dictionary."""
    body = (
        "BT /F1 12 Tf 72 720 Td 14 TL "
        + " ".join(f"({_pdf_string(line)}) Tj T*" for line in lines)
        + " ET"
    )
    info = []
    if title:
        info.append(f"/Title ({_pdf_string(title)})")
    if author:
        info.append(f"/Author ({_pdf_string(author)})")
    if created:
        info.append(f"/CreationDate ({created})")
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(body)} >>\nstream\n{body}\nendstream",
        "<< " + " ".join(info) + " >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, content in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n{content}\nendobj\n".encode("latin-1"))
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 6 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode()
    )
    return out.getvalue()


def scanned_document(lines: list[str], *, fmt: str = "PNG") -> bytes:
    """An image of printed text, as a scanner produces it: no text layer at all."""
    font = ImageFont.load_default(size=36)
    image = Image.new("L", (1400, 120 + 60 * len(lines)), 255)
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((60, 60 + 60 * index), line, fill=0, font=font)
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    """Return one PNG chunk: length, type, data and CRC."""
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png_header(width: int, height: int, *, comment: str = "") -> bytes:
    """Return a PNG that declares `width` by `height` RGB pixels but carries almost none.

    A decoder learns the size from the header alone, so this is how a decompression bomb
    looks before anything is decoded: a few dozen bytes claiming gigabytes of pixels.
    `comment` goes in a text chunk, to make the bytes distinct without changing the image.
    """
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    text = _png_chunk(b"tEXt", b"Comment\x00" + comment.encode()) if comment else b""
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + text
        + _png_chunk(b"IDAT", zlib.compress(b"\x00" * 16))
        + _png_chunk(b"IEND", b"")
    )


def jpeg_header(width: int, height: int) -> bytes:
    """Return a small greyscale JPEG whose frame header states `width` by `height`.

    Like `png_header`, the size a decoder reads is a lie the bytes cannot back.
    """
    buffer = io.BytesIO()
    Image.new("L", (16, 16), 255).save(buffer, format="JPEG")
    data = bytearray(buffer.getvalue())
    index = 2
    while index < len(data):
        marker = data[index + 1]
        (length,) = struct.unpack(">H", data[index + 2 : index + 4])
        if marker in (0xC0, 0xC1, 0xC2):
            data[index + 5 : index + 9] = struct.pack(">HH", height, width)
            break
        index += 2 + length
    return bytes(data)


def pdf_with_image_stream(
    stream: bytes,
    *,
    image_filter: str,
    width: int,
    height: int,
    content: bytes = b"q 612 0 0 792 0 0 cm /Im0 Do Q",
    content_filter: str | None = None,
) -> bytes:
    """Return a one-page PDF drawing one image XObject whose stream is `stream`, as given.

    The dictionary declares `width` by `height` and `image_filter`; nothing checks that the
    stream agrees, which is the point: a PDF may declare one size and carry another, or
    carry streams pypdf refuses to decode. `content` is the page's content stream, stored
    under `content_filter` when one is given.
    """
    content_entry = b"" if content_filter is None else b"/Filter " + content_filter.encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /XObject << /Im0 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Filter %s /Length %d >>\nstream\n"
        % (width, height, image_filter.encode(), len(stream))
        + stream
        + b"\nendstream",
        b"<< /Length %d %s >>\nstream\n" % (len(content), content_entry) + content + b"\nendstream",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(
        b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    )
    return out.getvalue()


def mp3() -> bytes:
    """An MP3 with an ID3 header; enough to be sniffed as audio."""
    return b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" * 64
