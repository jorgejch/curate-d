## Why

The maintainer wants migrations analysed as source and a more accurate name for the
repository's provisioning and environment-maintenance tools. These are operational
assets, not the separately maintained deployment infrastructure.

## What Changes

- **BREAKING**: Rename root `infrastructure/` to `ops/`, preferring IDE refactoring.
  Update live path references in Compose, CI, scripts, tests and documentation.
- Analyse `src/`, `ops/` and `migrations/` as Sonar sources; explicitly classify `tests/`.
  Cover only `src/`, and measure duplication across all three source trees.
- Keep historical OpenSpec archives and the broad `tests/unit/infrastructure/` CI-test
  grouping unchanged. Add regression coverage for operational entry points.

## Capabilities

### Modified Capabilities

- `release-pipeline`: update Sonar roots/metrics and integration selection paths.

## Impact

Root operational files, `.dockerignore`, `.github/workflows/integration.yml`,
`compose.yaml`, `sonar-project.properties`, affected unit/integration tests, `README.md`,
`CONTRIBUTING.md`, `docs/{reset,recovery,staging}.md`, architecture diagram path labels,
and current release-pipeline specs. No DBOS behavior, dependency or schema changes.