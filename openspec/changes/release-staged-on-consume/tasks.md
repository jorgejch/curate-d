# Tasks

One work stream, for one agent: every group edits the release path end to end and depends
on the one before it, so nothing here splits usefully across agents.

## 1. Conditional release in staging

- [x] 1.1 Add `attempt_id` to `StagingOccupancy.release` (design decision 2): a named attempt that is not the registered one releases to a no-op. Verify with unit tests in `tests/unit/staging/test_occupancy.py`: matching attempt deletes; another attempt registered leaves it registered and readable; nothing registered no-ops; a download holding the entry unpublishes only; a deletion failure after the unregistration raises and a repeat no-ops; no `attempt_id` behaves as before.

## 2. The staged attempt travels with the reference

- [x] 2.1 Add `attempt_id: UUID | None = None` to `StagedRef` and set it in `stage_source` (design decision 1). Verify with unit tests in `tests/unit/ingest/`: a staged result carries the published attempt; a `StagedRef` pickled without the field restores with `None`.

## 3. The release workflow

- [x] 3.1 Add `workflows/staged_release.py` (`release_staged_entry`, `_release_step`, `enqueue_staged_release`, `reads_staged`) and list it in `workflows/registry.py` (design decisions 3, 5, 6). Verify with unit tests in `tests/unit/workflows/test_staged_release.py`: the step releases through the writer on the staging loop; a step failure after retries returns `FAILED` and logs `staging.release_failed`; a repeated release no-ops; no writer raises; an unknown attempt enqueues nothing; the ID is deterministic; `reads_staged` matches only a `StagedRef` input with the same attempt, and treats a missing status as not holding.

## 4. Consumers release

- [ ] 4.1 `fingerprint_source` enqueues the release on the paths in design decision 4. Verify in `tests/integration/test_ingest_workflow.py`: a fingerprinted source whose classification reads another source's copy leaves staging; an unreadable snapshot is released; the copy classification was handed stays until classification ends.
- [ ] 4.2 `classify_content` enqueues the release of its `staged` on every return. Verify in `tests/integration/test_classify_workflow.py` (or the ingest suite's pipeline): after a full cycle every fingerprinted source's entry has left the in-memory staging, and a reused classification releases the copy it was handed.
- [ ] 4.3 Update `tests/integration/test_end_to_end.py`, whose RustFS cycle asserts every fetched source is still in the cache index: assert instead that each was staged through RustFS and released. Verify with that test under the stack lock.
- [ ] 4.4 Add a RustFS integration test showing a consumed object leaves the store (`tests/integration/test_ingest_rustfs.py`): stage, inspect, release by attempt; the object is gone from the bucket listing, and a release naming a superseded attempt leaves the newer one. Verify with `--staging-backend=rustfs` under the stack lock.

## 5. Documentation

- [ ] 5.1 `docs/staging.md`: rewrite the "Release" bullet under "DBOS step boundaries", add `staging.released` and `staging.release_failed` to the log events table, and correct any other text saying entries leave only by age or pressure. `docs/rewind.md`: add the release child to the parents table and note that a rewound classification after its release classifies from names. Verify by `grep -n "No step releases" docs/staging.md` returning nothing.

## 6. Checks

- [ ] 6.1 `make check` green (lint incl. `workflow_audit`, mypy, unit suite with coverage gate).
- [ ] 6.2 Under the stack lock: `make test-stack`, then `uv run pytest tests/integration/test_staging_occupancy.py tests/integration/test_ingest_rustfs.py tests/integration/test_ingest_workflow.py tests/integration/test_classify_workflow.py tests/integration/test_end_to_end.py --staging-backend=rustfs`; release the lock.

## Workflow follow-up

- Sync the delta into `openspec/specs/content-staging/spec.md`, archive the change, and run `uv run openspec validate --all --strict`.
