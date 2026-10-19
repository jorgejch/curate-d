# Tasks

Two streams, sequenced: stream 2 rebases onto stream 1 because both edit
`docs/staging.md`, `src/curate_d/staging/occupancy.py`, `src/curate_d/staging/cache_index.py`,
`tests/integration/test_staging_coordination.py` and `tests/unit/staging/test_cache_index.py`.
The change cannot be split further (design.md, D2). Group 3 waits on both. Integration
tests need `make test-stack` once before the first run. Work on one branch cut from
`origin/dev`; the PR targets `dev`.

## 1. Stream 1: PR #18 (`fix/issue-4-contention-phases`) onto dev

- [x] 1.1 Cut a branch from `origin/dev` and cherry-pick the four commits of
  `origin/fix/issue-4-contention-phases` in order (`6f52640`, `108b8cd`, `efcb922`,
  `1a713d6`), resolving any conflict in favour of the branch's intent; verify with
  `git diff origin/fix/issue-4-contention-phases -- src tests docs` showing only hunks
  attributable to the 182 intervening `dev` commits
- [x] 1.2 Confirm the `entry_claims` field, `_claim` wrapper, `AdmissionReport.admission_seconds`
  and `staging.reservation_phases` landed as in the branch; verify with
  `make lint typecheck` and `uv run pytest -n auto tests/unit/staging --no-cov`
- [x] 1.3 Run the unit suite with its coverage gate; verify with `make test` passing
- [x] 1.4 Run the two staging integration files against the test stack; verify with
  `make test-stack` then
  `uv run pytest tests/integration/test_staging_coordination.py tests/integration/test_staging_occupancy.py --no-cov -m integration`
  passing, including `test_a_holding_reports_every_entry_it_evicted`,
  `test_a_claim_a_reader_refused_is_not_counted` and
  `test_a_reclaiming_publication_splits_its_hold_into_phases`
- [x] 1.5 Amend the "Revisit threshold for #1" subsection of `docs/staging.md` to the set in
  design.md D5: keep #18's three bullets, add the fourth (any `registered: true` reader wait
  > 60 s, read from `wait_max` in the reader query), and keep the "proposed defaults, open
  to change in review" sentence; verify with
  `sed -n '/^### Revisit threshold/,/^## /p' docs/staging.md | grep -c "^- "` counting four
  bullets in that subsection and `grep -n "Revisit threshold" docs/staging.md` returning
  one heading

## 2. Stream 2: PR #15 (`fix/issue-1-reader-lock-docs`) onto stream 1

- [x] 2.1 Cherry-pick the three commits of `origin/fix/issue-1-reader-lock-docs` in order
  (`8496f60`, `ae66b68`, `0487fb5`). Resolve `src/curate_d/staging/occupancy.py` per
  design.md D3 (#18's event sentence, then #15's narrowing sentences) and `docs/staging.md`
  per D4 (#15's "names the key the reader asked for" paragraph after #18's `entry_claims`
  paragraph, before the `staging.reservation_phases` paragraph; the deleted "not yet split"
  sentence stays deleted); verify with `git diff origin/fix/issue-1-reader-lock-docs -- src/curate_d/staging/cache_index.py`
  showing no change to #15's `entry_lock_id` and `hold_for_download` docstrings, and
  `grep -n "not yet split" docs/staging.md` returning nothing
- [x] 2.2 Check the `cache_index.py` module docstring no longer says narrowing the entry
  locks means narrowing the writer lock and `occupancy.py`'s decision text ("Revisit when
  measured upload concurrency") is unchanged; verify with
  `grep -n "narrowing the writer lock" src/curate_d/staging/cache_index.py` returning
  nothing and `grep -n "Revisit when measured" src/curate_d/staging/occupancy.py` returning
  one line
- [x] 2.3 Run lint, type check and the unit suite; verify with `make check` passing,
  including `test_two_distinct_keys_can_share_an_entry_lock`
- [x] 2.4 Run the two staging integration files again; verify with the 1.4 command passing,
  including `test_a_reader_waits_behind_a_writer_that_claimed_only_a_colliding_key` and
  `test_a_reader_of_a_colliding_key_makes_reconciliation_skip_an_orphan`

## 3. Integration check

- [x] 3.1 Read the finished "Lock contention events" section of `docs/staging.md` top to
  bottom and confirm every field in the events table has a paragraph, every distribution
  has a `jq` query, and each spec scenario names a test that exists; verify with
  `grep -n "entry_claims\|reservation_phases\|blocked_1s\|duty_cycle\|colliding" docs/staging.md`
  listing each at least once
- [x] 3.2 Run `openspec validate staging-lock-contention-observability --strict` and the
  full unit suite one final time; verify both pass
- [x] 3.3 Open the pull request against `dev` titled
  `feat(staging): entry claims, reservation phases and collision docs for lock contention`,
  body naming #4, #1, #18 and #15 and the threshold set; verify `Integration tests` runs
  the suite (the diff touches `src/**`) and every required check is green
- [x] 3.4 Close PRs #18 and #15 as superseded, each with a comment linking the new PR; never
  delete the branches until the new PR merges; verify with `gh pr view 18 --json state` and
  `gh pr view 15 --json state` both `CLOSED`

## Workflow follow-up

- On merge: comment on #4 and #1 that `entry_claims`, `staging.reservation_phases` and the
  collision docstrings landed, link the PR and the "Revisit threshold for #1" subsection,
  and leave both issues open for the owner decisions and the first dev reading.
- Sync the delta into `openspec/specs/content-staging/spec.md` and archive the change.
- Whichever of this change and PR #25 lands second re-checks design.md D7
  (`StagingCapacityNeverFitsError` vs the `refused` outcome).
