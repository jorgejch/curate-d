# Tasks

Work streams. Stream 1 is the shared foundation and runs first, alone. Streams 2, 3 and 4
touch disjoint files and start together once stream 1 is done; stream 3's integration
tests (3.9, 3.10) need stream 2 merged, everything else in stream 3 runs against the fakes.
Stream 5 runs last. Each task's test or command is run green before the task is ticked
(`make test` is the unit suite; integration commands need `make test-stack`).

## 1. Foundation: schema, error, protocol (shared; runs first, alone)

- [x] 1.1 Add migration `migrations/versions/0021_staging_reservations.py` creating
      `staging_reservations` (`attempt_id UUID PK`, `staging_key TEXT NOT NULL`,
      `reserved_bytes BIGINT NOT NULL`, `reserved_at TIMESTAMPTZ NOT NULL DEFAULT now()`,
      `expires_at TIMESTAMPTZ NOT NULL`) with module, `upgrade()` and `downgrade()`
      docstrings per CLAUDE.md; mirror the table in `src/curate_d/ledger/tables.py` with
      column comments; verify with `uv run pytest tests/integration/test_migrations.py
      --no-cov -m integration` (upgrade, second upgrade, full downgrade round trip all pass
      with the new revision) and `make lint`.
- [x] 1.2 Add `StagingReservationLostError(StagingTransientError)` to
      `src/curate_d/staging/errors.py` and export it from `staging/__init__.py`; verify
      with a unit test in `tests/unit/staging/test_errors.py` (create if absent) that it
      is a `StagingTransientError` and a `StagingObjectStoreError`, and `make lint`.
- [x] 1.3 Extend `CoordinatedStagingIndex` in `src/curate_d/staging/occupancy.py` with
      `reserve(key, attempt_id, reserved_bytes, ttl)`, `consume_reservation(attempt_id)
      -> bool`, `forget_reservation(attempt_id)` and `total_reserved_bytes() -> int`,
      docstrings stating database-clock expiry and that `consume_reservation` returns False
      for an expired or absent reservation; add the same four methods, backed by a dict
      keyed by attempt with an injectable clock, to `FakeCacheIndex`
      (`tests/unit/staging/test_occupancy.py`) and `MemoryIndex`
      (`tests/stubs/staging.py`); verify with `make typecheck` and
      `test_the_fakes_satisfy_the_contracts_they_stand_in_for` passing.

## 2. Cache index: reservation operations and reconciliation (after 1; parallel with 3, 4)

- [x] 2.1 Implement `reserve`, `consume_reservation`, `forget_reservation` and
      `total_reserved_bytes` on `PostgresStagingCacheIndex` (`cache_index.py`): `reserve`
      and `consume_reservation` run on the writer connection (re-entrant holding required;
      `expires_at = now() + :ttl`; consume is `DELETE ... WHERE attempt_id = :id AND
      expires_at > now()` and returns whether a row went); `forget_reservation` takes a
      fresh `writer_lock("occupancy.abandon")` when none is held and deletes regardless of
      expiry; `total_reserved_bytes` sums live rows on the current connection; all failures
      translate to `StagingObjectStoreError`; verify with unit tests in
      `test_cache_index.py` against the unreachable engine (each is a typed failure) and
      the scripted engine (consume returns False on zero rows).
- [x] 2.2 Make `reconcile()` expire dead reservations first (`DELETE ... WHERE expires_at
      <= now()` on the writer connection), then protect the union of registered object keys
      and live reserved object keys (`<staging_key>/<attempt_id hex>`); rewrite its docstring
      premise per design D6; verify with unit tests in `test_cache_index.py`
      (`delete_orphans` given a reserved key spares it; an expired reservation's object is
      collected) using the scripted engine.
- [x] 2.3 Rewrite the `cache_index.py` module docstring: entry locks last for the
      reclamation holding that took them, never a transfer; drop the "deferred ... until the
      measurement" sentence; keep the collision cost statement; verify with `make lint`
      and a re-read against design D8.
- [x] 2.4 Integration tests in `tests/integration/test_staging_coordination.py`: a
      reservation written by one engine is counted by `total_reserved_bytes` on another;
      `consume_reservation` returns True once and False after; a reservation written with
      `ttl=timedelta(0)` is False to consume in the next transaction and its object is
      collected by `reconcile`; a live reservation's completed object is spared by
      `reconcile` on the other engine; `forget_reservation` from outside any holding
      reports `occupancy.abandon` in `staging.writer_lock_held`; add reservation rows to
      the `_cleanup` fixture; verify with `uv run pytest
      tests/integration/test_staging_coordination.py --no-cov -m integration`.

## 3. Occupancy and publish: two holdings, reservation handle, eviction ordering (after 1; parallel with 2, 4)

- [x] 3.1 Add `reservation_ttl: timedelta` to `StagingOccupancy.__init__` (non-negative,
      `ValueError` otherwise; zero expires in the next transaction, the test hook D11
      uses) and wire it in `factories.build_writer_backend` as `2 ×
      staging_transfer_timeout_seconds`; add `reserved_bytes` to `OccupancyReport` and
      include it in `StagingOccupancy.measure()` via `total_reserved_bytes()`
      (`measure_occupancy` keeps its signature and reports 0 for an index without the
      method); verify with unit tests in `test_occupancy.py` (construction rejects a
      negative TTL; `measure` reports live reservations) and `test_factories.py` (the
      derived TTL).
- [x] 3.2 Rewrite `StagingOccupancy.reserve(key, attempt_id, declared_max_bytes)` per
      design D4: holding 1 under `writer_lock("occupancy.reserve")` expires, measures
      (listing + live reservations), reclaims under pressure, inserts the reservation and
      commits; evicted bytes are deleted after the commit (D5; `_drop` removed,
      `_reclaim` returns the unpublished registrations); the block yields a `Reservation`
      with `admission: AdmissionReport`, `register(staged)` and `registered`; a deletion
      failure after the commit forgets the reservation and raises
      `StagingObjectStoreError`; a refusal still raises after the holding with nothing
      reserved; verify with unit tests: lock depth inside the block is 0 (inverting
      `test_admission_holds_the_writer_lock_across_the_publication`), eviction deletes run
      at depth 0 (inverting `test_admission_still_deletes_under_the_lock_it_holds_for_the_upload`),
      a reservation row exists while the block is open, two subjects over one fake index
      cannot both admit against the same headroom, a refusal reserves nothing, a delete
      failure after the commit leaves no reservation.
- [x] 3.3 Implement `Reservation.register(staged)`: holding 2 under
      `writer_lock("occupancy.register")` consumes the reservation and registers on success;
      on a lost reservation it registers nothing, logs `staging.reservation_lost` (WARNING,
      `staging_key`, `attempt_id`, `reserved_bytes`), deletes the attempt's bytes best
      effort after the holding and raises `StagingReservationLostError`; verify with unit
      tests: registration and consumption happen at the same lock depth and the
      reservation is gone afterwards, an expired reservation (fake clock) registers
      nothing, deletes the bytes and raises the typed error, a failed byte delete after a
      lost reservation still raises the lost-reservation error.
- [x] 3.4 Implement abandonment: on block exit without `registered`, forget the reservation
      in `writer_lock("occupancy.abandon")`; after `CancelledError` run it under
      `asyncio.shield` with a bounded `asyncio.timeout`; a failure logs
      `staging.reservation_abandon_failed` (WARNING) and never replaces the block's own
      exception; verify with unit tests: an exception in the block leaves no reservation
      and propagates, a cancelled block leaves no reservation and the `CancelledError`
      propagates, a forget that raises is logged and the original exception propagates.
- [x] 3.5 Update `publish()` to pass `key` and `target.attempt_id` into `reserve()` and
      call `reservation.register(staged)` instead of `index.register`; keep the phases
      event with `registration_seconds` spanning from upload end to the registration insert
      and `commit_seconds` to holding 2's end; verify with `test_publish.py`: the existing
      phase tests pass, `test_publishing_takes_the_writer_lock_as_a_release_then_a_reservation`
      becomes release, reserve, register in `lock_callers`, a lost reservation reports
      outcome `failed` with error `StagingReservationLostError` and non-null upload and
      registration phases.
- [x] 3.6 Rewrite the `occupancy.py` module docstring and the `reserve()`, `sweep()`,
      `_reclaim()`, `_unpublish()`, `_delete_unpublished()` and `reconcile`-related
      docstrings for the two-holding design, keeping the 2026-09-15 paragraph as history
      with the 2026-10-18 supersession sentence (design D8); verify with `make lint` and a
      re-read against D8.
- [x] 3.7 Update `docs/staging.md` "DBOS step boundaries" Stage bullet to the two holdings
      (this file is otherwise stream 4's; this one bullet is stream 3's because it states
      `publish()`'s contract); verify by re-reading it against the final `publish()`.
- [x] 3.8 Update `test_the_fakes_satisfy_the_contracts_they_stand_in_for`, `MemoryStaging`
      and any `tests/unit/staging/test_factories.py` construction sites for the new
      constructor argument; verify with `make test` (whole unit suite green, coverage gate
      kept).
- [x] 3.9 (needs 2) Integration tests in `tests/integration/test_staging_occupancy.py`:
      replace `test_a_reservation_blocks_another_workers_admission` with two tests, the
      other worker's admission completing within `_BLOCKED_FOR` while the first block is
      open when it fits, and being refused when the open reservation leaves no headroom
      though the listing shows none of its bytes; an upload cancelled mid-block leaves no
      reservation and no registration; a reservation with `reservation_ttl=timedelta(0)`
      makes `register` raise `StagingReservationLostError`, leaves no row, removes the
      object and logs `staging.reservation_lost`; a crashed pod's reservation (written via
      `other_index.reserve` in its own holding) counts against admission until expiry and
      its completed object is collected by the next sweep after; eviction under pressure
      leaves the registration gone when a reader looks after the block opens; update
      `test_a_reclaiming_publication_splits_its_hold_into_phases` to assert the
      `occupancy.reserve` holding excludes the upload and an `occupancy.register` holding
      exists; verify with `uv run pytest tests/integration/test_staging_occupancy.py
      --no-cov -m integration`.
- [x] 3.10 (needs 2) Integration tests in `tests/integration/test_staging_coordination.py`
      for the reader side through `StagingOccupancy`: while a `reserve()` block that
      reclaimed an orphan under the reader's key is open, the reader gets its winner within
      `_BLOCKED_FOR`; same for a colliding key; a reader of a key the admission evicted gets
      a miss within `_BLOCKED_FOR`; the existing blocked-reader tests keep passing against
      a raw `writer_lock` holding; verify with `uv run pytest
      tests/integration/test_staging_coordination.py --no-cov -m integration`.

## 4. Documentation (after 1; parallel with 2, 3)

- [x] 4.1 Rewrite `docs/staging.md` "Authority and data flow": the writer holds the lock for
      admission and for registration, uploads run unlocked under a reservation row, the
      reservation lifecycle (consumed, abandoned, expired at 2 × the transfer timeout by the
      database clock); add the TTL derivation under the transfer timeout in the
      configuration table; verify by `grep -n "across admission, upload" docs/staging.md`
      returning nothing.
- [x] 4.2 Rewrite `docs/staging.md` "Lock contention events": the `occupancy.reserve`,
      `occupancy.register` and `occupancy.abandon` callers, the new phase sums (design D7),
      the `staging.reservation_lost` and `staging.reservation_abandon_failed` events in the
      table with their fields, and a `jq` query for lost reservations; keep every existing
      query unchanged; verify each query parses with `jq -n '<filter>'` or by running it on
      a captured log sample.
- [x] 4.3 Replace "Revisit threshold for #1" with "Lock contention alarms" per design D8:
      the four criteria verbatim, a history paragraph (2026-09-15 deferral, #63's
      measurement, superseded 2026-10-18 by this change and why), and the expected
      post-change reading; verify by re-reading against D8 and that #4's reading is
      described as a regression check, not a prerequisite.
- [x] 4.4 Add to `docs/staging.md` "Recovery" a "Lost or expired reservation" entry (what
      `staging.reservation_lost` means, that the source is retried by the next rescan,
      what a stuck reservation looks like and that it expires) and a rollout note on the
      mixed-build window from the design's risks; verify by re-reading against design
      D9 and the risks section.

## 5. Integration (after 2, 3, 4)

- [x] 5.1 Run `make check` (lint, typecheck, unit suite with coverage gate) green.
- [x] 5.2 Run `uv run pytest tests/integration/test_staging_coordination.py
      tests/integration/test_staging_occupancy.py tests/integration/test_migrations.py
      --no-cov -m integration` green against `make test-stack`.
- [x] 5.3 Run `openspec validate resolve-staging-lock-contention --strict` green.
- [x] 5.4 Confirm no DBOS departure is needed: `git diff --stat dev -- src/curate_d/workflows
      src/curate_d/runtime` is empty and `make lint`'s workflow audit passes; no CLAUDE.md
      edit.

## Workflow follow-up

- Open the PR against `dev`; after merge, sync the delta spec (`openspec-sync-specs`) and
  archive the change (`openspec-archive-change`) in a follow-up PR.
- Comment on #1 and close it (`state_reason: completed`) naming the merged PR; comment on
  #4 that the first dev reading is now a regression check and keep it open.
