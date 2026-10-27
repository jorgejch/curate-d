# Proposal

## Why

Sonar's metric exclusions enumerate the repository layout and its test classification
incorrectly relies on pytest. Explicit source and test roots make the configuration
stable as unrelated directories are added.

## What Changes

- Limit ordinary Sonar analysis to `src/`, `infrastructure/` and test code in `tests/`,
  as selected by the maintainer; migrations, Dockerfiles, workflows and other paths
  will no longer receive ordinary Sonar findings.
- Declare source and test roots explicitly, exclude only `infrastructure/**` from
  coverage, and use normal production-code duplication measurement without exclusions.
- Remove out-of-scope analysis exclusions and the Docker-specific rule suppression.
- Replace layout-enumeration tests with scope-contract tests and document maintenance.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: positively select Sonar analysis roots and keep the existing
  coverage and duplication boundaries without exhaustive exclusions.

## Impact

`sonar-project.properties`, `tests/unit/infrastructure/test_sonar_properties.py`,
`tests/unit/infrastructure/test_checks_workflow.py`, `CONTRIBUTING.md` and the
release-pipeline specification. No dependencies, scanner action or application changes.

## Non-goals

Retaining repository-wide Sonar analysis, generating scanner properties, changing the
unit coverage floor, or importing integration coverage.