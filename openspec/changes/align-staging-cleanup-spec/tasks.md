# Tasks

One work stream: a single spec edit, no code.

## 1. Spec correction

- [ ] 1.1 Check each scenario in the delta against the code (`staging/publish.py` release before reserve, `ingest/pipeline.py` release on source change, `staging/occupancy.py` `sweep`/`_expired` and `_admit`/`_reclaim`, `worker/main.py` cleanup tick) and verify `uv run openspec validate align-staging-cleanup-spec --strict` passes.
- [ ] 1.2 Check `docs/staging.md` for text contradicting the corrected requirement and fix only such text; verify with `grep -n "No step releases" docs/staging.md` still matching the "DBOS step boundaries" release bullet.

## 2. Checks

- [ ] 2.1 `make check` green.

## Workflow follow-up

- Sync the delta into `openspec/specs/content-staging/spec.md`, archive the change, and run `uv run openspec validate --all --strict`.
