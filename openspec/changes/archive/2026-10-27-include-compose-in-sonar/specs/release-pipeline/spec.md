## MODIFIED Requirements

### Requirement: SonarQube measures production code only
SonarQube Cloud MUST count coverage on `src/` only. All selected source paths MUST remain
eligible for duplication measurement where their language analyzer supports it, without
duplication exclusions. It MUST explicitly classify `tests/` as test code, independently
of pytest configuration, so tests count toward neither metric. Coverage MUST import
only `coverage.xml` from the unit suite. A scope or report-path drift MUST fail the unit
suite naming the setting.

#### Scenario: Uncovered helper in infrastructure
- **WHEN** a script under `ops/` has no unit test
- **THEN** SonarQube's coverage metric is unchanged, and a block it copies from `src/` is
  reported as duplication

#### Scenario: Duplicated test fixture
- **WHEN** two test modules under `tests/` share a copied block
- **THEN** SonarQube classifies them as tests and reports neither coverage nor duplication
  for them

#### Scenario: Metric setting drifts
- **WHEN** coverage exclusions no longer name exactly `ops/**`, `migrations/**`,
  `docker/**`, `pyproject.toml`, `alembic.ini` and `compose.yaml`, duplication exclusions are added,
  or the coverage report path changes
- **THEN** the unit suite fails naming the setting

#### Scenario: New top-level directory left unlisted
- **WHEN** a top-level directory is added outside the selected source and test roots
- **THEN** its files remain outside analysis and both metrics without adding exclusions

#### Scenario: Uncovered migration
- **WHEN** a migration has no unit coverage or copies a block from another source root
- **THEN** it does not lower coverage, but remains eligible for duplication findings

#### Scenario: Image and configuration coverage
- **WHEN** `docker/`, `pyproject.toml`, `alembic.ini` or `compose.yaml` has no unit coverage
- **THEN** it does not lower application coverage

### Requirement: SonarQube analyses explicitly selected source and test trees
SonarQube Cloud MUST use `src/`, `ops/`, `migrations/`, `docker/`, `pyproject.toml`,
`alembic.ini` and `compose.yaml` as its only sources, and `tests/` as tests. Scopes MUST be disjoint,
independent of pytest and free of source/test filters. Scoped text MUST be eligible for
secret detection. Scope or text-setting drift MUST fail unit tests naming the setting.
Contributor docs MUST explain scope maintenance, metrics and language support.

#### Scenario: Existing non-source paths
- **WHEN** a workflow, unlisted root file, document or OpenSpec artifact changes
- **THEN** SonarQube does not analyse it because it is outside the selected paths

#### Scenario: New top-level directory
- **WHEN** a directory outside the selected paths is added
- **THEN** it receives no Sonar analysis, coverage or duplication measurement, without a
  new exclusion or test update

#### Scenario: Source and test roots drift
- **WHEN** a source or test path changes or a source path overlaps the test tree
- **THEN** the unit suite fails naming the setting

#### Scenario: Migration changes
- **WHEN** a Python migration changes
- **THEN** SonarQube analyses it as source code

#### Scenario: Image definition changes
- **WHEN** `docker/Dockerfile` changes
- **THEN** it is in source scope for Docker analysis without blanket rule suppressions

#### Scenario: Project and migration configuration changes
- **WHEN** `pyproject.toml` or `alembic.ini` changes
- **THEN** the file is in source scope and eligible for text/secret detection without
  assigning a false language or claiming general TOML/INI language rules

#### Scenario: Compose definition changes
- **WHEN** `compose.yaml` changes
- **THEN** it is in source scope and eligible for text/secret detection without claiming
  Dockerfile-specific rules apply to Compose

#### Scenario: Secret detection configuration drifts
- **WHEN** text inclusions no longer cover all scoped text files or are deactivated
- **THEN** the unit suite fails naming the setting