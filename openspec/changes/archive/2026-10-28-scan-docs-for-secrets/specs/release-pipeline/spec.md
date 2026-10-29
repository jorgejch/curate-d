# Spec Delta

## MODIFIED Requirements

### Requirement: SonarQube analyses explicitly selected source and test trees
SonarQube Cloud MUST use `src/`, `ops/`, `migrations/`, `docker/`, `pyproject.toml`,
`alembic.ini` and `compose.yaml` as its only sources, and `tests/` as tests. Scopes MUST be disjoint,
independent of pytest and free of source/test filters. Scoped text and top-level `docs/`
files MUST get secret detection; those files get nothing else. Scope or text-setting drift MUST fail unit tests naming the setting.
Contributor docs MUST explain scope maintenance, metrics and language support.

#### Scenario: Existing non-source paths
- **WHEN** a workflow, unlisted root file or OpenSpec artifact changes
- **THEN** SonarQube does not analyse it because it is outside the selected paths

#### Scenario: Document changes
- **WHEN** a file directly in `docs/` changes
- **THEN** SonarQube scans it for secrets only, and it receives no other issue, coverage or
  duplication measurement

#### Scenario: Generated diagram changes
- **WHEN** a file under `docs/diagrams/` changes
- **THEN** SonarQube does not analyse it, secrets included

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
- **WHEN** text inclusions no longer cover all scoped text files and the files directly in
  `docs/`, add any other path, or are deactivated
- **THEN** the unit suite fails naming the setting
