# Proposal

## Why

`StagingOccupancy.reserve()` holds the global PostgreSQL advisory writer lock across
admission, the upload it admits and the registration that publishes it, and the exclusive
entry locks reclamation takes live on that same transaction. Two problems follow, and #1
records both:

1. **Reader blocking.** A CPU reader of a healthy registered key stalls behind an unrelated
   upload whenever admission's reclamation claimed that key: to delete an orphan attempt
   under it, to evict it, or because the key's 4-byte entry lock id collides with a claimed
   one. The stall lasts until the upload's transaction ends, up to
   `staging_transfer_timeout_seconds` (300 s by default). A reader of an evicted key waits
   the same time to receive a cache miss.
2. **Global upload serialization.** The cluster stages one object at a time, whatever the
   number of I/O pods, and every sweep, release and registration queues behind the transfer
   in flight.

The 2026-09-15 decision kept this and deferred the fix until measured upload concurrency
showed the lock binding (#4). #63 landed the measurement (`staging.writer_lock_held` with
`entry_claims`, `staging.reservation_phases`, `staging.reader_lock_wait`) and one threshold
set in `docs/staging.md`. The current workload cannot produce a representative reading,
so waiting on it defers a known correctness-and-concurrency defect indefinitely. The owner
has decided to supersede the deferral and fix the problem now, with deterministic tests as
the evidence instead of production metrics.

The two problems share one root cause: the reservation is the lock. Nothing durable
records an in-flight upload's claim on headroom, so the only way to keep two pods from
spending the same headroom is to hold the lock until the bytes show up in the listing, and
every lock taken on that transaction inherits the upload's lifetime. They are therefore
addressed together, as one change, through one mechanism: a durable reservation record.

## What Changes

- **Reservation rows.** A new `staging_reservations` table records each admitted upload:
  staging key, attempt id, reserved bytes and a database-clock expiry. Admission counts live
  reservations as occupancy alongside the object-store listing, so two pods cannot spend
  the same headroom without the lock spanning the upload.
- **Narrowed writer lock.** `reserve()` takes the writer lock for two short holdings: the
  admission decision (measure, reclaim, insert the reservation; commit) and the registration
  (consume the reservation, insert the row; commit). The upload runs outside any lock.
  Entry locks taken by reclamation end at the admission commit, so a reader waits at most
  for one reclamation holding, never for a transfer.
- **Eviction ordering made uniform.** Admission unregisters under the lock and deletes
  bytes after the commit, the ordering `sweep()` and `release()` already use. Orphan deletes
  stay under their entry claim.
- **Reservation lifecycle.** A reservation is consumed by the registration that publishes
  its attempt, forgotten when the reservation block exits without registering (failure,
  refusal after admission, cancellation), and expired by the database clock after a TTL
  derived from the transfer timeout when its pod died. Reconciliation spares objects named
  by a live reservation and collects them once it has expired. A registration whose
  reservation has expired registers nothing, removes its bytes and fails as a retryable
  staging error, logged as `staging.reservation_lost`.
- **Observability kept, semantics updated.** Event names and fields from #63 are unchanged.
  `staging.writer_lock_held` gains the callers `occupancy.register` and
  `occupancy.abandon`; `occupancy.reserve` now labels the admission holding alone.
  `staging.reservation_phases` still reports admission, upload, registration and commit;
  they now add up to the reservation's lifetime rather than to one holding's
  `held_seconds`. `OccupancyReport` carries `reserved_bytes`.
- **Deferral record.** The 2026-09-15 decision text in the `occupancy.py` and
  `cache_index.py` module docstrings and the "Revisit threshold for #1" section of
  `docs/staging.md` are rewritten to record that the deferral was superseded on 2026-10-18
  by this change, keeping the original date, reasoning and the threshold set, which become
  regression alarms rather than a revisit trigger.
- **Deterministic tests.** Unit tests against the in-memory fakes and PostgreSQL + RustFS
  integration tests pin: concurrent admissions across two pods, the bound holding against
  in-flight reservations, readers unblocked while an upload is open, expiry and
  lost-reservation handling, cancellation, and the migration.
- **BREAKING** for direct callers of `StagingOccupancy.reserve()`: the block no longer holds
  the writer lock, and registration must go through the yielded reservation rather than
  `index.register()`. `publish()` is the only production caller.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `content-staging`:
  - "Bounded active staging capacity": admission accounts for in-flight reservations across
    workers, not only completed remote occupancy, and a reservation outlives the lock.
  - "Immutable atomic publication": registration consumes the live reservation of its
    attempt in the same transaction; an expired reservation publishes nothing.
  - "Cleanup without active ingestion": a dead worker's reservation expires and its bytes
    are reclaimed afterwards; reconciliation spares objects under live reservations.
  - "Writer-lock holdings are observable": the holding a reservation takes no longer spans
    the upload; registration and abandonment are separate, labelled holdings.
  - "Reservations report their phases": the phases sum to the reservation's lifetime, and
    registration time includes the wait for its own holding.
  - "Reader waits are observable and attributable": a reader's wait is bounded by one
    reclamation holding and never spans an upload, for a claimed key, an evicted key or a
    colliding key.
  - ADDED "Reservations are durable" and "Reservations expire by the database clock": the
    reservation lifecycle and its crash, cancellation and lost-reservation behaviour.

## Impact

- `src/curate_d/staging/occupancy.py`, `cache_index.py`, `publish.py`, `objects.py`,
  `errors.py`, `factories.py` (TTL wiring), `__init__.py` (exports).
- `src/curate_d/ledger/tables.py` and a new migration `0021_staging_reservations`.
- `tests/unit/staging/*`, `tests/stubs/staging.py`, `tests/integration/test_staging_*.py`,
  `tests/integration/test_migrations.py`.
- `docs/staging.md` (data flow, lock contention events, threshold section, recovery),
  `docs/recovery.md` is untouched: no workflow recovery event changes.
- No DBOS change: the cache index is retry-safe operational state outside the DBOS
  datasource, as `docs/staging.md` "DBOS step boundaries" records. The `_stage_step`
  boundary and the ingest workflow's capacity retry loop are unchanged.
- No settings change: the reservation TTL is derived from
  `staging_transfer_timeout_seconds`.
- Issues #1 and #4: this change closes #1's defect; #4 stays open for the first dev reading,
  now a regression check rather than a prerequisite.
