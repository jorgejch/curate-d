# Design

## Context

See proposal.md for motivation. Current state on `dev` (2bdb326):

- `PostgresStagingCacheIndex.writer_lock` logs `staging.writer_lock_held` with `caller`,
  `wait_seconds`, `held_seconds`; `hold_for_download` logs `staging.reader_lock_wait`;
  `object_store.py` logs `staging.transfer_deadline_hit`. The `occupancy.reserve` hold
  covers admission, upload and registration undivided, and `docs/staging.md` says so.
- Branch `fix/issue-4-contention-phases` (PR #18, 4 commits) and
  `fix/issue-1-reader-lock-docs` (PR #15, 3 commits) each merge cleanly onto `dev` but
  both trail it by 182 commits. `git merge-tree` of the two conflicts in `docs/staging.md`
  (both insert prose after the same paragraph, and #18 rewrites the paragraph #15 appends
  to) and in the `occupancy.py` module docstring (both edit the sentence naming the
  measurement events). Nothing else overlaps.
- PR #18 carries its own "Revisit threshold for #1" subsection; issue #1's body carries a
  different proposal. Both are "for the owner to confirm".
- Branch `fix/issue-8-cpu-worker-limits` (PR #25) touches `occupancy.py` (imports
  `StagingCapacityNeverFitsError`, changes `_admit`'s refusal class) and the
  `docs/staging.md` configuration table. `git merge-tree` with #18 is clean. Its new
  refusal class is a subclass concern for `_outcome_of` (below).
- Unit tests run under `make test` with a coverage floor. The integration gate's `suite`
  filter includes `src/**`, so the pull request runs the whole integration suite in CI.
- Both PRs pass unit-tests and Gitar; CodeRabbit is disabled on their base; no unresolved
  review threads on either.

## Goals / Non-Goals

**Goals:**

- One pull request against `dev` that contains both branches' commits, rebased, with the
  two conflicts resolved and one threshold section.
- The spec delta's scenarios each map to an existing or salvaged test.
- Zero change to which locks are taken, in what order, or for how long.

**Non-Goals:**

- Reclaim-before-admit (#1's narrowing), a reader `lock_timeout`, metrics or tracing
  exporters, and any deployed-environment reading. Pod identity on the events (the log
  shipper supplies it).
- Reworking #18's design: `_ReservationPhases`, `_outcome_of`, the `_claim` wrapper and the
  `MemoryStore` timing hooks are taken as they are.

## Decisions

**D1. Salvage by cherry-pick onto a fresh branch, not by rebasing the PR branches.**
A new branch from `dev` takes #18's four commits then #15's three, resolving the two
conflicts in the #15 commits. #18 and #15 are then closed as superseded, each with a
comment naming the new PR. Alternatives: rebase #18 in place and retarget #15 onto it (two
PRs, two CI runs, the second blocked on the first); or squash both into one commit (loses
the review-sized history). Rejected: the sequencing cost and the history loss outweigh the
one-time cherry-pick.

**D2. Stream order: #18 first, #15 second.** #18 rewrites the paragraph in
`docs/staging.md` that #15 appends to ("The `occupancy.reserve` hold covers ... not yet
split into those three parts" is deleted by #18 as no longer true), so #15's note has to be
re-placed into #18's rewritten section. The reverse order would have #18 delete text #15
just extended.

**D3. Conflict resolution, `occupancy.py` module docstring.** Keep #18's sentence naming
the three events and `admission_seconds` as the measurement, then #15's two sentences on
narrowing the entry locks alone. Both are true and neither depends on the other's wording.

**D4. Conflict resolution, `docs/staging.md`.** #15's paragraph ("A `staging.reader_lock_wait`
names the key the reader asked for, not the key the writer claimed ...") goes directly after
#18's `entry_claims` paragraph ("`entry_claims` counts entry locks, not keys ...") and
before the `staging.reservation_phases` paragraph: it explains the reader-side view of the
same claims. #18's `blocked_1s` reader query stays.

**D5. One threshold section.** The docs get exactly one "Revisit threshold for #1"
subsection, #18's, amended to carry #1's absolute cap. The chosen set: the lock counts as
binding, and #1's narrowing is due, when over at least seven days of real ingest in one
environment any of these holds:

1. `occupancy.reserve` `wait_seconds` p95 ≥ 30 s (both proposals agree; 10% of the default
   300 s transfer timeout).
2. Writer-lock duty cycle ≥ 50% in the busiest hour (#18 only; it is the only signal that
   says adding I/O pods cannot add throughput).
3. `staging.reader_lock_wait` with `registered: true` and `wait_seconds` ≥ 1 s is ≥ 1% of
   all reader events (#18's form; it subsumes #1's "p95 above 1 s", which implies ≥ 5%).
4. Any `staging.reader_lock_wait` with `registered: true` has `wait_seconds` > 60 s (#1's
   absolute cap; one stall that long is a defect signal whatever its rate, and `wait_max`
   in the reader query already exposes it).

Dropped: #1's "p95 above 1 s" (subsumed by 3). The section keeps #18's "proposed defaults,
open to change in review" framing: these remain owner decisions on #1, and the design's job
is to leave one set on record rather than two.

**D6. Outcome spelling.** The event uses `cancelled`, as #18's code, tests and docs do. The
proposal text says `canceled`; the code wins because renaming it buys nothing.

**D7. `_outcome_of` and PR #25.** #25 introduces `StagingCapacityNeverFitsError` for a
declaration larger than the whole bound. If it subclasses `StagingCapacityExhaustedError`,
`_outcome_of` already classifies it `refused`; if not, the branch that lands second adds
it to the `refused` test. Recorded here so the rebase of whichever lands second checks it;
`git merge-tree` shows no textual conflict either way.

**D8. Spec adds three requirements, modifies none.** The existing `content-staging` spec
has no requirement about these events, so there is nothing to MODIFY; the spec says the
events exist, what each reports, and what a reader blocked by a claim or a collision
observes. Field semantics that are implementation detail (monotonic clock, where the
`_claim` wrapper sits) stay in the docs.

**D9. Docstring premise.** #15 drops "narrowing it means narrowing the writer lock itself"
from `cache_index.py` and records that reclamation can commit in a holding of its own
before the admitting one. That is a statement about a possible design, not a change to
the recorded 2026-09-15 decision: the decision text in `occupancy.py` ("revisit when
measured ...") stays, and reclaim-before-admit stays deferred to #4's reading.

## Risks / Trade-offs

- [The 182-commit gap hides a semantic conflict `merge-tree` cannot see, e.g. a renamed
  fixture or a moved import] → Run `make test` and the two staging integration files after
  each stream, not only at the end; the integration gate reruns everything in CI.
- [`test_a_slow_upload_shows_in_its_phase` and the cancellation test depend on wall-clock
  sleeps via `MemoryStore.upload_delay`] → They are #18's tests and pass on its CI; keep
  the delays as #18 set them and treat a flake as a finding to report, not to loosen.
- [One more info line per publication] → I/O-side only, bounded by ingest rate; the docs'
  queries filter by event, so existing queries are unaffected.
- [Two threshold proposals had different authors' intent; merging them may misstate one]
  → D5 lists what was kept, merged and dropped, and the docs keep the "open to change"
  framing; the final call stays on #1.
- [PR #25 lands first and changes `_admit`'s refusal class] → D7; the refusal unit test
  in `test_publish.py` will fail loudly if `refused` stops matching, which is the right
  signal.

## Migration Plan

No schema, config or deployment change. Rollout is the ordinary `dev` merge; rollback is a
revert. After merge: comment on #4 and #1 that the code items landed, keep both open.

## Open Questions

None that affect the spec, approach or tasks. The owner decisions (confirm the threshold
set in D5; ship reclaim-before-admit or wait for the reading) are tracked on #1 and #4 and
are explicitly outside this change.
