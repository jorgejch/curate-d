# Proposal

## Why

`docs/` holds operator runbooks and generated pages that quote commands, connection strings
and environment variables, and nothing scans it for a leaked secret: commit 6ebcc1a limited
SonarQube's text inclusions to the analysed source and test paths. Secret detection is the one
analysis worth having there; the code issues that led `docs/` out of scope
(`2026-10-25-exclude-docs-from-sonar`) come from language analyzers, which this does not add.

## What Changes

- `sonar.text.inclusions` adds `docs/*`, so SonarQube's secret detection covers the runbooks
  directly in `docs/`. A single `*` does not cross a directory, so `docs/diagrams/` (generated
  pages, inline JavaScript and CSS) stays unscanned without an exclusion property. `docs/`
  stays out of `sonar.sources`: no language rules, coverage or duplication.
- The scope test pins text inclusions to the source/test paths plus `docs/*`, exactly; any
  other added path, `docs/**` included, still fails it.
- `CONTRIBUTING.md` lists the files directly in `docs/` as secret detection only.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: "SonarQube analyses explicitly selected source and test trees" makes
  the files directly in `docs/` eligible for secret detection only.

## Impact

- `sonar-project.properties`, `tests/unit/infrastructure/test_sonar_properties.py`,
  `CONTRIBUTING.md`.
- The next `dev` analysis may report secret findings in `docs/`; none affect the quality gate
  until they appear.
