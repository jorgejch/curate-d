# PDF reading limits

The classification tier reads PDFs with pypdf (`classification/metadata.py` for the
information dictionary and text layer, `classification/local.py` for OCR of embedded
images). From pypdf 7 the library refuses hostile or oversized input instead of
working through it. The values below are the defaults of `pypdf.Configuration` in 7.0.1;
curate.d does not override them. Re-check the list on every pypdf upgrade.

## Limits that matter here

| Setting | Default | Reached by |
| --- | --- | --- |
| `image_maximum_buffer_size` | 75,000,000 bytes | `page.images`: a large scan's image buffer, sized from the actual bytes per pixel since 7.0.1 |
| `zlib_maximum_output_length`, `lzw_…`, `run_length_…`, `jbig2_…`, `brotli_…` | 75,000,000 bytes | decompressing a stream |
| `maximum_declared_stream_length` | 75,000,000 bytes | a stream's declared `/Length` |
| `stream_decoding_work_maximum_length` | 300,000,000 bytes | accumulated decoding work per stream |
| `stream_parsing_work_maximum_count` | 2,000,000 | parsing a content stream in `extract_text()` |
| `xform_maximum_invocations_per_extraction` | 5,000 | `/XObject` forms per page in `extract_text()` |
| `page_tree_maximum_entries`, `…_depth` | 100,000, 100 | `len(reader.pages)` |

## What happens when one is reached

- pypdf raises `LimitReachedError` (or `EmptyImageDataError` for an image with no data).
  Both derive from `PyPdfError`, as does `PdfReadError`.
- `classification/pdf_errors.py` names the tuple every PDF read catches: `PyPdfError`,
  `DeprecationError`, `DependencyError`, `OSError`, `ValueError`, `KeyError`. A new pypdf
  error type that does not derive from `PyPdfError` is the case to look for on an upgrade.
- The read keeps what it had: the information dictionary, page count and text of earlier
  pages (`pdf_metadata`), the OCR text of earlier pages (`_ocr_pdf`).
- One `classification.pdf_unreadable` WARNING is logged with `stage` (`metadata` or `ocr`)
  and the error class. It carries no path and no pypdf message.
- A PDF that yields no text has no text source, so `categorize` returns confidence 0.0
  and the item escalates to the remote tier rather than being filed on nothing.

## Known regression: abbreviated filter names

pypdf 7 raises `DeprecationError` for the abbreviated filter names `/A85`, `/AHx`, `/LZW`
and `/Fl` where 6.21.4 warned and decoded (py-pdf/pypdf#4371, open). Such a PDF (typically an
old one) now reads as far as the failure and escalates if that left no text. Drop the
handling for `DeprecationError` only once upstream restores decoding and the pin moves.

## Other 7.0.x changes checked

- Non-deterministic file identifiers (#4366): curate.d only reads PDFs through pypdf; it
  does not write them, hash pypdf output or compare identifiers.
- Image size limits (#4361, #4374): covered above.
