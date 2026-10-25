# Tasks

One work stream: the guard and the test that runs it land together (design.md, Parallelism).

## 1. Fail the plan on every incomplete changed-file result

- [x] 1.1 Add `test_the_detection_guard_fails_a_possibly_truncated_list_whatever_it_selected` to `tests/unit/infrastructure/test_integration_workflow.py`, running the "Refuse an undecided detection" script under `bash --noprofile --norc -eo pipefail` with `SUITE` `true` and `false` and `CHANGED_FILES` at the limit, above it, empty and unset, and asserting each exits 1 naming the count and telling the author to split the pull request; add `test_the_detection_guard_passes_a_complete_list` for both `SUITE` values under the limit and `test_the_detection_guard_refuses_a_result_neither_true_nor_false` for an empty and an unknown `SUITE`; verify the first fails for `SUITE=true` with `uv run pytest tests/unit/infrastructure/test_integration_workflow.py --no-cov`
- [x] 1.2 Drop the `SUITE = false` condition from the guard's file-count check in `.github/workflows/integration.yml` and update the step's comment; verify `uv run pytest tests/unit/infrastructure/test_integration_workflow.py --no-cov` passes, then `make check` passes

## 2. Integration check

- [x] 2.1 Run `npx -y @fission-ai/openspec@1.15.0 validate --all --strict` and verify it passes

## Workflow follow-up

- Synced into `openspec/specs/release-pipeline/spec.md` and archived in the same pull request as
  the workflow change and its test (issue #66), with `openspec validate --all --strict` passing.
- After it merges, the first pull request with 3,000 or more changed files fails
  `Integration tests` at "Refuse an undecided detection" whichever result the detection gives.
