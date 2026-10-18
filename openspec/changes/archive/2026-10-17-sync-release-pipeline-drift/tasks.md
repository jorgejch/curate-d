# Tasks

Task 0.1 comes first. Streams A, B and C then touch disjoint files and can start at once;
group 4 waits on all three.

## 0. Prerequisite (repository owner)

- [x] 0.1 Create a SonarCloud analysis token for Dependabot and store it as the
  repository's Dependabot secret:
  `gh secret set SONAR_TOKEN --app dependabot --repo jorgejch/curate-d`; verify that
  `gh secret list --app dependabot --repo jorgejch/curate-d` lists `SONAR_TOKEN`

## 1. Stream A: checks workflow (`.github/workflows/checks.yml`, `tests/unit/infrastructure/test_checks_workflow.py`)

- [x] 1.1 Drop `github.actor != 'dependabot[bot]' &&` from the SonarQube scan's `if`, and
  extend `test_the_scan_skips_only_fork_pull_requests` to assert the condition names no
  actor; verify with
  `uv run pytest tests/unit/infrastructure/test_checks_workflow.py --no-cov`
- [x] 1.2 Add a test that every `actions/checkout` step in the `checks` and
  `coverage-report` jobs sets `persist-credentials: false`, failing with the job's name;
  verify with the same command

## 2. Stream B: integration workflow pin (`tests/unit/infrastructure/test_integration_workflow.py`)

- [x] 2.1 Add a test that the integration job's `actions/checkout` step sets
  `persist-credentials: false`; verify with
  `uv run pytest tests/unit/infrastructure/test_integration_workflow.py --no-cov`

## 3. Stream C: SonarQube scope pin (`tests/unit/infrastructure/test_sonar_properties.py`)

- [x] 3.1 Create the module: parse `sonar-project.properties` (continuation lines
  included), list the repository's tracked top-level entries with `git ls-files`, and
  assert that coverage excludes every entry but `src`, and duplication every entry but
  `src` and `infrastructure`, naming any unlisted directory; verify with
  `uv run pytest tests/unit/infrastructure/test_sonar_properties.py --no-cov`

## 4. Integration check

- [x] 4.1 Run `make lint typecheck test` and verify it passes with the coverage floor met
- [x] 4.2 Run `openspec validate sync-release-pipeline-drift --strict` and verify it passes

## Workflow follow-up

- After #37 merges, verify the next Dependabot pull request's checks job runs the
  SonarQube scan and SonarCloud shows its analysis. Moved out of the tracked tasks on
  2026-10-17 so the change could archive before a Dependabot run exists; tracked in #40.
- Sync the delta into `openspec/specs/release-pipeline/spec.md` (`openspec-sync-specs`).
- Archive the change (`openspec-archive-change`).
