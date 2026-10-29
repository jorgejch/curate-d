# Proposal

## Why

`docs/` holds operator runbooks and generated pages that quote commands, connection strings
and environment variables, and nothing scans it for a leaked secret: commit 6ebcc1a limited
SonarQube's text inclusions to the analysed source and test paths. Secret detection is the one
analysis worth having there; the code issues that led `docs/` out of scope
(`2026-10-25-exclude-docs-from-sonar`) come from language analyzers, which this does not add.

## What Changes

- `sonar.text.inclusions` adds `docs/**`, so SonarQube's secret detection covers `docs/`.
  `docs/` stays out of `sonar.sources`: no language rules, coverage or duplication.
- `sonar.exclusions=docs/diagrams/**` keeps the generated pages (inline JavaScript and CSS)
  out of every analysis, secrets included. Sonar has no text- or secrets-specific path
  exclusion, and its docs do not say whether `sonar.exclusions` removes a file a text
  inclusion adds, so the next `dev` scan's indexed files confirm it. This reverses the rule
  that scope is free of source/test filters, for this one pattern; a narrower `docs/*`
  inclusion would have avoided the filter, at the cost of skipping any future `docs/`
  subdirectory.
- The scope test pins text inclusions to the source/test paths plus `docs/**`, and
  `sonar.exclusions` to `docs/diagrams/**`, exactly; any other filter still fails it.
- `CONTRIBUTING.md` lists `docs/` as secret detection only and `docs/diagrams/` as excluded.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: "SonarQube analyses explicitly selected source and test trees" makes
  `docs/` eligible for secret detection only, excluding `docs/diagrams/`.

## Impact

- `sonar-project.properties`, `tests/unit/infrastructure/test_sonar_properties.py`,
  `CONTRIBUTING.md`.
- The next `dev` analysis may report secret findings in `docs/`; none affect the quality gate
  until they appear. If it still indexes `docs/diagrams/`, the exclusion does not reach text
  inclusions and the inclusion narrows to `docs/*`.
