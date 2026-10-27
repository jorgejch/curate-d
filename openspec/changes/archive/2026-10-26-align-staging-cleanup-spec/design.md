# Design

## Context

See proposal.md for why. What the code does today, which the corrected requirement states:

- **Release on re-stage.** `publish()` releases whatever its key holds before reserving the
  new upload (`staging/publish.py`, the `occupancy.release(key)` call at the top of
  `publish`), so a new version never loses the immutable registration to its predecessor.
- **Release on source change.** `stage_source` releases the entry it just published when
  the published digests disagree with the listing (`ingest/pipeline.py`, after
  `source_change`); a size mismatch is caught before anything is uploaded.
- **Age eviction.** The idle sweep (`occupancy.sweep`, every idle-cleanup tick) reconciles
  orphans and unregisters entries older than `CURATE_D_STAGING_MAX_AGE_HOURS`, skipping
  any a download holds; bytes are deleted after the unregistration commits.
- **Reclamation under pressure.** Admission that does not fit (`_admit` → `_reclaim`)
  reconciles orphans, then unregisters aged entries, then the oldest unheld entries up to
  the shortfall, and refuses if the declaration still does not fit.
- **Failure.** A failed sweep is logged (`staging.cleanup_failed`) and the next tick repeats
  it; a failed deletion after a committed unregistration leaves an orphan the next
  reconciliation collects.

## Goals / Non-Goals

**Goals:** make the requirement match the code, keeping every other obligation it carries.

**Non-Goals:** any code change, and release on consumption.

## Decisions

### Readers do not release

One staged entry is read by more than one step: `fingerprint_source`'s `_inspect_step`,
then `classify_content`'s `_prepare_step`, and later `reclassify_rejected`'s
classification when the bytes are still there. Classification is keyed by content
fingerprint, so its reader is not even the same workflow lineage as every fingerprinting
of that content. A reader that finds its entry missing gets a miss and never fetches from
the source (the "Verified seekable read snapshots" requirement): fingerprinting asks for a
bounded re-stage, classification works from names and media type. Releasing on one
reader's completion could therefore strand a later reader, and deciding that every reader
has finished needs coordination the pipeline does not have. Age eviction and admission
reclamation bound how long consumed bytes stay instead.

*Alternative considered:* release once the last reader finishes (a release workflow on the
I/O profile, named by upload attempt). Rejected by the user: the stranding risk outweighs
the storage it saves.

### Requirement text length

The modified requirement keeps every SHALL/MUST and stays under 500 characters for
`openspec validate --all --strict`; the detail moved into scenarios. The phrase about
minimum-duration charges is dropped from the text: "no retention tiers or object versions"
is the obligation it explained.
