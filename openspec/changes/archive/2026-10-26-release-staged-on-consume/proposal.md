# Proposal

## Why

The content-staging spec's "Cleanup without active ingestion" requirement says completed
objects SHALL be deleted promptly when no download uses them, but nothing releases a staged
entry once its readers are done. An entry leaves only by idle-cleanup age eviction (24 hours
by default) or under ceiling admission pressure (`docs/staging.md`, "DBOS step
boundaries"), so consumed bytes keep billing and occupying the bound for a day. The code
changes to meet the spec; the spec keeps its text and gains scenarios that make the
behaviour testable.

## What Changes

- A staged version's reference (`StagedRef`) carries the upload attempt that published it,
  so a release can name exactly the version that was consumed and never a newer one
  staged under the same key. The field defaults to "unknown", which keeps the old
  behaviour (no release on consumption) for references pickled before the change.
- Staging occupancy gains a release conditioned on that attempt: it drops the
  registration and deletes the bytes only when the named attempt is still the registered
  one, and keeps the bytes for a download that still holds the entry.
- A new I/O-profile workflow carries the release out. The CPU profile has no release
  authority, so the CPU workflows that consume an entry enqueue it rather than release
  in place.
- `fingerprint_source` enqueues the release of its own staged version once nothing else
  will read it: after it hands the content to a classification that reads another
  source's copy (or reuses an existing one), or when it ends because the bytes are
  unreadable or the source moved on.
- `classify_content` enqueues the release of the staged version it was given on every
  path it returns by, once its cheap tiers have read the bytes or found a classification
  to reuse.
- A release failure is logged as a named event and never fails the consuming workflow;
  age eviction, admission reclamation and orphan reconciliation stay as backstops.
- `docs/staging.md` describes release on consumption instead of "no step releases an
  entry after its consumers finish".

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `content-staging`: "Cleanup without active ingestion" gains scenarios for release after
  consumption, a key restaged to a newer version, a download still holding the entry, a
  failed release, and a consumer that fails or is cancelled.

## Impact

- Code: `src/curate_d/staging/occupancy.py`, `src/curate_d/ingest/pipeline.py`,
  `src/curate_d/workflows/ingest.py`, `src/curate_d/workflows/classify.py`, a new
  `src/curate_d/workflows/staged_release.py`, and its import in
  `src/curate_d/workflows/registry.py`.
- DBOS: one new workflow per consumed staged version, on the I/O profile's `maintenance`
  queue at a priority below scheduled housekeeping. Workflow input `StagedRef` gains a
  defaulted field (pickle-compatible). No new setting, queue, or migration.
- Docs: `docs/staging.md` (release bullet, log events), `docs/rewind.md` (new child
  workflow).
- Tests: unit tests for the conditional release, the release step and the consumer
  decisions; integration tests over the in-memory pipeline and RustFS.
