# Design

## Context

Most of the behaviour is already in `.github/workflows/checks.yml`, `integration.yml`, the
`Makefile` and `sonar-project.properties` (proposal.md, What Changes). Unit tests already
pin the `make test-stack` path (`test_ci_starts_the_suites_services_with_the_developers_target`,
`test_ci_keeps_its_image_and_lockfile_guarantees_through_the_target`,
`test_test_stack_resets_the_stack_before_it_starts_the_rest`). Three behaviours have no pin:
the scan's skip condition (`test_the_scan_skips_only_fork_pull_requests` checks the fork
clause but not that nothing else is skipped), `persist-credentials: false`, and the
SonarQube exclusion lists.

The repository has one `SONAR_TOKEN`, an Actions secret. GitHub never exposes Actions
secrets to a run Dependabot triggers; such a run reads Dependabot secrets instead, and the
repository has none.

## Goals / Non-Goals

**Goals:**
- Spec matches the pipeline; every new scenario is pinned by a unit test or by an existing
  one.
- Dependabot pull requests carry a SonarQube analysis, so they meet the required
  code-scanning rule without a bypass.

**Non-Goals:**
- Changing any Makefile target or Sonar property, or any workflow beyond the scan's `if`.
- `docker-publish.yml`'s checkout, which still persists the token: it runs only on `dev`
  pushes and semver tags, never on pull request code, so the requirement leaves it out
  rather than this change altering a workflow.
- `claude.yml`, which is not part of the release pipeline.

## Decisions

### D1. Spec catch-up, one workflow revert, pin tests

The drift came from reviewed, merged commits, so the delta describes them as they are,
except the Dependabot skip (D5). Writing a retroactive change with code tasks for the rest
was rejected: there is nothing left to build.

### D2. Scan skip as its own requirement

The fork skip lived only in `ci-pr-gates`'s design. One ADDED requirement states it, and
that a Dependabot run is scanned with its own token; the feature-branch requirement stays
as it is.

### D3. Pin the Sonar scope by listing top-level directories

A test reads `sonar-project.properties`, expands the two exclusion lists, and asserts that
every top-level directory in the repository is either excluded or is the measured one
(`src/` for coverage; `src/` and `infrastructure/` for duplication). That catches the
"add a new top-level directory here" note the properties file relies on a reader to honour.
Asserting the literal property strings was rejected: it fails on a reorder and misses a new
directory.

### D4. Extend the fork-skip test

`test_the_scan_skips_only_fork_pull_requests` keeps its name, which is accurate again once
the Dependabot clause goes, and also asserts that the condition names no actor.

### D5. Dependabot gets its own `SONAR_TOKEN`

The repository gets a Dependabot secret `SONAR_TOKEN`, and the scan's `if` drops
`github.actor != 'dependabot[bot]'`. The step's `env` needs no change: a Dependabot run
resolves `secrets.SONAR_TOKEN` from the Dependabot secrets. Use a separate SonarCloud
analysis token, not a copy of the Actions one, so it can be revoked on its own.

Rejected: an admin bypass on every Dependabot pull request (manual, and it waives the
scan); dropping SonarCloud from the required ruleset rule (a decision for the SonarCloud
trial's review, and it would ungate every pull request).

## Risks / Trade-offs

- [The workflow edit merges before the Dependabot secret exists] → Every Dependabot run
  fails at the scan. Task 0.1 sets the secret first, and the apply does not start the
  workflow edit until it is listed.
- [A Dependabot pull request runs the updated dependency's code in the checks job] → The
  token sits only in the scan step's environment, after the tests, and is an analysis token
  for this one SonarCloud project.
- [The directory-listing test reads the working tree, so an untracked local directory such
  as `.venv/` would fail it] → List tracked top-level entries with `git ls-files`, so
  ignored directories never count. It reads this repository only, so the
  repository-boundary rule does not apply.

## Work streams

Task 0.1 (the owner's) comes first. Then three disjoint streams, all startable at once: A
edits `checks.yml` and `test_checks_workflow.py`, B edits `test_integration_workflow.py`, C
creates `test_sonar_properties.py`. The final checks wait on all three.
