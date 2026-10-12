# CPU worker budget

The CPU profile decodes, hashes and OCRs untrusted content. This page states the budget it
is sized for, every limit that keeps one file inside that budget, the setting behind each
limit, the measurements the defaults rest on, and the [decisions](#decisions) behind them
(issue #8). The behaviour is specified in the
[classification](../openspec/specs/classification/spec.md) and
[content-ledger](../openspec/specs/content-ledger/spec.md) specs.

## The budget

| | |
| --- | --- |
| Reference worker | 1 CPU worker replica, 1 vCPU, 1 GiB |
| Ceiling | 2 vCPU in total; past that, larger nodes |
| Idle worker | about 200 MiB resident (the local stack's CPU worker, idle) |
| Concurrent decodes | 4: the `fingerprint` and `classify-cheap` queues run 2 workflows each per worker (`runtime/queues.py`) |
| Per decode | (1024 − 200) / 4 ≈ 206 MiB |
| Tesseract | one thread per process (`OMP_THREAD_LIMIT=1`); runs as a child process in the same container |

The CPU worker's resource requests and limits are set in the deployment repository, not
here. They should state this budget and cite the measurement below.

## Limits

| Limit | Default | Setting or constant | When it is reached |
| --- | --- | --- | --- |
| Pixels in one image decode | 32 MP | `cpu_decode_max_pixels` | Refused from the header, before any pixel is decoded (`content/decode.py`). No perceptual fingerprint; the inspection records a note. No OCR and no thumbnail. |
| Pillow's own ceiling | warns above 200 MP, refuses above 400 MP | `Image.MAX_IMAGE_PIXELS` in `content/decode.py` | Backstop for any decode that bypasses the budget; reported as the same refusal. |
| Reduced-scale JPEG decode | long edge 512 (perceptual hash), 1024 (thumbnail), 4096 (OCR) | constants beside each caller | A JPEG decodes at 1/2, 1/4 or 1/8 scale where the caller needs no more; the budget applies to what is decoded. Other formats decode in full. |
| Image handed to Tesseract | long edge 4096 | `_OCR_EDGE`, `classification/local.py` | Larger scans are reduced first; the full decode is released before Tesseract starts. |
| PDF pages OCRed | 3 | `_OCR_PAGES` | Later pages are not read; recorded as `page_cap`. |
| Embedded images OCRed per PDF | 6 | `_OCR_IMAGES` | Later images are not read; recorded as `image_cap`. A page image whose declared size exceeds the pixel budget is skipped before its stream is decoded (`decode_budget`). |
| Tesseract time per image | 60 s | `_OCR_TIMEOUT_SECONDS` | The process is killed; text read so far is kept and OCR stops (`ocr_timeout`). |
| Extracted text | 20,000 characters | `_MAX_TEXT` | OCR stops once reached; plain text is read to it (`text_cap`). |
| Text shown to the remote tier | 3,000 characters | `_EXCERPT_CHARS`, `classification/prepare.py` | The excerpt is cut. |
| Image shown to the remote tier | JPEG, long edge 1024 | `THUMBNAIL_EDGE` | Never the file itself. |
| Remote answer | 1024 tokens | `remote_max_output_tokens` | An answer cut off at the limit is malformed output and goes to review. |
| Remote spend | USD 5 per UTC month | `remote_spend_cap_usd`, reserved per call by `remote_call_estimate_usd` | Content is held for the next month. |
| File size | unset | `ingest_max_bytes` | The source fails at once, before any provider read, with its size and the bound. |
| Staged bytes in total | 100 GiB | `staging_max_bytes` | A file larger than this fails at once, as above. A full staging area waits and retries. |
| I/O worker scratch | 20 GiB | `staging_local_scratch_max_bytes` | A file larger than this fails at once, as above. |
| One staging upload or download | 300 s | `staging_transfer_timeout_seconds` | A size ceiling that depends on throughput: at 50 MB/s, about 15 GB. Owned by the staging transfer path (issue #1), not changed here. |

Every limit that cuts text extraction short is recorded as `text_limited` in the local
tier's evidence and as `input_content_limited` in the classification audit entry.

### Content the limits keep unread

When a decode is refused for the pixel budget, or OCR is killed at its time limit, and the
local tier has read no text at all, the remote tier would be shown only the file's names.
Classification then sends the content to review with the reason `resource_limit` instead of
spending a remote call on it. The review item's detail names the limit. Content the cheap
tiers settle from names or metadata is still accepted as usual.

### Files that can never fit

A file larger than `ingest_max_bytes`, `staging_max_bytes` or the I/O worker's scratch
fails in `ingest_source` before it is fetched: the source is marked failed with a reason
such as `file is 30000000000 bytes; the bound is 21474836480 bytes
(staging_local_scratch_max_bytes)`, a `source_read_failed` audit entry is written, and the
workflow returns `too_large`. Staging that reports a request will never fit
(`StagingCapacityNeverFitsError`) takes the same route. Only staging that is full for now
gets the durable backoff.

## Measurements

Peak resident memory added by one decode, in a fresh interpreter, after imports. Measured
on 2026-10-11 by `tests/integration/test_cpu_decode_budget.py` (macOS, arm64) and by the
same operations inside the local stack's CPU worker container (Linux, Pillow from the
worker image).

| Image | Operation | Decoded | Linux | macOS |
| --- | --- | --- | --- | --- |
| PNG 6928×4618 (32 MP) | perceptual hash | 32 MP | +187 MiB | +172 MiB |
| PNG 6928×4618 (32 MP) | thumbnail | 32 MP | +147 MiB | +149 MiB |
| PNG 6928×4618 (32 MP) | OCR decode | 32 MP | +184 MiB | +183 MiB |
| PNG 9000×6667 (60 MP) | any | refused | — | +1 MiB |
| PNG header 12600×9524 (120 MP) | any | refused | — | +1 MiB |
| JPEG 9000×6667 (60 MP) | perceptual hash | 0.9 MP (1/8) | +41 MiB | +22 MiB |
| JPEG 9000×6667 (60 MP) | thumbnail | 0.8 MP (1/8) | +13 MiB | +11 MiB |
| JPEG 9000×6667 (60 MP) | OCR decode | 15 MP (1/2, greyscale) | +56 MiB | +55 MiB |
| JPEG 12600×9524 (120 MP) | perceptual hash | 1.9 MP (1/8) | +44 MiB | +27 MiB |
| JPEG 12600×9524 (120 MP) | OCR decode | 30 MP (1/2, greyscale) | +91 MiB | +90 MiB |

A decode that cannot use a reduced scale costs about 5.7 bytes a pixel on Linux: 3 for the
RGB image, 1 for the greyscale copy the hash and OCR work on, and the decoder's own
buffers. Before this budget, a 60 MP PNG added about 320 MiB, and Pillow decoded anything
up to 400 MP.

Worst case at the default: 200 MiB idle + 4 × 187 MiB ≈ 950 MiB, inside 1 GiB. Tesseract
starts only after the full decode is released, on at most 4096 × 4096 greyscale pixels.

The 32 MP default refuses an A4 page scanned at 600 dpi as PNG or TIFF (about 35 MP); the
same page as JPEG, or at 300 dpi, is read. Raise `cpu_decode_max_pixels` only with the
memory to match: each additional megapixel costs four concurrent decodes about 22 MiB.

## Decisions

One per question of issue #8.

1. **Perceptual hash of very large images.** Decode through the bounded helper: reduced
   scale for JPEG, refusal above the pixel budget, a recorded note in place of the hash.
   Not the embedded EXIF thumbnail (absent from most PNG and HEIF, and attacker
   controlled), and not skipping every large image (modern camera JPEGs hash in tens of
   MiB through draft mode). The issue proposed 64 MP from 4 bytes a pixel and no idle
   allowance; measured, that is about 350 MiB a decode, so the default is 32 MP.
2. **OCR of long scanned PDFs.** Stop at the text cap, the first 3 pages and 6 embedded
   images; check each page image's declared size before decoding it; 60 s of Tesseract an
   image, keeping the text read before a timeout; record the limit.
3. **Maximum file size.** The mechanism, not the threshold: a file that can never be staged
   fails at once with its size and the bound, before any provider read, with no backoff.
   `ingest_max_bytes` is the hook for a per-file ceiling and is unset. Such a file has no
   content object, so it appears with the failed sources (`/jobs`), not in the review queue.
4. **Video and audio.** Rules only, unchanged.
5. **What may reach the model.** Unchanged inputs (a 1024 px JPEG, 3,000 characters, never
   the file); the answer is bounded by `remote_max_output_tokens`. Content a limit kept
   unread goes to review as `resource_limit`, never to a remote call that sees only names.
6. **Content identity for big files.** Keep the SHA-256 of the full staged file: the
   content-ledger spec requires a strong fingerprint, and provider digests are not proof.
7. **Sampled metadata reads.** Deferred; they pay off only if 6 changes or large videos
   become the bottleneck.
8. **Scaling within the budget.** Not decided here. The decode budget is per worker, so it
   holds for any replica count.

### Not decided here

- A per-file size ceiling (`ingest_max_bytes`), per media kind or not, and whether oversized
  files get a slow lane or fail visibly; whether a file refused before staging must appear
  in the review queue, which needs review items that can name a source instead of a
  content object.
- Video and audio beyond rules: container metadata (a new parsing dependency) or one
  keyframe (ffmpeg in the image).
- Which media kinds may reach the remote tier (video, audio and other kinds go today with
  names only), and the production spend cap.
- Scaling within 2 vCPU: a second replica, autoscaling on queue depth, or slower
  throughput; and the CPU worker's requests and limits in the deployment repository.
- Confirming or tuning the defaults of `cpu_decode_max_pixels` (32 MP) and
  `remote_max_output_tokens` (1024).
