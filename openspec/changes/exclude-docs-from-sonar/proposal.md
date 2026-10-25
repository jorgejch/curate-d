# Proposal

## Why

SonarQube Cloud analyses the whole repository, `docs/` and `openspec/` included. Neither counts
toward coverage or duplication, but their files are still analysed for issues, and the two
generated diagram pages under `docs/diagrams/` carry inline JavaScript and CSS that SonarQube
reports as 1,886 of the 1,963 open issues on `dev` (2026-10-25). That noise hides the 77 issues on
code we maintain, and it is why the web UI and SonarQube for IDE (which excludes both directories
locally) disagree on the issue count. `openspec/` holds planning artifacts, not code: it carries no
issue today, and excluding it keeps it out of analysis as it grows.

## What Changes

- `sonar-project.properties` sets `sonar.exclusions=docs/**,openspec/**`. SonarQube analyses no
  file under either directory, so it raises no issue, secret or hotspot there. The next `dev`
  analysis closes the existing `docs/` issues.
- The unit test that pins SonarQube's measured scope also pins the analysis exclusions to exactly
  `docs/**` and `openspec/**`. A drift either way (a pattern dropped, or another added) fails the
  test.
- The coverage and duplication exclusions keep their `docs/**` and `openspec/**` entries. They
  become redundant, but the existing rule that every top-level directory but the measured ones is
  listed still holds.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `release-pipeline`: adds a requirement that SonarQube leaves `docs/` and `openspec/` out of its
  analysis, and that the unit suite fails when the analysis exclusions drift from exactly those
  two patterns.

## Impact

- `sonar-project.properties`, `tests/unit/infrastructure/test_sonar_properties.py`.
- SonarQube Cloud: the open issue count on `dev` drops by the `docs/` issues once the change merges
  and `coverage-report` analyses `dev`. The quality gate is unaffected: no `docs/` issue is on new
  code, and `openspec/` has none.
- Secret detection: Sonar is the only secret scanner on the repository (GitHub secret scanning is
  disabled), so the Markdown runbooks under `docs/` and the change artifacts and specs under
  `openspec/` lose that check. Turning on GitHub secret scanning is a separate decision, outside
  this change.
