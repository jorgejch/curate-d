# Design

## Context

See proposal.md for why. What shapes the approach:

- **Keys are per source, not per content.** `derive_staging_key(source.id)` names one
  source's bytes. A newer version of the same source, a restage after eviction, or a
  deliberate re-ingest (`generation`) publishes a new attempt under the same key, and
  `publish()` releases whatever was registered there first. A release that names only the
  key can therefore delete a newer version's bytes.
- **Readers of a staged version.** `ingest_source` (I/O) stages attempt *A* of key *K* and
  enqueues `fingerprint_source` (CPU), which reads *A* in `_inspect_step`. On success it
  enqueues `classify_content` (CPU) under `classification_workflow_id(fingerprint)`.
  That ID collapses identical content found at many sources, so classification reads the
  `StagedRef` of whichever source enqueued it first; every other source's copy is read by
  its fingerprinting alone. `classify_content` reads its copy once, in `_prepare_step`;
  `classify_remote` and `release_holds` carry a prepared request and read no bytes.
  `reclassify_rejected` builds a `StagedRef` from the ledger and enqueues
  `classify_content` under its own ID; `_prepare_step` already treats a missing entry as
  "content unavailable" and classifies from names and media type. Placement reads the
  source provider, not staging. No other code reads staging.
- **Authority.** The CPU profile holds the reader identity only: no release or eviction
  method (`docs/staging.md`, "Authority and data flow"). Every release has to run on the
  I/O profile, which holds the writer.
- **Release is cache state.** `occupancy.release` changes the cache index and the object
  store, both retry-safe operational state; no ledger row changes, so no datasource
  transaction is involved, and nothing about source-release authority changes.

## Goals / Non-Goals

**Goals:**

- Release attempt *A* of key *K* once every workflow that reads *A* has finished, on every
  path those workflows return by.
- Never release a version other than the one consumed.
- Keep a release failure out of the consumer's outcome; leave the existing backstops
  (idle-sweep age eviction, admission reclamation, orphan reconciliation) unchanged.

**Non-Goals:**

- Releasing for a consumer that ends by exception or cancellation. Such a workflow is the
  one an operator resumes, forks or rewinds, and the recovery sweep forks; its re-run
  reads the bytes again, so they stay until age eviction.
- Keeping bytes for `reclassify_rejected`. It already tolerates a missing entry, and today
  it finds bytes only when the entry happens to be younger than the eviction age.
- Batching releases, a new queue, or a new setting.

## Decisions

### 1. `StagedRef` names the attempt

`StagedRef` gains `attempt_id: UUID | None = None`, set by `stage_source` from the
published `StagedObject`. `None` means "attempt unknown": no release on consumption, which
is what the old code did, so a `StagedRef` pickled before this change (a pending
`fingerprint_source` or `classify_content`) replays as before (`DomainModel.__setstate__`
fills the default). `reclassify_rejected`'s ledger-built refs carry `None` too, so a
reclassification never releases an entry a concurrent re-ingest of the same bytes may be
about to read.

*Alternative:* match on `sha256`. Rejected: a re-ingest of identical bytes (a full rescan's
`generation`, a restage) registers a new attempt with the same digest, and a sha-matched
release from the old run would delete the bytes the new run is about to read.

### 2. Release conditioned on the attempt

`StagingOccupancy.release(key, *, attempt_id=None)`. With `attempt_id`, under the writer
lock it looks the key up and returns a no-op `ReleaseReport` unless that attempt is the
registered one; otherwise it proceeds exactly as today (evict, or unpublish only when a
download holds the entry; delete the bytes after the unregistration commits). Every index
mutation takes the writer lock, so the attempt cannot change between the lookup and the
eviction. Without `attempt_id` the method is unchanged, for `publish()` and the
source-change path in `stage_source`.

### 3. An I/O workflow carries the release out

New module `workflows/staged_release.py`:

- `release_staged_entry(key: str, attempt_id: UUID) -> ReleaseOutcome`, a workflow with
  one step, `_release_step`, that runs `writer.occupancy.release(key, attempt_id=...)` on
  the staging loop and logs `staging.released` (`staging_key`, `attempt_id`, `deleted`,
  `freed_bytes`).
- `enqueue_staged_release(staged: StagedRef)`, called from CPU workflow bodies; a no-op
  for `attempt_id is None`. It enqueues on `maintenance` under the deterministic ID
  `release-staged:<key>:<attempt hex>`, so a replayed or doubled enqueue collapses, with
  `SetEnqueueOptions(priority=RELEASE_PRIORITY)`. DBOS runs un-prioritised work first, so
  scheduled housekeeping (token refresh, sweeps) never waits behind a backlog of releases;
  at most one in-flight release, a single writer-lock holding and one delete.

Inputs are a string and a UUID, so the checkpoint survives any model change. The module is
imported by `workflows/ingest.py` and `workflows/classify.py` and listed in
`workflows/registry.py`, so every process registers it; only the I/O profile dequeues
`maintenance`.

*Alternatives:* release inside `_inspect_step`/`_prepare_step` (the CPU profile has no
release authority); a "consumed" column the CPU sets and the idle sweep acts on (gives the
reader a write on the index, needs a migration, and waits up to a sweep interval); the
`stage` queue (partitioned by provider, which neither consumer knows, and sized by upload
capacity); a dedicated queue (a `runtime/queues.py` registration change for a short,
infrequent effect that `maintenance` at a low priority already serves).

### 4. Where each consumer releases

`classify_content` releases the `staged` it was given on every path it returns by:
`REUSED` (it read nothing), `ESCALATED` (the prepared request holds what the remote tier
needs), `CLASSIFIED` and `REVIEW`. An exception or cancellation releases nothing.

`fingerprint_source` releases its own `staged`:

| Path | Release? | Why |
|---|---|---|
| `FINGERPRINTED`, classification was handed another copy | yes | nothing else reads this copy |
| `FINGERPRINTED`, classification was handed this copy | no | `classify_content` releases it |
| `SUPERSEDED`, record found the source moved on | yes | the newer version stages a new attempt |
| `UNREADABLE`, the snapshot failed verification | yes | nobody can use these bytes |
| `SUPERSEDED`, newer version already in the key | no | attempt is gone; the release would no-op |
| `RESTAGED` / `UNREADABLE` after a missing entry | no | nothing is staged |

### 5. Did classification get this copy?

After enqueueing, `fingerprint_source` calls `DBOS.get_workflow_status_async` on the
classification ID. DBOS records that call as a step (`DBOS.getStatus`), so a replay sees
the same answer. The status carries the workflow's inputs as written at creation, which
never change; the copy is classification's when a `StagedRef` among them has this
`attempt_id`. No status (the classification workflow was pruned from history) means it
finished long ago and reads nothing more: release.

*Alternatives:* the `newly_created` flag `record_fingerprint` returns (wrong when the first
source's workflow dies between recording the content and enqueueing classification, and
when a pruned classification is re-created by a later source); `classify_content`
releasing every linked source's copy (attempt IDs are not in the ledger, and a key-only
release races a restage); `fingerprint_source` awaiting classification (holds a
`fingerprint` slot for the length of classification).

### 6. A failed release

`_release_step` retries on `StagingError` (DBOS step retries, 3 attempts, 1 s then 2 s),
and never on anything else: a process with no staging writer is a deployment error that
fails the workflow visibly. Once retries are exhausted, the workflow catches
`DBOSMaxStepRetriesExceeded` (public `dbos.error`), logs `staging.release_failed`
(WARNING; `staging_key`, `attempt_id`) and returns `FAILED`. The consumer only enqueued the
release, so its outcome is untouched. A retry after a failed deletion finds the
registration gone and no-ops; the orphaned bytes go at the next reconciliation (every
idle-sweep tick). A failure before the unregistration leaves the entry registered until age
eviction or admission reclamation. Both backstops stay as they are.

### 7. DBOS departures

None. The release is an external effect in a step, never in a replayed workflow body;
there is no ledger mutation; the workflow ID is deterministic; enqueueing from a workflow
body and reading another workflow's status from one are the skill's documented,
checkpointed patterns.

## Risks / Trade-offs

- [One extra workflow per consumed file] → a few system-table rows each, pruned with the
  rest of the workflow history; a release is one writer-lock holding and one delete.
- [Rewinding or forking a `classify_content` that already released] → its re-run
  classifies from names and media type (the existing missing-entry path); restage the
  source (a full rescan) to classify from the bytes. Recorded in `docs/rewind.md`.
- [Two sources with identical content fingerprinted at once] → only the copy
  classification was handed waits for classification; the decision reads a fixed workflow
  input, so neither copy is released early.
- [`reclassify_rejected` almost never finds bytes now] → it already handles that; its
  outcome then rests on names and media type, as it did for any entry past the eviction
  age.
- [The release backlog on `maintenance` under a large ingest] → releases run behind
  scheduled housekeeping, one per I/O worker at a time; staged bytes wait at most as long
  as they did before (age eviction still applies).

## Migration Plan

No migration and no setting. In-flight `fingerprint_source` and `classify_content`
workflows carry `StagedRef`s without `attempt_id` and release nothing, as before. A
rollback leaves enqueued `release_staged_entry` workflows on a version nothing runs; the
entries they name are reclaimed by age.
