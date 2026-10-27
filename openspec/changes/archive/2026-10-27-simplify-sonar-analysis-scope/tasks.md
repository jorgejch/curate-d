# Tasks

Streams A and B own disjoint files and may start together. A's properties edit waits
for B's red-test result; A's documentation edit can proceed immediately. The parent
owns shared OpenSpec artifacts and combined verification after both streams join.

## 1. Stream B — scope contract tests

- [x] 1.1 Update `tests/unit/infrastructure/test_sonar_properties.py` to assert explicit, disjoint source/test roots, minimal exclusions, unchanged identity/Python/report settings and no stale Docker suppression; remove Git-directory enumeration and pytest inference. Correct the stale Sonar comment in `tests/unit/infrastructure/test_checks_workflow.py`. Verify the new contract fails against the old configuration with `uv run --locked --no-build pytest tests/unit/infrastructure/test_sonar_properties.py --no-cov`.

## 2. Stream A — configuration and maintenance

- [x] 2.1 Update `sonar-project.properties` to the selected roots and sole infrastructure coverage exclusion; remove obsolete exclusions and Docker suppression. Document scope, trade-offs and expansion policy in `CONTRIBUTING.md`. Verify with `uv run --locked --no-build pytest tests/unit/infrastructure/test_sonar_properties.py tests/unit/infrastructure/test_checks_workflow.py --no-cov` and review documentation against the properties.

## 3. Combined verification

- [x] 3.1 Run `uv run --locked --no-build pytest -n auto tests/unit`, targeted ruff checks and `openspec validate simplify-sonar-analysis-scope --strict`; record results.

## Verification results

- Old configuration: six scope failures demonstrated the problem; an additional new-test
  organization expectation was corrected to the existing `jorge-haddad-oss` value.
- Updated configuration: 15 targeted tests passed; `make test` passed all 2013 unit
  tests with 84.88% coverage. `make typecheck` passed for 388 files.
- Targeted ruff lint and format checks passed. Full lint passed the lint stage, but its
  format stage encountered an unrelated untracked `skills/` document. The PR's clean
  worktree will run the complete pre-push checks without altering that document.
- Strict change and release-pipeline spec validation passed. No application or integration
  behavior changed; Cloud-side indexing and metric results await the next CI scan.

## Workflow follow-up

- Sync the delta into `openspec/specs/release-pipeline/spec.md` and validate the spec.
- Archive the completed change and check that the delta is synced and the archive exists.
- Confirm server-side indexing and metrics in the next authenticated CI scan.