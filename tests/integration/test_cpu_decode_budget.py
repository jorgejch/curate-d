"""Peak memory of the CPU worker's image decodes, measured against its stated budget.

`cpu_decode_max_pixels` rests on an arithmetic claim: a 1 GiB CPU worker that idles near
200 MiB and runs four decodes at once can afford a quarter of the rest for each. This
measures what one decode actually costs, in a fresh interpreter per case so earlier
allocations do not mask it: the peak resident set after the decode, minus the peak after
imports. The numbers are recorded in docs/cpu-worker-budget.md.

Integration rather than unit because it builds images of tens of megapixels and spawns
interpreters; nothing here needs the local stack.
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.integration

# The reference CPU worker: 1 GiB, of which the idle process holds about 200 MiB (the
# local stack's worker, measured); four concurrent decodes share the rest.
WORKER_MEMORY_BYTES: Final = 1024**3
WORKER_IDLE_BYTES: Final = 200 * 1024**2
PER_DECODE_BUDGET_BYTES: Final = (WORKER_MEMORY_BYTES - WORKER_IDLE_BYTES) // 4

_MAKE: Final = """
import struct, sys, zlib
from PIL import Image, ImageDraw

path, fmt, width, height = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
if fmt == "PNG-HEADER":
    def chunk(kind, data):
        crc = struct.pack(">I", zlib.crc32(kind + data))
        return struct.pack(">I", len(data)) + kind + data + crc
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    with open(path, "wb") as out:
        out.write(b"\\x89PNG\\r\\n\\x1a\\n" + chunk(b"IHDR", header)
                  + chunk(b"IDAT", zlib.compress(b"\\x00" * 16)) + chunk(b"IEND", b""))
    sys.exit(0)
image = Image.new("RGB", (width, height), (40, 90, 160))
draw = ImageDraw.Draw(image)
for i in range(40):
    x, y = i * width // 41, i * height // 43
    draw.rectangle((x, y, x + width // 8, y + height // 9), fill=(i * 6, 255 - i * 6, 90))
image.save(path, format=fmt, **({"quality": 90} if fmt == "JPEG" else {"compress_level": 1}))
"""

_MEASURE: Final = """
import json, resource, sys

from curate_d.classification.local import _OCR_EDGE
from curate_d.classification.prepare import thumbnail_jpeg
from curate_d.content.decode import DEFAULT_MAX_PIXELS, ImageTooLargeError, open_bounded
from curate_d.content.perceptual import perceptual_fingerprint

scale = 1 if sys.platform == "darwin" else 1024
path, operation = sys.argv[1], sys.argv[2]
handle = open(path, "rb")
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
outcome = "decoded"
try:
    if operation == "fingerprint":
        perceptual_fingerprint(handle, max_pixels=DEFAULT_MAX_PIXELS)
    elif operation == "thumbnail":
        if thumbnail_jpeg(handle, max_pixels=DEFAULT_MAX_PIXELS) is None:
            outcome = "refused"
    else:
        with open_bounded(
            handle, max_pixels=DEFAULT_MAX_PIXELS, target_edge=_OCR_EDGE, greyscale=True
        ) as image:
            image.convert("L").thumbnail((_OCR_EDGE, _OCR_EDGE))
except ImageTooLargeError:
    outcome = "refused"
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
print(json.dumps({"outcome": outcome, "baseline": before, "peak": after}))
"""

_REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[2]


def _fixture(directory: Path, name: str, fmt: str, size: tuple[int, int]) -> Path:
    """Build one image file in a separate interpreter, so this process stays small."""
    path = directory / name
    subprocess.run(
        [sys.executable, "-I", "-c", _MAKE, str(path), fmt, *map(str, size)],
        check=True,
        timeout=300,
    )
    return path


def _measure(path: Path, operation: str) -> dict[str, int | str]:
    """Run one decode in a fresh interpreter and return its outcome and peak memory."""
    completed = subprocess.run(
        [sys.executable, "-c", _MEASURE, str(path), operation],
        check=True,
        capture_output=True,
        text=True,
        timeout=300,
        cwd=_REPOSITORY_ROOT,
    )
    result: dict[str, int | str] = json.loads(completed.stdout.strip().splitlines()[-1])
    return result


@pytest.fixture(scope="module")
def images(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Build the measured images once for the module."""
    directory = tmp_path_factory.mktemp("decode-budget")
    return {
        # Just under the default budget, in a format that cannot decode at reduced scale.
        "png-32mp": _fixture(directory, "32mp.png", "PNG", (6928, 4618)),
        "png-60mp": _fixture(directory, "60mp.png", "PNG", (9000, 6667)),
        "png-120mp": _fixture(directory, "120mp.png", "PNG-HEADER", (12600, 9524)),
        "jpeg-60mp": _fixture(directory, "60mp.jpg", "JPEG", (9000, 6667)),
        "jpeg-120mp": _fixture(directory, "120mp.jpg", "JPEG", (12600, 9524)),
    }


@pytest.mark.parametrize("operation", ["fingerprint", "thumbnail", "ocr"])
@pytest.mark.parametrize(
    ("image", "outcome"),
    [
        ("png-32mp", "decoded"),
        ("png-60mp", "refused"),
        ("png-120mp", "refused"),
        ("jpeg-60mp", "decoded"),
        ("jpeg-120mp", "decoded"),
    ],
)
def test_one_decode_stays_within_its_share_of_the_worker(
    images: dict[str, Path], image: str, outcome: str, operation: str
) -> None:
    """Each decode the budget admits peaks within its quarter; above it, nothing decodes."""
    measured = _measure(images[image], operation)
    growth = int(measured["peak"]) - int(measured["baseline"])
    report = f"{image} {operation}: {measured['outcome']}, +{growth / 1024**2:.0f} MiB"
    assert measured["outcome"] == outcome, report
    assert growth < PER_DECODE_BUDGET_BYTES, report
