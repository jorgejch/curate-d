# Proposal

## Why

Issue #1 (narrow the entry locks reclamation holds across an upload) was deferred on
2026-09-15 until measurement shows the global writer lock binding, and #4 added the
`staging.writer_lock_held` and `staging.reader_lock_wait` events as that measurement. Two
open pull requests finish the measurement and have sat unmerged since: #18 (branch
`fix/issue-4-contention-phases`) splits a reservation's hold into phases and counts the
entry locks each holding takes, and #15 (branch `fix/issue-1-reader-lock-docs`) states
the reader-side cost of an entry-lock collision and pins it in tests. Both are clean
against `dev` individually, conflict with each other in `docs/staging.md` and the
`occupancy.py` module docstring, and each records a different "binding" threshold. Without
them the first dev reading #1 waits on cannot be taken, and the content-staging spec has no
requirement that the contention events exist at all.

## What Changes

- Land #18's code: `staging.writer_lock_held` gains `entry_claims` (distinct entry locks
  a fresh holding took, all held until it ended); a new `staging.reservation_phases` event
  is logged once per publication reservation with admission, upload, registration and
  commit seconds, the outcome (`registered`, `refused`, `cancelled`, `failed`), the error
  class and what admission reclaimed; the admission report carries `admission_seconds`.
  No locking behaviour changes.
- Land #15's docstrings and tests: `entry_lock_id` says a collision is a stall on the
  blocking reader side, `hold_for_download` names the two ways a writer comes to hold a
  reader's lock, and the `cache_index.py` and `occupancy.py` module docstrings drop the
  premise that narrowing the entry locks requires narrowing the writer lock. Two
  integration tests pin a colliding 4-byte `entry_lock_id` pair from both sides.
- Reconcile the two threshold proposals into one "Revisit threshold for #1" subsection of
  `docs/staging.md` (the chosen set is in design.md), with the new fields, one `jq` query
  per distribution (phases by outcome, entry-claim window, writer-lock duty cycle across
  I/O pods) and the note that a reader wait can be a hash collision.
- Add a spec requirement for the contention events: today the three existing events are
  undocumented by the spec, so the new fields and event are added alongside them.
- Open one pull request against `dev` carrying both branches' commits, and close #18 and
  #15 as superseded by it (closed with a comment, never deleted).

Out of scope: the reclaim-before-admit narrowing itself, a reader-side `lock_timeout`,
confirming the deployed dev/prod state, and the first reading. Those stay on #4 and #1 as
manual follow-ups; both issues stay open for the owner decisions recorded there.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `content-staging`: adds requirements that lock contention is observable through the
  `staging.writer_lock_held`, `staging.reservation_phases` and `staging.reader_lock_wait`
  events, including what each reports and what a reader behind a reclaiming or colliding
  writer observes. No existing requirement changes.

## Impact

- Code: `src/curate_d/staging/cache_index.py`, `occupancy.py`, `publish.py`;
  `tests/stubs/staging.py` (`MemoryStore` gains `upload_delay`, `upload_gate`,
  `uploading` for timing and cancellation tests).
- Tests: `tests/unit/staging/test_cache_index.py`, `test_occupancy.py`, `test_publish.py`;
  `tests/integration/test_staging_coordination.py`, `test_staging_occupancy.py`. The
  integration gate's `suite` filter includes `src/**`, so the pull request runs the whole
  integration suite in CI.
- Docs: `docs/staging.md`, "Lock contention events" section and the new subsection.
- Spec: `openspec/specs/content-staging/spec.md` on archive.
- Logging volume: one more info line per publication, on I/O workers only.
- Branches: PR #25 (`fix/issue-8-cpu-worker-limits`, issue #8) also edits
  `occupancy.py` (an import and `_admit`'s refusal class) and the `docs/staging.md`
  configuration table; `git merge-tree` reports no conflict with #18, so either may land
  first.
- Issues: #4's and #1's code items are marked done on merge; neither issue closes.
