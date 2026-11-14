"""The failures a PDF read is expected to end in, and the one event that reports them.

pypdf 7 refuses hostile or malformed input with its own errors, and its deprecation
cleanup turned `DeprecationError` into a hard failure: a PDF that uses an abbreviated
filter name (`/A85`, `/AHx`, `/LZW`, `/Fl`) raises it from `extract_text()` and `.images`
(py-pdf/pypdf#4371). None of these is a defect of ours: the file yields what it yielded
before the failure, and the tier that needs text it did not get escalates to the remote
model. See `docs/pdf-limits.md`.
"""

from typing import Final

from pypdf.errors import DependencyError, DeprecationError, PyPdfError

from curate_d.observability import get_logger

logger = get_logger(__name__)

# `PyPdfError` covers `PdfReadError`, `LimitReachedError` (pypdf's resource limits, such as
# the image buffer size) and `EmptyImageDataError`. `DeprecationError` and `DependencyError`
# (a filter needing an absent optional package) do not derive from it.
PDF_READ_ERRORS: Final = (
    PyPdfError,
    DeprecationError,
    DependencyError,
    OSError,
    ValueError,
    KeyError,
)


def log_unreadable(stage: str, error: Exception) -> None:
    """Log that a PDF was read only in part; the path and message stay out of the event."""
    logger.warning("classification.pdf_unreadable", stage=stage, error=type(error).__name__)
