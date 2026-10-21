# Design

## Context

See proposal.md for motivation. Current state on `dev` (ab2d1ed, after #63 and #64):

- `PostgresStagingCacheIndex.writer_lock()` (`cache_index.py`) opens `engine.begin()`, takes
  `pg_advisory_xact_lock(cdsw, 0)` and yields the connection; the transaction commits when
  the block exits. Re-entrant for the holding context via a `ContextVar`. `evict()` and
  `reconcile()` claim entries with `pg_try_advisory_xact_lock(cdse, blake2b-4(key))` on that
  same connection, so each claim lasts to the holding's commit.
- `StagingOccupancy.reserve()` (`occupancy.py`) wraps `_admit()` and the caller's block in
  one `writer_lock("occupancy.reserve")`. `_admit()` measures the listing, and under
  pressure runs `_reclaim()`: `reconcile()` (orphan deletes under entry claims), a second
  listing, then `_drop()` on aged and oldest entries, which unregisters and deletes bytes
  inside the holding because "admission cannot wait for its own commit". `sweep()` and
  `release()` already unregister under the lock and delete after the commit.
- `publish()` (`publish.py`) does `release(key)`, then `async with reserve(size)`: upload
  on the store, `index.register(staged)` (re-entrant holding), exit; then `lookup(key)` to
  confirm the attempt won. One `staging.reservation_phases` event per reservation.
- `hold_for_download()` takes the blocking shared entry lock, then reads the row; the
  reader's wait ends with the claimant's transaction.
- `reconcile()`'s premise, stated in its docstring: under the writer lock no upload is in
  flight, so a listed object without a row is settled. That premise is what forces the
  lock to span the upload today.
- `publish()` runs on the worker's staging event loop (`worker/main.py` `submit`), called
  from the `_stage_step` DBOS step; the provider fetch lands on local scratch before
  `publish()` starts, so the upload reads a local file. The ingest workflow retries
  `StagingCapacityExhaustedError` with a durable backoff and fails the source on any
  other `StagingError`. A failed source is retried by the next full rescan.
- Uploads and downloads run under `_transfer_deadline`, `staging_transfer_timeout_seconds`
  (300 s). `asyncio.timeout` cancels the transfer; a multipart in flight is aborted.
- The staging engine (`factories._build_engine`) is a pool of 5 + 10 overflow, separate
  from DBOS's. Readers hold one connection per in-flight snapshot download.
- Entry lock ids are 4-byte; `runtime/lease.py` uses the single-`bigint` advisory form,
  a different lock space (`objsubid = 1`), for the executor, version and promotion leases.
- Tests: `tests/unit/staging/test_occupancy.py` (`FakeCacheIndex` records lock depth per
  operation; `LockDepthStore` records depth per delete), `test_cache_index.py` (scripted
  engine for timings, unreachable engine for typed failures), `test_publish.py` over
  `tests/stubs/staging.py`'s `MemoryIndex`/`MemoryStore`; integration
  `test_staging_coordination.py` (two engines as two pods, fake store, fixed colliding
  key pair, `_BLOCKED_FOR = 0.25`) and `test_staging_occupancy.py` (two engines + RustFS).
  `tests/integration/test_migrations.py` round-trips every migration.
- `docs/staging.md` "Lock contention events" documents the events and queries, and
  "Revisit threshold for #1" the threshold set. `docs/staging.md` "Authority and data
  flow" says the writer holds the lock across admission, upload and registration.

## Goals / Non-Goals

**Goals:**

- A reader's wait on an entry lock is bounded by one reclamation holding, never by a
  transfer, for every way a writer can hold that lock: an orphan under the reader's key,
  the reader's key evicted, a colliding key.
- Admissions on different pods proceed concurrently; a pod waits only for other pods'
  admission and registration holdings and sweeps.
- The bound holds exactly as today across pods: no two admissions spend the same headroom,
  and a crashed pod's headroom returns within a bounded, database-judged time.
- Every guarantee in the current spec is kept: immutable registration, no partial object
  readable, no registration naming deleted bytes, reader protection, prompt release,
  verified reconciliation, the existing events and their fields.
- Every property above is pinned by a deterministic test: unit against the fakes and
  integration against PostgreSQL and RustFS, none dependent on production load.

**Non-Goals:**

- Widening the 4-byte entry lock id. Once the claim lasts only for a reclamation holding,
  a collision costs seconds, not a transfer; widening would need a new lock space that the
  `bigint` leases already occupy. Recorded below as rejected.
- Bounding a single reclamation holding by chunking reconciliation or eviction. The
  holding is bounded by object-store calls (listing pages and serial deletes) and is the
  residual reader-wait bound; chunking is a follow-up if the readings ever show it.
- A reader-side `lock_timeout` (turns a bounded stall into a failed download).
- Changing the DBOS step boundary, the ingest workflow's capacity retry, or the worker's
  staging loop.
- Metrics or tracing exporters; pod identity on events.

## Decisions

**D1. Address reader blocking and upload serialization together, through one mechanism.**
The root cause of both is that the reservation is the lock: with nothing durable recording
an in-flight upload's claim on headroom, the lock must span the upload to keep the bound,
and every entry lock on that transaction inherits the upload's lifetime. A durable
reservation lets the lock end at admission, which ends the entry locks there too. Fixing
reader blocking alone (reclaim in a separate holding that commits before admitting,
#1's 2026-10-11 proposal) was evaluated: it is smaller, but it leaves the inline
reclamation fallback that still blocks readers for a transfer when another pod spends the
freed headroom in between, leaves every sweep, release and registration queued behind
transfers, and would be deleted by the serialization fix. Fixing serialization without a
reservation record (optimistic admission against the listing) was rejected: two pods could
admit against the same headroom, breaking the bound by up to pods × max object. One change
with two work streams (index, occupancy) is therefore the recommendation.

**D2. Reservations are rows in the ledger database, table `staging_reservations`.**
Columns: `attempt_id UUID PRIMARY KEY`, `staging_key TEXT NOT NULL`, `reserved_bytes BIGINT
NOT NULL`, `reserved_at TIMESTAMPTZ NOT NULL DEFAULT now()`, `expires_at TIMESTAMPTZ NOT
NULL`. Migration `0021_staging_reservations`, mirrored in `ledger/tables.py`. The table
holds only in-flight uploads, so no index beyond the key. Alternatives: (a) marker objects
in the bucket named `<key>/<attempt>.r<bytes>` so the listing itself carries reservations;
rejected because expiry would rest on object-store timestamps against pod clocks, the
marker cannot be consumed atomically with the row insert, and every admission would pay a
PUT. (b) Session advisory locks per reservation on a dedicated connection, as
`runtime/lease.py` does for executors, so liveness is immediate; rejected because each
in-flight upload would pin a pooled connection for its duration, which is the pressure the
reader hold was deliberately narrowed to avoid, and the upload is already deadline-bounded
so a TTL is tight enough. (c) Counting in-flight multipart uploads from
`list_multipart_uploads`; rejected: single-request uploads are invisible and no declared
size is available.

**D3. Expiry is judged by the database clock.** `expires_at` is written as
`clock_timestamp() + :ttl` and compared as `expires_at > now()` in every read, so pods with
skewed clocks agree. Not `now() + :ttl`: `now()` is the transaction's start, before the
writer-lock wait and reclamation, so a slow admission would commit an already-expired
reservation (found in review on #65). `now()` is fixed per transaction, so a TTL of zero is
live in the transaction that wrote it and expired in the next; the tests use that. TTL is `2 × staging_transfer_timeout_seconds`, derived in
`factories.build_writer_backend` and passed to `StagingOccupancy(reservation_ttl=...)`: one
transfer deadline covers the upload, the second covers lock waits and scheduling. No new
setting; the derivation is documented in `docs/staging.md`'s configuration table under the
transfer timeout. A crashed pod's headroom returns within 10 minutes by default, inside the
ingest workflow's capacity backoff (which tops out at 15 minutes).

**D4. `reserve()` becomes two short holdings and yields a `Reservation`.**

```python
async with occupancy.reserve(key, attempt_id, size) as reservation:   # holding 1 ended
    staged = await store.upload(...)                                   # no lock held
    await reservation.register(staged)                                 # holding 2
```

Holding 1 (`writer_lock("occupancy.reserve")`): measure (listing total + live reserved
bytes; expired rows are not counted), reclaim under pressure (its reconciliation deletes
expired rows), insert this reservation; commit.
`AdmissionReport` gains nothing new; `OccupancyReport` gains `reserved_bytes`, and
`measure_occupancy(store, index)` keeps its signature and reads `reserved_bytes` from the
index when it exposes `total_reserved_bytes()`, else 0. Holding 2
(`writer_lock("occupancy.register")`): delete the reservation `WHERE attempt_id = :id AND
expires_at > now()`; if no row, raise `StagingReservationLostError` (subclass of
`StagingTransientError`) after the holding and log `staging.reservation_lost` at WARNING;
else insert the registration with `ON CONFLICT (staging_key) DO NOTHING`; commit.
`Reservation.registered` records whether holding 2 ran. On block exit without a
registration (exception, cancellation, or the caller simply not registering), the
reservation is abandoned in a third holding (`writer_lock("occupancy.abandon")`), best
effort: a failure there is logged at WARNING (`staging.reservation_abandon_failed`) and
left to expiry, and never masks the block's own exception. Abandonment after a
`CancelledError` runs inside `asyncio.shield` with a short bound so a drain cannot leave
the row behind by cancelling the cleanup itself. The index gains `reserve(key, attempt_id,
reserved_bytes, ttl)`, `consume_reservation(attempt_id) -> bool`, `forget_reservation
(attempt_id)` and `total_reserved_bytes()` on `CoordinatedStagingIndex`; `register(obj)`
stays as the unreserved registration for tests and the `stage` helper, with its docstring
restating that production registration goes through a reservation. Alternative: keep
`reserve()` yielding `AdmissionReport` and have `publish()` call `index.register(staged,
reservation=...)`; rejected because the reservation's lifecycle then depends on the caller
remembering to consume it, which is the contract-on-the-caller `reconcile()`'s docstring
already complains about.

**D5. Admission's eviction uses the stronger ordering.** With the lock no longer spanning
the upload, `_admit()` unregisters aged and oldest entries under holding 1 and deletes
their bytes after it commits, exactly as `sweep()` does. `_drop()` and its "admission
cannot wait for its own commit" rationale go away; `_reclaim()` returns the unpublished
registrations and `reserve()` deletes them after the commit, before yielding. A deletion
failure after the commit abandons the reservation and raises `StagingObjectStoreError`,
as today's "raised after the admission transaction has committed". The accounting treats
unpublished bytes as freed when deciding admission; a later delete failure leaves orphans
that count against the next admission's listing, never a row naming absent bytes. Orphan
deletes in `reconcile()` stay under their entry claim and inside holding 1, because that
claim is what protects a reader mid-download of a released attempt. Entry locks are
therefore held across the listing and orphan deletes only; the evictions' deletes leave the
holding.

**D6. Reconciliation spares live reservations.** `reconcile()` first expires dead
reservations, then reads the registered object keys and the live reserved object keys
(`<staging_key>/<attempt_id hex>`) on the writer connection, and treats both sets as
protected. Its docstring's premise changes from "no upload is in flight under the lock" to
"every in-flight upload is reserved, and the registration that consumes a reservation runs
under this lock". A completed object whose reservation has expired is an orphan and is
collected; the registrant that then arrives finds no reservation and publishes nothing
(D4), so a row never names bytes reconciliation removed. The double count of a completed,
not-yet-registered object (in the listing and in reservations) is conservative and lasts
until holding 2.

**D7. Phases and callers keep their names; the docs state the new sums.**
`staging.reservation_phases` keeps `admission_seconds` (holding 1 from acquisition
through the decision and the reservation insert), `upload_seconds` (upload outside the lock), `registration_seconds`
(from upload end to the registration insert, now including the wait for holding 2) and
`commit_seconds` (insert to holding 2's end). For any admitted reservation, the
phases that started add up to its lifetime after the admission lock wait, no longer to one
`held_seconds`: a phase that never started is null, and one cut short by a failure or
cancellation runs to the reservation's end, so it absorbs the `occupancy.abandon` holding.
The admission holding's commit and, after an eviction, the deletion of the evicted bytes
fall between the insert and the upload and are in no phase. `staging.writer_lock_held`
keeps `occupancy.reserve` for holding 1, so the "Revisit threshold" queries and the
duty-cycle query keep working with their meaning sharpened (the duty cycle no longer
includes transfers); it gains `occupancy.register` and `occupancy.abandon`. No field is
removed or renamed. `docs/staging.md` "Lock contention events" and "Authority and data
flow" are rewritten to describe the two holdings.

**D8. The superseded deferral is recorded, not erased.** The `occupancy.py` module
docstring keeps a paragraph: reviewed and kept on 2026-09-15 for the reasons it gave,
superseded on 2026-10-18 by this change because the workload could not produce the
measurement and the defect was understood; it points at the reservation table as the
mechanism. `cache_index.py`'s docstring drops the "deferred ... until the measurement"
sentence and states the new entry-lock bound. `docs/staging.md`'s "Revisit threshold for
#1" becomes "Lock contention alarms": its four criteria are kept verbatim as regression
alarms with a sentence of history (what they were for, when and why that changed), and the
expected post-change reading is stated: `occupancy.reserve` holdings bounded by reclamation,
registered reader waits bounded the same way, and a 60-second registered reader wait now a
defect rather than a cap. #4's first dev reading stays useful as a regression check and is
not a prerequisite for anything.

**D9. `StagingReservationLostError` is a `StagingTransientError`.** The ingest workflow
fails the source on any `StagingError` other than capacity exhaustion and the next full
rescan retries it; a lost reservation is rare (a pod paused or partitioned past twice the
transfer deadline) and this is the same outcome as a transfer deadline hit today. Not
retried inline: the bytes have to be re-uploaded anyway and the capacity backoff is the
wrong wait. The error's bytes are deleted best-effort after holding 2 (the object may
already be gone; delete-absent is success); a failed delete leaves an orphan.

**D10. Unit fakes grow the reservation surface.** `FakeCacheIndex`
(`test_occupancy.py`) and `MemoryIndex` (`tests/stubs/staging.py`) gain reservations keyed
by attempt with an injected clock for expiry, so expiry and lost-reservation paths are unit
tests. `test_admission_holds_the_writer_lock_across_the_publication` and
`test_admission_still_deletes_under_the_lock_it_holds_for_the_upload` are inverted: lock
depth inside the block is 0 and evictions' deletes run at depth 0. `LockDepthStore` is
reused as is.

**D11. Integration tests use two engines as two pods and TTL 0 for expiry.** The existing
fixtures (`index`, `other_index`, `colliding_keys`, `_BLOCKED_FOR`) are reused. "Not
blocked" is asserted as the reader or the second admission completing within
`_BLOCKED_FOR` while the first `reserve()` block is still open; "blocked" keeps the
existing `wait_for(shield(...))` pattern. Expiry uses `reservation_ttl=timedelta(0)`: the
reservation is live in the transaction that wrote it and expired in every later one, with
no sleeping. Crash is a reservation written through the index with no block to abandon it.

**D12. No DBOS change.** The cache index is retry-safe operational state outside the DBOS
datasource (`docs/staging.md`, "DBOS step boundaries"); `_stage_step` is one step and stays
one step. Checked against the `dbos-python` skill: no workflow, step, queue or schedule
changes, so no departure to record in `CLAUDE.md`.

## Risks / Trade-offs

- [A pod paused or partitioned for more than 2 × the transfer deadline loses its
  reservation and its upload is discarded] → Rare, bounded, logged
  (`staging.reservation_lost`), and the source is retried by the next rescan, the same path
  a deadline hit takes today. The TTL derivation is documented so an operator raising the
  transfer timeout raises it too.
- [Abandonment after cancellation needs a database round trip during a drain] → Shielded
  with a short bound; a failure is logged and the row expires. The drain already waits
  `_drain_seconds` for the sweep.
- [Two holdings per publication instead of one, plus an abandon holding on failure] →
  Each is a few statements; the lock is no longer held for transfers, so total lock
  occupancy falls by orders of magnitude. The duty-cycle query shows it.
- [Conservative double count between upload completion and registration] → Lasts for one
  short holding's wait; the bound is never exceeded, only under-used briefly.
- [A delete failure after holding 1 commits leaves evicted orphans and an abandoned
  reservation] → The same orphan class `sweep()` already produces on a failed delete; the
  next reconciliation collects them, and the admission fails loudly as today.
- [An old-build I/O pod runs beside a new-build one during the rolling restart] → The old
  build's `reconcile()` does not know reservations, so it can collect a new-build pod's
  completed, not-yet-registered object, after which the new pod's registration (whose
  reservation is still live) writes a row naming absent bytes; and the old build's
  admission does not count new-build reservations, so the bound can be overspent by one
  object. Both last for one rolling restart of the I/O StatefulSet. The row naming absent
  bytes is repaired by the reader path (`docs/staging.md`, "Expired or missing staged
  object": a miss invalidates the entry and the I/O side re-stages), the overspend by the
  next admission's listing. Recorded in `docs/staging.md` under the rollout note rather
  than guarded in code.
- [Fixed colliding-key pair in tests leaves rows on an interrupted run] → The existing
  `colliding_keys` fixture already deletes them first; reservations get the same cleanup.

## Migration Plan

1. Migration `0021_staging_reservations` runs with the ordinary `make migrate` before the
   build is deployed; it creates one empty table and is reversible (`downgrade` drops it).
2. Deploy I/O pods in one rolling restart (the StatefulSet's default). CPU pods need no
   change but pick up the build with it.
3. Rollback: revert the build; the table may stay (unused) or be dropped by the downgrade.
   Reservations left by the new build expire and are ignored by the old build's
   accounting; their objects are orphans the old build's reconciliation collects.
4. After merge: comment on #1 that the defect is fixed by this change and close it with
   `state_reason: completed`; comment on #4 that the first dev reading is now a regression
   check and keep it open.

## Open Questions

None that affect the specs, approach or tasks.
