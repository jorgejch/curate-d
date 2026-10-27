## MODIFIED Requirements

### Requirement: SonarQube measures production code only
SonarQube Cloud MUST count coverage on `src/` only, and duplication on `src/` and
`infrastructure/` only. It MUST explicitly classify `tests/` as test code, independently
of pytest configuration, so tests count toward neither metric. Coverage MUST import
only `coverage.xml` from the unit suite. A scope or report-path drift MUST fail the unit
suite naming the setting.

#### Scenario: Uncovered helper in infrastructure
- **WHEN** a script under `infrastructure/` has no unit test
- **THEN** SonarQube's coverage metric is unchanged, and a block it copies from `src/` is
  reported as duplication

#### Scenario: Duplicated test fixture
- **WHEN** two test modules under `tests/` share a copied block
- **THEN** SonarQube classifies them as tests and reports neither coverage nor duplication
  for them

#### Scenario: Metric setting drifts
- **WHEN** coverage exclusions no longer name only `infrastructure/**`, duplication
  exclusions are added, or the coverage report path changes
- **THEN** the unit suite fails naming the setting

#### Scenario: New top-level directory left unlisted
- **WHEN** a top-level directory is added outside the selected source and test roots
- **THEN** its files remain outside analysis and both metrics without adding exclusions

## REMOVED Requirements

### Requirement: SonarQube does not analyse docs/ or openspec/
**Reason**: Positive source and test roots replace repository-wide analysis with exclusions.
**Migration**: Analyse only `src/`, `infrastructure/` and `tests/`; remove global exclusions
and the out-of-scope Docker rule suppression.

## ADDED Requirements

### Requirement: SonarQube analyses explicitly selected source and test trees
SonarQube Cloud MUST use `src/` and `infrastructure/` as its only source roots and `tests/`
as its only test root. These scopes MUST be disjoint and MUST NOT depend on pytest's
`testpaths`. It MUST NOT enumerate other paths through exclusions or inclusions. Scope
drift MUST fail the unit suite naming the setting. Contributor documentation MUST explain
how to expand the roots and review metric scope.

#### Scenario: Existing non-source paths
- **WHEN** a migration, Dockerfile, workflow, root file, document or OpenSpec artifact changes
- **THEN** SonarQube does not analyse it because it is outside the selected roots

#### Scenario: New top-level directory
- **WHEN** a directory outside the selected roots is added
- **THEN** it receives no Sonar analysis, coverage or duplication measurement, without a
  new exclusion or test update

#### Scenario: Source and test roots drift
- **WHEN** a source or test root changes or a source root overlaps the test tree
- **THEN** the unit suite fails naming the setting