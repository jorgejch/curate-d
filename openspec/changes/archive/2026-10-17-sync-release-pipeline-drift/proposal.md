# Proposal

## Why

Several CI and test-stack changes merged into `dev` after `ci-pr-gates` was archived,
without a change of their own, so `openspec/specs/release-pipeline/spec.md` no longer
describes what the pipeline does. One of them contradicts the spec: since 6d713a9 a
Dependabot run uploads no SonarQube analysis, yet every feature-branch push MUST upload
one, and with SonarCloud a required code-scanning tool on `dev` and `main` a Dependabot
pull request cannot merge without an admin bypass. That skip is reverted rather than
written into the spec; the rest of the drift is brought into the spec as it is, and the
behaviour no unit test asserts yet is pinned.

## What Changes

- Dependabot gets its own `SONAR_TOKEN` Dependabot secret, and `checks.yml` drops the
  `github.actor != 'dependabot[bot]'` clause from 6d713a9. The scan again skips only fork
  pull requests, and a Dependabot run without the secret fails instead of skipping.
- SonarQube counts coverage on `src/` only and duplication on `src/` and `infrastructure/`;
  `tests/` is measured for neither (3aed164, 5b183c7, 1ce8a2a).
- The checks, coverage-report and integration jobs check the repository out without
  persisting the token (4327d0f).
- The integration job and a developer start the suite's services with the same
  `make test-stack` target, which destroys the local stack first, so a local suite never
  runs beside a stack-up's API, workers or persisted schedules (ae64707, e25199c, 1d24446,
  6b3e2b4).
- The suite no longer promotes the previous latest version back after it runs (faad7e2):
  `make test-stack`'s reset makes the hand-back unnecessary, since the next application
  launch registers its own version as latest.
- Unit tests pin the fork-only scan skip, the token-free checkouts and the SonarQube scope.
  The only workflow edit is the Dependabot clause; no Makefile or application code changes.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: new requirements for the scan's fork-only skip, the SonarQube
  measurement scope, token-free checkouts, and the shared integration stack target. The
  feature-branch requirement is unchanged: Dependabot pushes upload an analysis like any
  other branch.

## Impact

- `openspec/specs/release-pipeline/spec.md` (on sync).
- `.github/workflows/checks.yml`: the scan's `if` loses the Dependabot clause.
- The repository's Dependabot secret `SONAR_TOKEN`, set by the owner before the workflow
  edit merges.
- `tests/unit/infrastructure/test_checks_workflow.py`,
  `tests/unit/infrastructure/test_integration_workflow.py`, and a new
  `tests/unit/infrastructure/test_sonar_properties.py`: pin tests.
- No runtime or deployment change.
