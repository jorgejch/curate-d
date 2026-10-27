# Proposal

## Why

The content-staging requirement "Cleanup without active ingestion" says completed objects
SHALL be deleted promptly when no download uses them. The code does not do that, and should
not: more than one step reads the same staged entry (fingerprinting's inspection and
classification's preparation), and a reader that finds its entry missing gets a miss
rather than fetching from the source. Releasing an entry when one reader finishes could
strand a later one. The spec is corrected to state what the code does, as `docs/staging.md`
already does ("No step releases an entry after its consumers finish").

## What Changes

- The requirement's first sentence is replaced: a completed entry is released when its key
  is re-staged with a new version or its fetched bytes show a source change, and otherwise
  leaves only by idle-cleanup age eviction or reclamation under admission pressure, never
  when its readers finish.
- Every other obligation in the requirement is kept: periodic cleanup without new jobs,
  visible and retryable cleanup failures, and the multipart and abandoned-content
  backstops without retention tiers or object versions.
- Scenarios state each release path and the reason readers do not release.
- No code or documentation change: the code and `docs/staging.md` already behave and read
  this way.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `content-staging`: "Cleanup without active ingestion" states when a completed entry is
  released, and that a reader finishing is not one of those times.

## Impact

- `openspec/specs/content-staging/spec.md` only. No code, API, setting or migration
  changes.
